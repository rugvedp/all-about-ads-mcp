#!/usr/bin/env bash
# One-command local install for the all-about-ads-mcp server.
# Usage: bash install.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_NAME="all-about-ads"

# ── colours ────────────────────────────────────────────────────────────────────
G='\033[0;32m'; Y='\033[1;33m'; C='\033[0;36m'; NC='\033[0m'
step() { echo -e "\n${C}▶  $1${NC}"; }
ok()   { echo -e "${G}✓  $1${NC}"; }
warn() { echo -e "${Y}!  $1${NC}"; }
sep()  { echo "────────────────────────────────────────────────────────────"; }

# ── 1. uv ─────────────────────────────────────────────────────────────────────
step "Checking for uv..."
if ! command -v uv &>/dev/null; then
    warn "uv not found — installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
ok "uv $(uv --version)"

# ── 2. dependencies ───────────────────────────────────────────────────────────
step "Installing Python dependencies..."
uv sync --quiet
ok "Dependencies ready"

# ── 3. .env ───────────────────────────────────────────────────────────────────
step "Configuring environment..."

if [ ! -f "$REPO_DIR/.env" ]; then
    cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
    warn "Created .env from .env.example"
fi

# Prompt for token if not already set
CURRENT_TOKEN=$(grep -E '^APIFY_API_TOKEN=' "$REPO_DIR/.env" 2>/dev/null \
                | cut -d= -f2 | tr -d '"' | tr -d "'" | xargs)

if [ -z "$CURRENT_TOKEN" ]; then
    echo ""
    echo "  Get a free Apify token at: https://console.apify.com/account/integrations"
    read -rp "  Enter your APIFY_API_TOKEN (or press Enter to skip): " TOKEN
    if [ -n "$TOKEN" ]; then
        # Replace the empty APIFY_API_TOKEN= line
        if [[ "$OSTYPE" == "darwin"* ]]; then
            sed -i '' "s|^APIFY_API_TOKEN=.*|APIFY_API_TOKEN=${TOKEN}|" "$REPO_DIR/.env"
        else
            sed -i "s|^APIFY_API_TOKEN=.*|APIFY_API_TOKEN=${TOKEN}|" "$REPO_DIR/.env"
        fi
        CURRENT_TOKEN="$TOKEN"
        ok "APIFY_API_TOKEN saved to .env"
    else
        warn "Skipped — edit $REPO_DIR/.env manually before using the server"
    fi
else
    ok "APIFY_API_TOKEN already configured"
fi

TOKEN_VALUE="${CURRENT_TOKEN:-your_apify_token_here}"

# ── 4. generate the JSON block ─────────────────────────────────────────────────
JSON_BLOCK=$(cat <<JSONBLOCK
{
  "mcpServers": {
    "$SERVER_NAME": {
      "command": "uv",
      "args": ["run", "--directory", "$REPO_DIR", "main.py"],
      "env": {
        "APIFY_API_TOKEN": "$TOKEN_VALUE"
      }
    }
  }
}
JSONBLOCK
)

# VS Code / Copilot uses a slightly different schema key
VSCODE_BLOCK=$(cat <<JSONBLOCK
{
  "servers": {
    "$SERVER_NAME": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "$REPO_DIR", "main.py"],
      "env": {
        "APIFY_API_TOKEN": "$TOKEN_VALUE"
      }
    }
  }
}
JSONBLOCK
)

# ── 5. print configs ──────────────────────────────────────────────────────────
echo ""
sep
echo "  Client configs — copy the block for your editor"
sep

echo -e "\n${C}Claude Code CLI${NC} (run this once):\n"
echo "  claude mcp add $SERVER_NAME \\"
echo "    uv run --directory \"$REPO_DIR\" main.py \\"
echo "    --env APIFY_API_TOKEN=\"$TOKEN_VALUE\""

echo -e "\n${C}Claude Desktop${NC}  ~/Library/Application Support/Claude/claude_desktop_config.json\n"
echo "$JSON_BLOCK"

echo -e "\n${C}Cursor${NC}  ~/.cursor/mcp.json  (or .cursor/mcp.json in the project root)\n"
echo "$JSON_BLOCK"

echo -e "\n${C}Windsurf${NC}  ~/.codeium/windsurf/mcp_config.json\n"
echo "$JSON_BLOCK"

echo -e "\n${C}VS Code / GitHub Copilot${NC}  .vscode/mcp.json in the workspace root\n"
echo "$VSCODE_BLOCK"

# ── 6. auto-patch Claude Desktop (optional) ───────────────────────────────────
CLAUDE_CONFIG_DIR=""
if [[ "$OSTYPE" == "darwin"* ]]; then
    CLAUDE_CONFIG_DIR="$HOME/Library/Application Support/Claude"
elif [[ -n "${APPDATA:-}" ]]; then
    CLAUDE_CONFIG_DIR="$APPDATA/Claude"
fi

if [ -n "$CLAUDE_CONFIG_DIR" ] && [ -d "$CLAUDE_CONFIG_DIR" ]; then
    CLAUDE_CONFIG="$CLAUDE_CONFIG_DIR/claude_desktop_config.json"
    echo ""
    sep
    echo -e "  ${C}Claude Desktop detected${NC} at: $CLAUDE_CONFIG"
    sep
    read -rp "  Auto-add to Claude Desktop config? [y/N] " PATCH_CLAUDE
    if [[ "$PATCH_CLAUDE" =~ ^[Yy]$ ]]; then
        if [ -f "$CLAUDE_CONFIG" ]; then
            cp "$CLAUDE_CONFIG" "${CLAUDE_CONFIG}.bak"
            ok "Backed up existing config → ${CLAUDE_CONFIG}.bak"
        fi
        uv run python3 - <<PYTHON
import json, sys

config_path = "$CLAUDE_CONFIG"
server_name = "$SERVER_NAME"
repo_dir    = "$REPO_DIR"
token       = "$TOKEN_VALUE"

try:
    with open(config_path) as f:
        config = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    config = {}

config.setdefault("mcpServers", {})[server_name] = {
    "command": "uv",
    "args": ["run", "--directory", repo_dir, "main.py"],
    "env": {"APIFY_API_TOKEN": token},
}

with open(config_path, "w") as f:
    json.dump(config, f, indent=2)
    f.write("\n")

print(f"  Added '{server_name}' to {config_path}")
PYTHON
        warn "Restart Claude Desktop to pick up the new server."
    fi
fi

# ── done ──────────────────────────────────────────────────────────────────────
echo ""
sep
ok "All done. Server is at: $REPO_DIR"
echo "   Free Apify token: https://console.apify.com/account/integrations"
sep
echo ""
