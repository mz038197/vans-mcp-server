# vans-mcp-server

Vans MCP Portal server for Agent Dungeon. HTTP MCP Gateway at `mcp.vanscoding.com`.

- Milestone 1: `vcr_sk_` auth + course mock Knowledge Portal (`notion_search_pages`, `notion_read_page`)
- Milestone 3: Google Calendar Planning Portal (`google_get_connect_url`, `calendar_*`)
- Milestone 4: Gmail Communication Portal (`gmail_search_messages`, `gmail_summarize_thread`, `gmail_create_draft`, `gmail_send_email`, `gmail_trash_message`)

Google connect is **separate from** portal/dungeon Google login. One connect grants Calendar + Gmail scopes. `gmail_send_email` and `gmail_trash_message` require `confirm=true`. Filter messages with `gmail_search_messages` (Gmail query), then trash by id.

Student client package `peas-agent-mcp` is a separate project (not this repo).

## Local development

```powershell
cd C:\Users\mz038\Desktop\peas-agent\vans-mcp-server
uv sync --extra dev
$env:MCP_DEV_BYPASS_KEY = "vcr_sk_dev_local_only"
uv run vans-mcp-server
```

- Health: `http://127.0.0.1:8080/health`
- MCP: `http://127.0.0.1:8080/mcp/`（Streamable HTTP；請帶尾斜線）
- Google connect: `http://127.0.0.1:8080/connect/google/start?state=...`（由 `google_get_connect_url` 產生）

Production auth uses the same Neon `DATABASE_URL` as `vans-coding-router` (verify `api_keys`). For local without Neon, set `MCP_DEV_BYPASS_KEY` only (Google connect still needs `DATABASE_URL` + Google secrets).

## Tests

```powershell
uv run pytest
```

## Fly deploy

See [docs/deploy-fly.md](docs/deploy-fly.md).

```powershell
powershell -ExecutionPolicy Bypass -File scripts\deploy-fly.ps1
```
