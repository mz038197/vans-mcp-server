# vans-mcp-server

Vans MCP Portal server for Agent Dungeon. HTTP MCP Gateway at `mcp.vanscoding.com`.

Milestone 1: `vcr_sk_` auth + course mock Knowledge Portal (`notion_search_pages`, `notion_read_page`).

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

Production auth uses the same Neon `DATABASE_URL` as `vans-coding-router` (verify `api_keys`). For local without Neon, set `MCP_DEV_BYPASS_KEY` only.

## Tests

```powershell
uv run pytest
```

## Fly deploy

See [docs/deploy-fly.md](docs/deploy-fly.md).

```powershell
powershell -ExecutionPolicy Bypass -File scripts\deploy-fly.ps1
```
