"""Static/dynamic context resources for the all-about-ads MCP server."""

from src.server import mcp
from src.storage import RESULTS_DIR


@mcp.resource("ads://about")
def about() -> str:
    """Overview of the all-about-ads MCP server and its capabilities."""
    return f"""\
# all-about-ads MCP server

Scrapes the Facebook (Meta) Ads Library and Instagram profiles via Apify actors.

Scraper runs take 30s to a few minutes. Full results are saved as JSON files in
{RESULTS_DIR} and tools return a file path plus a compact summary, so the full
payload never floods the context window. Use list_saved_results /
read_saved_results to access saved data quickly.

## Tool: search_facebook_ads (actor 20nRTxLD3a3jIlZbZ)

Search ads by keyword/brand. Key parameters and accepted values:

- search_queries: list of keywords or brand names (required)
- max_results_per_query: int, default 10 (actor minimum is 10)
- enrich_with_ad_details: bool, default false (extra details, slower/costlier)
- sort_by: SORT_BY_TOTAL_IMPRESSIONS | SORT_BY_RELEVANCY_MONTHLY_GROUPED
- country: single ISO code (e.g. "US", "IN") or "ALL"; null for no filter
- content_languages: list of language codes (e.g. ["en"]) or null
- publisher_platforms: e.g. ["facebook", "instagram"] or null
- active_status: ALL | ACTIVE | INACTIVE
- ad_type: ALL | POLITICAL_AND_ISSUE_ADS | HOUSING_ADS | EMPLOYMENT_ADS | CREDIT_ADS
- media_type: ALL | IMAGE | MEME | IMAGE_AND_MEME | VIDEO | NONE
- start_date / end_date: YYYY-MM-DD or null

Returns: file_path, result_count, queries, and compact ad summaries.

## Tool: scrape_instagram_profiles (actor 98ivcMaUAxs5pu9tV)

Scrape public Instagram profile data.

- profiles: list of usernames (required), e.g. ["natgeo", "nike"]
- include_recent_posts: bool, default true

Returns: file_path, result_count, and compact profile summaries.

## Tool: list_saved_results

Lists saved result files with path, size, item count, tool, and queries.

## Tool: read_saved_results

Reads a slice of items from a saved file:

- file_path: path (or bare filename) of a saved results file
- offset / limit: pagination (default 0 / 5)
- fields: optional top-level key projection to shrink the payload
- query: optional case-insensitive substring filter across item JSON

Requires the APIFY_API_TOKEN environment variable.
"""
