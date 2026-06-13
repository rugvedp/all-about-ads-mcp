# all-about-ads-mcp

An MCP (Model Context Protocol) server that scrapes the Facebook (Meta) Ads Library and Instagram profiles using [Apify](https://apify.com/) actors.

## Project layout

```
all-about-ads-mcp/
├── .venv/                  # Virtual environment (created by uv)
├── src/
│   ├── __init__.py
│   ├── server.py           # FastMCP server initialization
│   ├── tools.py            # AI tool definitions (functions)
│   ├── storage.py          # Result persistence + summarizers
│   └── resources.py        # Static/dynamic context data
├── main.py                 # Application entry point
├── pyproject.toml          # UV / Pip project metadata
└── README.md
```

## How large results are handled

Scraper runs take 30 seconds to a few minutes; the tools report progress to the MCP client while the run is in flight. Because the raw payloads are huge, full results are written to JSON files under `<system temp>/all-about-ads-mcp/` and the tools return only a **file path + compact summary**. Use `list_saved_results` and `read_saved_results` to page through the saved data without flooding the context window.

## Setup

1. Install dependencies (creates `.venv/` automatically):

```bash
uv sync
```

2. Configure your Apify API token:

```bash
cp .env.example .env
# then edit .env and set APIFY_API_TOKEN=<your token>
```

## Running

```bash
uv run main.py
```

The server runs over stdio, which is what MCP clients like Cursor expect.

## Using with Cursor

Add this to your `mcp.json` (Cursor Settings → MCP):

```json
{
  "mcpServers": {
    "all-about-ads": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/all-about-ads-mcp", "main.py"]
    }
  }
}
```

The `APIFY_API_TOKEN` is loaded from the project's `.env` file, or you can pass it via the `env` field in `mcp.json`.

## Tools

### `search_facebook_ads`

Search the Facebook Ads Library by keyword/brand (Apify actor `20nRTxLD3a3jIlZbZ`). Returns `file_path`, `result_count`, `queries`, and compact ad summaries.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `search_queries` | `list[str]` | required | Keywords or brand names to search for |
| `max_results_per_query` | `int` | `10` | Max ads returned per query (actor minimum is 10) |
| `enrich_with_ad_details` | `bool` | `false` | Fetch extra per-ad details (slower/costlier) |
| `sort_by` | `str` | `SORT_BY_TOTAL_IMPRESSIONS` | Also: `SORT_BY_RELEVANCY_MONTHLY_GROUPED` |
| `country` | `str \| null` | `null` | Single ISO country code, e.g. `"US"`, `"IN"`, or `"ALL"` |
| `content_languages` | `list[str] \| null` | `null` | Language codes, e.g. `["en"]` |
| `publisher_platforms` | `list[str] \| null` | `null` | e.g. `["facebook", "instagram"]` |
| `active_status` | `str` | `ALL` | `ALL`, `ACTIVE`, `INACTIVE` |
| `ad_type` | `str` | `ALL` | `ALL`, `POLITICAL_AND_ISSUE_ADS`, `HOUSING_ADS`, `EMPLOYMENT_ADS`, `CREDIT_ADS` |
| `media_type` | `str` | `ALL` | `ALL`, `IMAGE`, `MEME`, `IMAGE_AND_MEME`, `VIDEO`, `NONE` |
| `start_date` / `end_date` | `str \| null` | `null` | `YYYY-MM-DD` |

### `scrape_instagram_profiles`

Scrape public Instagram profile data (Apify actor `98ivcMaUAxs5pu9tV`). Returns `file_path`, `result_count`, and compact profile summaries.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `profiles` | `list[str]` | required | Instagram usernames, e.g. `["natgeo", "nike"]` |
| `include_recent_posts` | `bool` | `true` | Also fetch each profile's recent posts |

### `list_saved_results`

Lists previously saved result files with path, size, item count, and the tool/queries that produced them.

### `read_saved_results`

Reads a slice of items from a saved results file — fast access to existing data without re-running scrapers.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `file_path` | `str` | required | Path (or bare filename) of a saved results file |
| `offset` | `int` | `0` | Index of the first item to return |
| `limit` | `int` | `5` | Max items to return |
| `fields` | `list[str] \| null` | `null` | Project only these top-level keys per item |
| `query` | `str \| null` | `null` | Case-insensitive substring filter across item JSON |

## Resources

- `ads://about` — overview of the server and accepted parameter values.
