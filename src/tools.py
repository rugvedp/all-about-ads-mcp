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
    load_results,
    save_results,
    summarize_fb_ads,
    summarize_ig_profiles,
)

FACEBOOK_ADS_ACTOR_ID = "20nRTxLD3a3jIlZbZ"
INSTAGRAM_PROFILES_ACTOR_ID = "98ivcMaUAxs5pu9tV"

POLL_INTERVAL_SECONDS = 5
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}


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

    return {
        "file_path": str(file_path),
        "result_count": len(items),
        "queries": search_queries,
        "ads": summarize_fb_ads(items),
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

    return {
        "file_path": str(file_path),
        "result_count": len(items),
        "profiles": summarize_ig_profiles(items),
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
