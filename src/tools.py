"""AI tool definitions for the all-about-ads MCP server."""

import asyncio
import json
import os
from typing import Any

from apify_client import ApifyClientAsync
from mcp.server.fastmcp import Context

from src.server import mcp
from src.storage import (
    RESULTS_DIR,
    load_pending_run,
    load_results,
    save_pending_run,
    save_results,
    summarize_fb_ads,
    summarize_google_ads,
    summarize_google_search,
    summarize_ig_profiles,
)

FACEBOOK_ADS_ACTOR_ID = "20nRTxLD3a3jIlZbZ"
INSTAGRAM_PROFILES_ACTOR_ID = "98ivcMaUAxs5pu9tV"
GOOGLE_ADS_TRANSPARENCY_ACTOR_ID = "pkJmSVBI83vFyy2r5"
GOOGLE_SEARCH_ACTOR_ID = "563JCPLOqM1kMmbbP"

POLL_INTERVAL_SECONDS = 5
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}
INLINE_PREVIEW_LIMIT = 5  # max compact items returned inline; rest are in the saved file


def _preview(summaries: list[dict], total: int) -> dict:
    """Return a capped inline preview + a note when items were truncated."""
    result: dict[str, Any] = {"items": summaries[:INLINE_PREVIEW_LIMIT]}
    if total > INLINE_PREVIEW_LIMIT:
        result["note"] = (
            f"Showing {INLINE_PREVIEW_LIMIT} of {total}. "
            "Use read_saved_results to page through the full dataset."
        )
    return result


def _get_client() -> ApifyClientAsync:
    token = os.environ.get("APIFY_API_TOKEN")
    if not token:
        raise RuntimeError(
            "APIFY_API_TOKEN is not set. Add it to your .env file or environment "
            "(see .env.example)."
        )
    return ApifyClientAsync(token)


async def _notify(ctx: Context | None, message: str, progress: float | None = None) -> None:
    """Best-effort progress/log reporting; never fails the tool call."""
    if ctx is None:
        return
    try:
        await ctx.info(message)
        if progress is not None:
            await ctx.report_progress(progress, total=1.0, message=message)
    except Exception:
        pass


async def _start_actor_nonblocking(
    actor_id: str, run_input: dict[str, Any], ctx: Context | None
) -> str:
    """Start an Apify actor and return the run ID immediately without waiting."""
    client = _get_client()
    run = await client.actor(actor_id).start(run_input=run_input)
    await _notify(ctx, f"Apify run {run.id} started in background — call check_run_status to monitor.")
    return run.id


async def _collect_actor_results(run_id: str, ctx: Context | None) -> list[dict]:
    """Fetch results from an already-completed Apify run (no polling)."""
    client = _get_client()
    run = await client.run(run_id).get()
    if run is None:
        raise RuntimeError(f"Apify run {run_id} not found.")
    if str(run.status) not in TERMINAL_STATUSES:
        raise RuntimeError(
            f"Run {run_id} is not finished yet (status: {run.status}). "
            "Call check_run_status first."
        )
    if str(run.status) != "SUCCEEDED":
        raise RuntimeError(f"Run {run_id} ended with status {run.status}: {run.status_message or ''}")

    await _notify(ctx, f"Fetching results for run {run_id}...", 0.95)
    dataset = client.dataset(run.default_dataset_id)
    items: list[dict] = [item async for item in dataset.iterate_items()]
    if not items:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        items = [item async for item in dataset.iterate_items()]
    return items


async def _run_actor(
    actor_id: str, run_input: dict[str, Any], ctx: Context | None
) -> list[dict]:
    """Start an Apify actor run, poll until it finishes, return dataset items.

    Polling (instead of a single blocking call) lets us emit progress
    notifications so long runs don't look stalled to the MCP client.
    """
    client = _get_client()

    run = await client.actor(actor_id).start(run_input=run_input)
    await _notify(ctx, f"Apify run {run.id} started; this can take a few minutes.", 0.1)

    run_client = client.run(run.id)
    elapsed = 0
    while True:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS
        current = await run_client.get()
        if current is None:
            raise RuntimeError(f"Apify run {run.id} disappeared while polling.")
        status = str(current.status)
        if status in TERMINAL_STATUSES:
            run = current
            break
        # Asymptotically approach 90% while the run is in progress.
        progress = min(0.1 + elapsed / (elapsed + 60), 0.9)
        await _notify(ctx, f"Apify run {run.id}: {status} ({elapsed}s elapsed)", progress)

    if str(run.status) != "SUCCEEDED":
        raise RuntimeError(
            f"Apify run {run.id} finished with status {run.status}: "
            f"{run.status_message or 'no status message'}"
        )

    await _notify(ctx, "Run succeeded; fetching results...", 0.95)
    dataset = client.dataset(run.default_dataset_id)
    items: list[dict] = [item async for item in dataset.iterate_items()]
    if not items:
        # Dataset writes can lag slightly behind run completion; retry once.
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        items = [item async for item in dataset.iterate_items()]
    return items


@mcp.tool()
async def search_facebook_ads(
    search_queries: list[str],
    max_results_per_query: int = 10,
    enrich_with_ad_details: bool = False,
    sort_by: str = "SORT_BY_TOTAL_IMPRESSIONS",
    country: str | None = None,
    content_languages: list[str] | None = None,
    publisher_platforms: list[str] | None = None,
    active_status: str = "ALL",
    ad_type: str = "ALL",
    media_type: str = "ALL",
    start_date: str | None = None,
    end_date: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Search the Facebook Ads Library for ads matching the given queries.

    NOTE: This runs a remote scraper and typically takes 30s to a few minutes.
    Full results are saved to a JSON file (path returned as `file_path`); only a
    compact summary is returned here. Use read_saved_results to inspect the
    full data without flooding the context.

    Args:
        search_queries: Keywords or brand names to search for (e.g. ["nike", "adidas"]).
        max_results_per_query: Maximum number of ads to return per query
            (the actor requires a minimum of 10).
        enrich_with_ad_details: Fetch extra details per ad (slower and more expensive).
        sort_by: Sort order. One of SORT_BY_TOTAL_IMPRESSIONS,
            SORT_BY_RELEVANCY_MONTHLY_GROUPED.
        country: Single ISO country code to filter by (e.g. "US", "IN") or "ALL".
            None for no filter.
        content_languages: Language codes to filter ad content by (e.g. ["en"]).
        publisher_platforms: Platforms to filter by (e.g. ["facebook", "instagram"]).
        active_status: ALL, ACTIVE, or INACTIVE.
        ad_type: ALL, POLITICAL_AND_ISSUE_ADS, HOUSING_ADS, EMPLOYMENT_ADS, CREDIT_ADS.
        media_type: ALL, IMAGE, MEME, IMAGE_AND_MEME, VIDEO, NONE.
        start_date: Earliest ad delivery date, YYYY-MM-DD. None for no lower bound.
        end_date: Latest ad delivery date, YYYY-MM-DD. None for no upper bound.

    Returns:
        {"file_path": ..., "result_count": ..., "queries": ..., "ads": [compact summaries]}
    """
    run_input = {
        "searchQueries": search_queries,
        # The actor rejects values below 10.
        "maxResultsPerQuery": max(max_results_per_query, 10),
        "enrichWithAdDetails": enrich_with_ad_details,
        "sortBy": sort_by,
        "countries": country,
        "contentLanguages": content_languages,
        "publisherPlatforms": publisher_platforms,
        "activeStatus": active_status,
        "adType": ad_type,
        "mediaType": media_type,
        "startDate": start_date,
        "endDate": end_date,
    }
    # The actor's input schema rejects explicit nulls, so omit unset fields.
    run_input = {k: v for k, v in run_input.items() if v is not None}

    items = await _run_actor(FACEBOOK_ADS_ACTOR_ID, run_input, ctx)

    file_path = save_results(
        "fb_ads",
        items,
        meta={"tool": "search_facebook_ads", "queries": search_queries, "input": run_input},
    )
    await _notify(ctx, f"Saved {len(items)} ads to {file_path}", 1.0)

    summaries = summarize_fb_ads(items)
    return {
        "file_path": str(file_path),
        "result_count": len(items),
        "queries": search_queries,
        "ads": _preview(summaries, len(items)),
    }


@mcp.tool()
async def scrape_instagram_profiles(
    profiles: list[str],
    include_recent_posts: bool = True,
    ctx: Context | None = None,
) -> dict:
    """Scrape public Instagram profile data (and optionally recent posts).

    NOTE: This runs a remote scraper and typically takes 30s to a few minutes.
    Full results are saved to a JSON file (path returned as `file_path`); only a
    compact summary is returned here. Use read_saved_results to inspect the
    full data without flooding the context.

    Args:
        profiles: Instagram usernames to scrape (e.g. ["natgeo", "nike"]).
        include_recent_posts: Also fetch each profile's recent posts.

    Returns:
        {"file_path": ..., "result_count": ..., "profiles": [compact summaries]}
    """
    run_input = {
        "profiles": profiles,
        "includeRecentPosts": include_recent_posts,
    }

    items = await _run_actor(INSTAGRAM_PROFILES_ACTOR_ID, run_input, ctx)

    file_path = save_results(
        "ig_profiles",
        items,
        meta={"tool": "scrape_instagram_profiles", "queries": profiles, "input": run_input},
    )
    await _notify(ctx, f"Saved {len(items)} profiles to {file_path}", 1.0)

    summaries = summarize_ig_profiles(items)
    return {
        "file_path": str(file_path),
        "result_count": len(items),
        "profiles": _preview(summaries, len(items)),
    }


@mcp.tool()
async def search_google_ads(
    advertisers: list[str],
    max_ads_per_advertiser: int = 100,
    start_date: str | None = None,
    end_date: str | None = None,
    region: str | None = None,
    political_ads_only: bool = False,
    ctx: Context | None = None,
) -> dict:
    """Search the Google Ads Transparency Center for ads by advertiser.

    NOTE: This runs a remote scraper and typically takes 30s to a few minutes.
    Full results are saved to a JSON file (path returned as `file_path`); only a
    compact summary is returned here. Use read_saved_results to inspect the
    full data without flooding the context.

    Args:
        advertisers: Brand names, domains (e.g. "nike.com"), full URLs, or
            advertiser IDs starting with "AR" (e.g. "AR01614014350098432001").
            Mix formats freely — the actor classifies each entry automatically.
        max_ads_per_advertiser: Maximum ads returned per advertiser (0 = unlimited),
            default 100.
        start_date: Earliest first-shown date, YYYY-MM-DD. None for no lower bound.
        end_date: Latest last-shown date, YYYY-MM-DD. None for no upper bound.
        region: 2-letter ISO country code to filter by (e.g. "US", "GB").
            None or blank for worldwide.
        political_ads_only: Restrict results to political/election ads, default False.

    Returns:
        {"file_path": ..., "result_count": ..., "advertisers": ..., "ads": [compact summaries]}
    """
    run_input = {
        "advertisers": advertisers,
        "maxAdsPerAdvertiser": max_ads_per_advertiser,
        "startDate": start_date,
        "endDate": end_date,
        "region": region,
        "politicalAdsOnly": political_ads_only,
    }
    run_input = {k: v for k, v in run_input.items() if v is not None}

    items = await _run_actor(GOOGLE_ADS_TRANSPARENCY_ACTOR_ID, run_input, ctx)

    file_path = save_results(
        "google_ads",
        items,
        meta={"tool": "search_google_ads", "queries": advertisers, "input": run_input},
    )
    await _notify(ctx, f"Saved {len(items)} ads to {file_path}", 1.0)

    summaries = summarize_google_ads(items)
    return {
        "file_path": str(file_path),
        "result_count": len(items),
        "advertisers": advertisers,
        "ads": _preview(summaries, len(items)),
    }


@mcp.tool()
async def search_google(
    queries: list[str],
    max_pages_per_query: int = 1,
    results_per_page: int = 10,
    country_code: str | None = None,
    search_language: str | None = None,
    quick_date_range: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Search Google for organic results to research brands and ads.

    NOTE: This runs a remote scraper and typically takes 30s to a few minutes.
    Full results are saved to a JSON file (path returned as `file_path`); only a
    compact summary is returned here. Use read_saved_results to inspect the
    full data without flooding the context.

    Args:
        queries: Search queries to run (e.g. ["nike ad strategy 2025"]).
        max_pages_per_query: Number of result pages per query (each ~10 results),
            default 1.
        results_per_page: Results per page (10–100), default 10.
        country_code: 2-letter code controlling the Google domain used
            (e.g. "gb" → google.co.uk, "es" → google.es). None for US (google.com).
        search_language: Language code to filter results by (e.g. "en", "fr").
        quick_date_range: Relative date filter. Format: d<N> (past N days),
            w<N> (past N weeks), m<N> (past N months), y<N> (past N years).
            Examples: "d10", "w2", "m6", "y1". None for no date filter.

    Returns:
        {"file_path": ..., "result_count": ..., "queries": ..., "results": [compact summaries]}
    """
    run_input = {
        "keyword": "\n".join(queries),  # actor uses "keyword", not "queries"
        "maxPagesPerQuery": max_pages_per_query,
        "resultsPerPage": results_per_page,
        "countryCode": country_code,
        "searchLanguage": search_language,
        "quickDateRange": quick_date_range,
    }
    run_input = {k: v for k, v in run_input.items() if v is not None}

    items = await _run_actor(GOOGLE_SEARCH_ACTOR_ID, run_input, ctx)

    # Each dataset item is one results page; count individual URLs across all pages.
    total_results = sum(len(page.get("results") or []) for page in items)
    file_path = save_results(
        "google_search",
        items,
        meta={
            "tool": "search_google",
            "queries": queries,
            "input": run_input,
            "item_count": total_results,
        },
    )
    await _notify(ctx, f"Saved {total_results} results ({len(items)} pages) to {file_path}", 1.0)

    summaries = summarize_google_search(items)
    return {
        "file_path": str(file_path),
        "result_count": total_results,
        "queries": queries,
        "results": _preview(summaries, len(summaries)),
    }


@mcp.tool()
def list_saved_results() -> list[dict]:
    """List previously saved result files (from earlier scraper runs).

    Returns one entry per file with its path, size, item count, and the tool /
    queries that produced it. Use read_saved_results to read items from a file.
    """
    if not RESULTS_DIR.exists():
        return []

    entries = []
    for path in sorted(RESULTS_DIR.glob("*.json"), reverse=True):
        entry: dict[str, Any] = {
            "file_path": str(path),
            "size_bytes": path.stat().st_size,
        }
        try:
            meta = load_results(str(path))["meta"]
            entry.update(
                {
                    "tool": meta.get("tool"),
                    "queries": meta.get("queries"),
                    "saved_at": meta.get("saved_at"),
                    "item_count": meta.get("item_count"),
                }
            )
        except Exception as exc:
            entry["error"] = f"unreadable: {exc}"
        entries.append(entry)
    return entries


@mcp.tool()
def read_saved_results(
    file_path: str,
    offset: int = 0,
    limit: int = 5,
    fields: list[str] | None = None,
    query: str | None = None,
) -> dict:
    """Read a slice of items from a previously saved results file.

    Results files can be very large, so read them in small pages and/or project
    only the fields you need.

    Args:
        file_path: Path returned by search_facebook_ads / scrape_instagram_profiles
            / list_saved_results (a bare filename also works).
        offset: Index of the first item to return.
        limit: Maximum number of items to return.
        fields: If given, only these top-level keys are included per item.
        query: Optional case-insensitive substring filter; only items whose
            JSON representation contains it are returned.

    Returns:
        {"meta": ..., "total_items": ..., "matched_items": ..., "offset": ...,
         "returned": ..., "items": [...]}
    """
    data = load_results(file_path)
    items: list[dict] = data["items"]
    total = len(items)

    if query:
        needle = query.lower()
        items = [i for i in items if needle in json.dumps(i, default=str).lower()]
    matched = len(items)

    page = items[offset : offset + limit]
    if fields:
        page = [{k: item.get(k) for k in fields} for item in page]

    return {
        "meta": data.get("meta", {}),
        "total_items": total,
        "matched_items": matched,
        "offset": offset,
        "returned": len(page),
        "items": page,
    }


# ---------------------------------------------------------------------------
# Non-blocking (fire-and-forget) variants
# ---------------------------------------------------------------------------

@mcp.tool()
async def start_facebook_ads_scrape(
    search_queries: list[str],
    max_results_per_query: int = 10,
    enrich_with_ad_details: bool = False,
    sort_by: str = "SORT_BY_TOTAL_IMPRESSIONS",
    country: str | None = None,
    content_languages: list[str] | None = None,
    publisher_platforms: list[str] | None = None,
    active_status: str = "ALL",
    ad_type: str = "ALL",
    media_type: str = "ALL",
    start_date: str | None = None,
    end_date: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Start a Facebook Ads Library scrape and return immediately with a run_id.

    Unlike search_facebook_ads, this does NOT wait for the scrape to finish.
    Use check_run_status to monitor progress, then collect_scrape_results to
    retrieve the data once the run has SUCCEEDED. This lets you kick off multiple
    scrapers in parallel and do other work (e.g. search_google) in the meantime.

    Args: same as search_facebook_ads.

    Returns:
        {"run_id": ..., "status": "RUNNING", "hint": "..."}
    """
    run_input = {
        "searchQueries": search_queries,
        "maxResultsPerQuery": max(max_results_per_query, 10),
        "enrichWithAdDetails": enrich_with_ad_details,
        "sortBy": sort_by,
        "countries": country,
        "contentLanguages": content_languages,
        "publisherPlatforms": publisher_platforms,
        "activeStatus": active_status,
        "adType": ad_type,
        "mediaType": media_type,
        "startDate": start_date,
        "endDate": end_date,
    }
    run_input = {k: v for k, v in run_input.items() if v is not None}
    run_id = await _start_actor_nonblocking(FACEBOOK_ADS_ACTOR_ID, run_input, ctx)
    save_pending_run(run_id, "fb_ads", search_queries, run_input)
    return {
        "run_id": run_id,
        "status": "RUNNING",
        "hint": "Call check_run_status([run_id]) to monitor, then collect_scrape_results(run_id) when SUCCEEDED.",
    }


@mcp.tool()
async def start_google_ads_scrape(
    advertisers: list[str],
    max_ads_per_advertiser: int = 100,
    start_date: str | None = None,
    end_date: str | None = None,
    region: str | None = None,
    political_ads_only: bool = False,
    ctx: Context | None = None,
) -> dict:
    """Start a Google Ads Transparency Center scrape and return immediately with a run_id.

    Unlike search_google_ads, this does NOT wait for the scrape to finish.
    Use check_run_status to monitor progress, then collect_scrape_results to
    retrieve the data once the run has SUCCEEDED.

    Args: same as search_google_ads.

    Returns:
        {"run_id": ..., "status": "RUNNING", "hint": "..."}
    """
    run_input = {
        "advertisers": advertisers,
        "maxAdsPerAdvertiser": max_ads_per_advertiser,
        "startDate": start_date,
        "endDate": end_date,
        "region": region,
        "politicalAdsOnly": political_ads_only,
    }
    run_input = {k: v for k, v in run_input.items() if v is not None}
    run_id = await _start_actor_nonblocking(GOOGLE_ADS_TRANSPARENCY_ACTOR_ID, run_input, ctx)
    save_pending_run(run_id, "google_ads", advertisers, run_input)
    return {
        "run_id": run_id,
        "status": "RUNNING",
        "hint": "Call check_run_status([run_id]) to monitor, then collect_scrape_results(run_id) when SUCCEEDED.",
    }


@mcp.tool()
async def start_instagram_scrape(
    profiles: list[str],
    include_recent_posts: bool = True,
    ctx: Context | None = None,
) -> dict:
    """Start an Instagram profile scrape and return immediately with a run_id.

    Unlike scrape_instagram_profiles, this does NOT wait for the scrape to finish.
    Use check_run_status to monitor progress, then collect_scrape_results to
    retrieve the data once the run has SUCCEEDED.

    Args: same as scrape_instagram_profiles.

    Returns:
        {"run_id": ..., "status": "RUNNING", "hint": "..."}
    """
    run_input = {"profiles": profiles, "includeRecentPosts": include_recent_posts}
    run_id = await _start_actor_nonblocking(INSTAGRAM_PROFILES_ACTOR_ID, run_input, ctx)
    save_pending_run(run_id, "ig_profiles", profiles, run_input)
    return {
        "run_id": run_id,
        "status": "RUNNING",
        "hint": "Call check_run_status([run_id]) to monitor, then collect_scrape_results(run_id) when SUCCEEDED.",
    }


@mcp.tool()
async def check_run_status(run_ids: list[str]) -> list[dict]:
    """Check the current status of one or more background Apify scrape runs.

    Args:
        run_ids: List of run IDs returned by start_facebook_ads_scrape,
            start_google_ads_scrape, or start_instagram_scrape.

    Returns:
        List of {"run_id": ..., "status": ..., "done": bool, "succeeded": bool}
    """
    client = _get_client()
    results = []
    for run_id in run_ids:
        run = await client.run(run_id).get()
        if run is None:
            results.append({"run_id": run_id, "status": "NOT_FOUND", "done": True, "succeeded": False})
        else:
            status = str(run.status)
            results.append({
                "run_id": run_id,
                "status": status,
                "done": status in TERMINAL_STATUSES,
                "succeeded": status == "SUCCEEDED",
            })
    return results


@mcp.tool()
async def collect_scrape_results(run_id: str, ctx: Context | None = None) -> dict:
    """Collect and save results from a completed background scrape run.

    The run must have SUCCEEDED (check with check_run_status first). Results are
    saved to a file and a compact summary is returned inline, identical to the
    blocking search_* / scrape_* tools.

    Args:
        run_id: The run ID returned by start_facebook_ads_scrape,
            start_google_ads_scrape, or start_instagram_scrape.

    Returns:
        {"file_path": ..., "result_count": ..., "items": [compact summaries]}
    """
    pending = load_pending_run(run_id)
    if not pending:
        raise ValueError(
            f"No metadata found for run {run_id}. "
            "Only runs started via start_facebook_ads_scrape / start_google_ads_scrape / "
            "start_instagram_scrape are tracked."
        )

    items = await _collect_actor_results(run_id, ctx)
    run_type: str = pending["type"]
    queries: list = pending["queries"]
    run_input: dict = pending["input"]

    summarizers = {
        "fb_ads": summarize_fb_ads,
        "google_ads": summarize_google_ads,
        "ig_profiles": summarize_ig_profiles,
    }
    summarize = summarizers.get(run_type, lambda x: x)
    summaries = summarize(items)

    file_path = save_results(
        run_type,
        items,
        meta={"tool": f"start_{run_type}_scrape", "queries": queries, "input": run_input},
    )
    await _notify(ctx, f"Saved {len(items)} items to {file_path}", 1.0)

    return {
        "file_path": str(file_path),
        "result_count": len(items),
        "queries": queries,
        "items": _preview(summaries, len(items)),
    }
