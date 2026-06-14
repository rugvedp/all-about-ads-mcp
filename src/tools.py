"""AI tool definitions for the all-about-ads MCP server."""

import asyncio
import json
import os
from typing import Annotated, Any
from typing_extensions import TypedDict

from apify_client import ApifyClientAsync
from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from pydantic import Field

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

# ---------------------------------------------------------------------------
# Output schemas
# ---------------------------------------------------------------------------

class AdsResult(TypedDict):
    file_path: str
    result_count: int
    queries: list[str]
    ads: dict

class ProfilesResult(TypedDict):
    file_path: str
    result_count: int
    profiles: dict

class SearchResult(TypedDict):
    file_path: str
    result_count: int
    queries: list[str]
    results: dict

class SavedFileEntry(TypedDict):
    file_path: str
    size_bytes: int
    tool: str
    queries: list[str]
    saved_at: str
    item_count: int

class SavedResultsPage(TypedDict):
    meta: dict
    total_items: int
    matched_items: int
    offset: int
    returned: int
    items: list[dict]

class RunStarted(TypedDict):
    run_id: str
    status: str
    hint: str

class RunStatus(TypedDict):
    run_id: str
    status: str
    done: bool
    succeeded: bool


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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def search_facebook_ads(
    search_queries: Annotated[list[str], Field(description="Keywords or brand names to search for, e.g. ['nike', 'adidas'].")],
    max_results_per_query: Annotated[int, Field(description="Maximum ads to return per query. Minimum 10 (actor limit).")] = 10,
    enrich_with_ad_details: Annotated[bool, Field(description="Fetch extra per-ad details. Slower and uses more Apify credits.")] = False,
    sort_by: Annotated[str, Field(description="Sort order: SORT_BY_TOTAL_IMPRESSIONS or SORT_BY_RELEVANCY_MONTHLY_GROUPED.")] = "SORT_BY_TOTAL_IMPRESSIONS",
    country: Annotated[str | None, Field(description="ISO country code to filter by, e.g. 'US', 'IN', or 'ALL'. None for no filter.")] = None,
    content_languages: Annotated[list[str] | None, Field(description="Language codes to filter ad content by, e.g. ['en', 'fr'].")] = None,
    publisher_platforms: Annotated[list[str] | None, Field(description="Platforms to filter by, e.g. ['facebook', 'instagram'].")] = None,
    active_status: Annotated[str, Field(description="Ad status filter: ALL, ACTIVE, or INACTIVE.")] = "ALL",
    ad_type: Annotated[str, Field(description="Ad type filter: ALL, POLITICAL_AND_ISSUE_ADS, HOUSING_ADS, EMPLOYMENT_ADS, or CREDIT_ADS.")] = "ALL",
    media_type: Annotated[str, Field(description="Media type filter: ALL, IMAGE, MEME, IMAGE_AND_MEME, VIDEO, or NONE.")] = "ALL",
    start_date: Annotated[str | None, Field(description="Earliest ad delivery date in YYYY-MM-DD format. None for no lower bound.")] = None,
    end_date: Annotated[str | None, Field(description="Latest ad delivery date in YYYY-MM-DD format. None for no upper bound.")] = None,
    ctx: Context | None = None,
) -> AdsResult:
    """Search the Facebook Ads Library for live ads matching the given queries.

    Runs a remote scraper (30s–few minutes). Full results are saved to a JSON
    file; only a compact preview is returned inline. Use read_saved_results to
    page through the full dataset without flooding context.
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def search_instagram_profiles(
    profiles: Annotated[list[str], Field(description="Instagram usernames to fetch, e.g. ['natgeo', 'nike'].")],
    include_recent_posts: Annotated[bool, Field(description="Also fetch each profile's recent posts (captions, likes, timestamps).")] = True,
    ctx: Context | None = None,
) -> ProfilesResult:
    """Fetch public Instagram profile data including follower counts, bio, and recent posts.

    Runs a remote scraper (30s–few minutes). Full results are saved to a JSON
    file; only a compact preview is returned inline. Use read_saved_results to
    page through the full dataset without flooding context.
    """
    run_input = {
        "profiles": profiles,
        "includeRecentPosts": include_recent_posts,
    }

    items = await _run_actor(INSTAGRAM_PROFILES_ACTOR_ID, run_input, ctx)

    file_path = save_results(
        "ig_profiles",
        items,
        meta={"tool": "search_instagram_profiles", "queries": profiles, "input": run_input},
    )
    await _notify(ctx, f"Saved {len(items)} profiles to {file_path}", 1.0)

    summaries = summarize_ig_profiles(items)
    return {
        "file_path": str(file_path),
        "result_count": len(items),
        "profiles": _preview(summaries, len(items)),
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def search_google_ads(
    advertisers: Annotated[list[str], Field(description="Brand names, domains (e.g. 'nike.com'), full URLs, or advertiser IDs starting with 'AR'. Mix formats freely.")],
    max_ads_per_advertiser: Annotated[int, Field(description="Maximum ads returned per advertiser. 0 means unlimited.")] = 100,
    start_date: Annotated[str | None, Field(description="Earliest first-shown date in YYYY-MM-DD format. None for no lower bound.")] = None,
    end_date: Annotated[str | None, Field(description="Latest last-shown date in YYYY-MM-DD format. None for no upper bound.")] = None,
    region: Annotated[str | None, Field(description="2-letter ISO country code to filter by, e.g. 'US', 'GB'. None for worldwide.")] = None,
    political_ads_only: Annotated[bool, Field(description="Restrict results to political and election ads only.")] = False,
    ctx: Context | None = None,
) -> AdsResult:
    """Search the Google Ads Transparency Center for live ads by advertiser name or domain.

    Returns headlines, formats, regions, days_active, and destination URLs.
    Runs a remote scraper (30s–few minutes). Full results are saved to a JSON
    file; only a compact preview is returned inline. Use read_saved_results to
    page through the full dataset without flooding context.
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def search_google(
    queries: Annotated[list[str], Field(description="Search queries to run, e.g. ['nike ad strategy 2025', 'adidas campaign news'].")],
    max_pages_per_query: Annotated[int, Field(description="Number of result pages per query. Each page contains ~10 results.")] = 1,
    results_per_page: Annotated[int, Field(description="Results per page, between 10 and 100.")] = 10,
    country_code: Annotated[str | None, Field(description="2-letter country code controlling the Google domain, e.g. 'gb' → google.co.uk. None uses google.com.")] = None,
    search_language: Annotated[str | None, Field(description="Language code to filter results by, e.g. 'en', 'fr', 'de'.")] = None,
    quick_date_range: Annotated[str | None, Field(description="Relative date filter: d<N> (days), w<N> (weeks), m<N> (months), y<N> (years). E.g. 'd10', 'w2', 'm6'.")] = None,
    ctx: Context | None = None,
) -> SearchResult:
    """Search Google for organic results to research brands, news, and ad context.

    Runs a remote scraper (30s–few minutes). Full results are saved to a JSON
    file; only a compact preview is returned inline. Use read_saved_results to
    page through the full dataset without flooding context.
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def list_saved_results() -> list[SavedFileEntry]:
    """List all result files saved from previous scraper runs.

    Returns one entry per file with path, size, item count, tool name, and queries.
    No Apify call is made — this is instant. Use read_saved_results to read items.
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def read_saved_results(
    file_path: Annotated[str, Field(description="Path returned by a search tool or list_saved_results. A bare filename also works.")],
    offset: Annotated[int, Field(description="Index of the first item to return. Use with limit to paginate.")] = 0,
    limit: Annotated[int, Field(description="Maximum number of items to return per call.")] = 5,
    fields: Annotated[list[str] | None, Field(description="If set, only these top-level keys are included per item. Use to reduce token usage.")] = None,
    query: Annotated[str | None, Field(description="Case-insensitive substring filter. Only items whose JSON contains this string are returned.")] = None,
) -> SavedResultsPage:
    """Read a paginated slice of items from a previously saved scraper results file.

    Results files can be very large. Read in small pages and project only the
    fields you need to stay within context limits.
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

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True))
async def start_facebook_ads_scrape(
    search_queries: Annotated[list[str], Field(description="Keywords or brand names to search for, e.g. ['nike', 'adidas'].")],
    max_results_per_query: Annotated[int, Field(description="Maximum ads to return per query. Minimum 10 (actor limit).")] = 10,
    enrich_with_ad_details: Annotated[bool, Field(description="Fetch extra per-ad details. Slower and uses more Apify credits.")] = False,
    sort_by: Annotated[str, Field(description="Sort order: SORT_BY_TOTAL_IMPRESSIONS or SORT_BY_RELEVANCY_MONTHLY_GROUPED.")] = "SORT_BY_TOTAL_IMPRESSIONS",
    country: Annotated[str | None, Field(description="ISO country code to filter by, e.g. 'US', 'IN', or 'ALL'. None for no filter.")] = None,
    content_languages: Annotated[list[str] | None, Field(description="Language codes to filter ad content by, e.g. ['en', 'fr'].")] = None,
    publisher_platforms: Annotated[list[str] | None, Field(description="Platforms to filter by, e.g. ['facebook', 'instagram'].")] = None,
    active_status: Annotated[str, Field(description="Ad status filter: ALL, ACTIVE, or INACTIVE.")] = "ALL",
    ad_type: Annotated[str, Field(description="Ad type filter: ALL, POLITICAL_AND_ISSUE_ADS, HOUSING_ADS, EMPLOYMENT_ADS, or CREDIT_ADS.")] = "ALL",
    media_type: Annotated[str, Field(description="Media type filter: ALL, IMAGE, MEME, IMAGE_AND_MEME, VIDEO, or NONE.")] = "ALL",
    start_date: Annotated[str | None, Field(description="Earliest ad delivery date in YYYY-MM-DD format. None for no lower bound.")] = None,
    end_date: Annotated[str | None, Field(description="Latest ad delivery date in YYYY-MM-DD format. None for no upper bound.")] = None,
    ctx: Context | None = None,
) -> RunStarted:
    """Start a Facebook Ads Library scrape in the background and return a run_id immediately.

    Use this instead of search_facebook_ads when running multiple scrapers in parallel.
    Call check_run_status to monitor progress, then collect_scrape_results once SUCCEEDED.
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True))
async def start_google_ads_scrape(
    advertisers: Annotated[list[str], Field(description="Brand names, domains (e.g. 'nike.com'), full URLs, or advertiser IDs starting with 'AR'. Mix formats freely.")],
    max_ads_per_advertiser: Annotated[int, Field(description="Maximum ads returned per advertiser. 0 means unlimited.")] = 100,
    start_date: Annotated[str | None, Field(description="Earliest first-shown date in YYYY-MM-DD format. None for no lower bound.")] = None,
    end_date: Annotated[str | None, Field(description="Latest last-shown date in YYYY-MM-DD format. None for no upper bound.")] = None,
    region: Annotated[str | None, Field(description="2-letter ISO country code to filter by, e.g. 'US', 'GB'. None for worldwide.")] = None,
    political_ads_only: Annotated[bool, Field(description="Restrict results to political and election ads only.")] = False,
    ctx: Context | None = None,
) -> RunStarted:
    """Start a Google Ads Transparency Center scrape in the background and return a run_id immediately.

    Use this instead of search_google_ads when running multiple scrapers in parallel.
    Call check_run_status to monitor progress, then collect_scrape_results once SUCCEEDED.
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True))
async def start_instagram_scrape(
    profiles: Annotated[list[str], Field(description="Instagram usernames to fetch, e.g. ['natgeo', 'nike'].")],
    include_recent_posts: Annotated[bool, Field(description="Also fetch each profile's recent posts (captions, likes, timestamps).")] = True,
    ctx: Context | None = None,
) -> RunStarted:
    """Start an Instagram profile scrape in the background and return a run_id immediately.

    Use this instead of search_instagram_profiles when running multiple scrapers in parallel.
    Call check_run_status to monitor progress, then collect_scrape_results once SUCCEEDED.
    """
    run_input = {"profiles": profiles, "includeRecentPosts": include_recent_posts}
    run_id = await _start_actor_nonblocking(INSTAGRAM_PROFILES_ACTOR_ID, run_input, ctx)
    save_pending_run(run_id, "ig_profiles", profiles, run_input)
    return {
        "run_id": run_id,
        "status": "RUNNING",
        "hint": "Call check_run_status([run_id]) to monitor, then collect_scrape_results(run_id) when SUCCEEDED.",
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def check_run_status(
    run_ids: Annotated[list[str], Field(description="Run IDs returned by start_facebook_ads_scrape, start_google_ads_scrape, or start_instagram_scrape.")],
) -> list[RunStatus]:
    """Check the current status of one or more background Apify scrape runs.

    Returns done and succeeded flags for each run so you know when to call
    collect_scrape_results.
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True))
async def collect_scrape_results(
    run_id: Annotated[str, Field(description="Run ID returned by start_facebook_ads_scrape, start_google_ads_scrape, or start_instagram_scrape.")],
    ctx: Context | None = None,
) -> SavedResultsPage:
    """Collect and save results from a completed background Apify scrape run.

    The run must have SUCCEEDED — check with check_run_status first. Results are
    saved to a file and a compact preview is returned inline, identical to the
    blocking search_* tools.
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
