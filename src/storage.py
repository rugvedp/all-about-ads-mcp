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
PENDING_RUNS_FILE = RESULTS_DIR / "pending_runs.json"


def save_pending_run(run_id: str, run_type: str, queries: list, input_params: dict) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    runs = _load_pending_runs()
    runs[run_id] = {
        "run_id": run_id,
        "type": run_type,
        "queries": queries,
        "input": input_params,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    PENDING_RUNS_FILE.write_text(json.dumps(runs, ensure_ascii=False))


def load_pending_run(run_id: str) -> dict | None:
    return _load_pending_runs().get(run_id)


def _load_pending_runs() -> dict:
    if not PENDING_RUNS_FILE.exists():
        return {}
    try:
        return json.loads(PENDING_RUNS_FILE.read_text())
    except Exception:
        return {}


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


_CDN_HOSTS = (
    "fbcdn.net",
    "cdninstagram.com",
    "googleusercontent.com",
    "gstatic.com",
    "doubleclick.net",
    "googlevideo.com",
    "akamaized.net",
    "cloudfront.net",
    "fastly.net",
)


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


def _is_cdn(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        return any(cdn in host for cdn in _CDN_HOSTS)
    except Exception:
        return False


def _domain(url: Any) -> str | None:
    """Return just the domain of a URL, or None if it's a CDN or unparseable."""
    if not isinstance(url, str) or _is_cdn(url):
        return None
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower().lstrip("www.")
        return host or None
    except Exception:
        return None


def _compact(d: dict) -> dict:
    """Drop None values so they don't waste tokens."""
    return {k: v for k, v in d.items() if v is not None}


def _truncate(value: Any, max_len: int = 150) -> Any:
    if isinstance(value, str) and len(value) > max_len:
        return value[:max_len] + "…"
    return value


def summarize_fb_ads(items: list[dict]) -> list[dict]:
    summaries = []
    for item in items:
        summaries.append(_compact({
            "id": _first(item, "id", "ad_archive_id", "adArchiveID"),
            "page": _first(item, "page_name", "pageName", "snapshot.page_name", "ad.page_name"),
            "title": _truncate(_first(item, "title", "snapshot.title", "ad.title")),
            "caption": _truncate(_first(item, "caption", "snapshot.caption")),
            "cta": _first(item, "cta_text", "snapshot.cta_text"),
            "landing_domain": _domain(_first(item, "link_url", "snapshot.link_url")),
            "active": _first(item, "is_active", "isActive"),
            "start": _first(item, "start_date", "startDate"),
            "end": _first(item, "end_date", "endDate"),
            "countries": _first(item, "countries"),
        }))
    return summaries


def summarize_ig_profiles(items: list[dict]) -> list[dict]:
    summaries = []
    for item in items:
        recent_posts = _first(item, "recent_posts", "recentPosts", "latestPosts") or []
        post_captions = []
        if isinstance(recent_posts, list):
            for post in recent_posts[:5]:
                caption = _truncate(_first(post, "caption", "text", "caption_text") if isinstance(post, dict) else None)
                if caption:
                    post_captions.append(caption)
        summaries.append(_compact({
            "username": _first(item, "username", "userName"),
            "full_name": _first(item, "full_name", "fullName"),
            "followers": _first(item, "followers", "followersCount", "followers_count"),
            "following": _first(item, "following", "followsCount", "following_count"),
            "posts_count": _first(item, "posts_count", "postsCount", "media_count"),
            "verified": _first(item, "is_verified", "verified"),
            "bio": _truncate(_first(item, "biography", "bio")),
            "recent_post_captions": post_captions if post_captions else None,
        }))
    return summaries


def summarize_google_ads(items: list[dict]) -> list[dict]:
    summaries = []
    for item in items:
        summaries.append(_compact({
            "advertiser": _first(item, "advertiserName", "advertiser", "brand"),
            "headline": _truncate(_first(item, "headline", "title", "adTitle")),
            "description": _truncate(_first(item, "description", "adDescription", "body")),
            "format": _first(item, "format", "adFormat", "type"),
            "regions": _first(item, "regions", "region", "targetedRegion"),
            "landing_domain": _domain(_first(item, "destinationUrl", "destination_url", "landingUrl")),
            "first_shown": _first(item, "firstShown", "first_shown", "startDate"),
            "last_shown": _first(item, "lastShown", "last_shown", "endDate"),
            "days_active": _first(item, "daysActive", "days_active"),
        }))
    return summaries


def summarize_google_search(items: list[dict]) -> list[dict]:
    summaries = []
    for page in items:
        query = _first(page, "search_term", "searchTerm", "query")
        for result in page.get("results") or []:
            url = result.get("url", "")
            if _is_cdn(url):
                continue
            summaries.append(_compact({
                "query": query,
                "pos": result.get("position"),
                "title": result.get("title"),
                "url": url or None,
                "snippet": _truncate(result.get("description")),
            }))
    return summaries
