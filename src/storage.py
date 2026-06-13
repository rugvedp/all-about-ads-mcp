"""Persistence and summarization helpers for large Apify results.

Full actor results are written to JSON files under the system temp dir so that
tools can return a small summary plus a file path instead of exploding the
model's context window.
"""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RESULTS_DIR = Path(tempfile.gettempdir()) / "all-about-ads-mcp"


def save_results(prefix: str, items: list[dict], meta: dict[str, Any]) -> Path:
    """Save full result items to a timestamped JSON file and return its path."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = RESULTS_DIR / f"{prefix}_{timestamp}.json"
    payload = {
        "meta": {
            **meta,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            # Honour a caller-supplied item_count (e.g. search_google counts URLs, not pages).
            "item_count": meta.get("item_count", len(items)),
        },
        "items": items,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, default=str))
    return path


def load_results(file_path: str) -> dict[str, Any]:
    """Load a previously saved results file ({"meta": ..., "items": [...]})."""
    path = Path(file_path)
    if not path.is_absolute():
        path = RESULTS_DIR / path
    if not path.exists():
        raise FileNotFoundError(
            f"No saved results at {path}. Use list_saved_results to see available files."
        )
    return json.loads(path.read_text())


def _first(item: dict, *keys: str) -> Any:
    """Return the first non-None value among (possibly nested dotted) keys."""
    for key in keys:
        value: Any = item
        for part in key.split("."):
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(part)
        if value is not None:
            return value
    return None


def summarize_fb_ads(items: list[dict]) -> list[dict]:
    """Compact per-ad summary safe to return to the model."""
    summaries = []
    for item in items:
        summaries.append(
            {
                "id": _first(item, "id", "ad_archive_id", "adArchiveID"),
                "page_name": _first(
                    item, "page_name", "pageName", "snapshot.page_name", "ad.page_name"
                ),
                "title": _first(item, "title", "snapshot.title", "ad.title"),
                "caption": _truncate(_first(item, "caption", "snapshot.caption")),
                "cta_text": _first(item, "cta_text", "snapshot.cta_text"),
                "ad_url": _first(item, "ad_url", "url"),
                "link_url": _first(item, "link_url", "snapshot.link_url"),
                "is_active": _first(item, "is_active", "isActive"),
                "start_date": _first(item, "start_date", "startDate"),
                "end_date": _first(item, "end_date", "endDate"),
                "countries": _first(item, "countries"),
            }
        )
    return summaries


def summarize_ig_profiles(items: list[dict]) -> list[dict]:
    """Compact per-profile summary safe to return to the model."""
    summaries = []
    for item in items:
        recent_posts = _first(item, "recent_posts", "recentPosts", "latestPosts") or []
        summaries.append(
            {
                "username": _first(item, "username", "userName"),
                "full_name": _first(item, "full_name", "fullName"),
                "followers": _first(item, "followers", "followersCount", "followers_count"),
                "following": _first(item, "following", "followsCount", "following_count"),
                "posts_count": _first(item, "posts_count", "postsCount", "media_count"),
                "is_verified": _first(item, "is_verified", "verified"),
                "biography": _truncate(_first(item, "biography", "bio")),
                "url": _first(item, "url", "profile_url", "profileUrl"),
                "recent_posts_included": len(recent_posts)
                if isinstance(recent_posts, list)
                else None,
            }
        )
    return summaries


def summarize_google_ads(items: list[dict]) -> list[dict]:
    """Compact per-ad summary for Google Ads Transparency Center results."""
    summaries = []
    for item in items:
        summaries.append(
            {
                "advertiser": _first(item, "advertiserName", "advertiser", "brand"),
                "advertiser_id": _first(item, "advertiserId", "advertiser_id"),
                "headline": _truncate(_first(item, "headline", "title", "adTitle")),
                "description": _truncate(_first(item, "description", "adDescription", "body")),
                "format": _first(item, "format", "adFormat", "type"),
                "regions": _first(item, "regions", "region", "targetedRegion"),
                "destination_url": _first(item, "destinationUrl", "destination_url", "landingUrl"),
                "first_shown": _first(item, "firstShown", "first_shown", "startDate"),
                "last_shown": _first(item, "lastShown", "last_shown", "endDate"),
                "days_active": _first(item, "daysActive", "days_active"),
                "ad_url": _first(item, "adTransparencyUrl", "adUrl", "ad_url"),
            }
        )
    return summaries


def summarize_google_search(items: list[dict]) -> list[dict]:
    """Compact per-result summary for Google organic SERP results.

    Each dataset item is one results page; this flattens the nested
    results[] array into individual per-URL summaries.
    """
    summaries = []
    for page in items:
        query = _first(page, "search_term", "searchTerm", "query")
        for result in page.get("results") or []:
            summaries.append(
                {
                    "query": query,
                    "position": result.get("position"),
                    "title": result.get("title"),
                    "url": result.get("url"),
                    "description": _truncate(result.get("description")),
                }
            )
    return summaries


def _truncate(value: Any, max_len: int = 200) -> Any:
    if isinstance(value, str) and len(value) > max_len:
        return value[:max_len] + "..."
    return value
