# Fly.io 部署（vans-mcp-server）

## 事前

- `flyctl auth login`
- Squarespace DNS：`mcp.vanscoding.com` → Fly（對齊 `ai.vanscoding.com` 做法）
- GitHub repo secret：`FLY_API_TOKEN`（push `main` 自動 deploy）

## Secrets

```powershell
Copy-Item config\fly.secrets.env.example "$HOME\.vans-mcp-server\fly.secrets.env"
notepad "$HOME\.vans-mcp-server\fly.secrets.env"
```

填入：

| Secret | 說明 |
|--------|------|
| `DATABASE_URL` | 與 `vans-coding-router` **同一** Neon connection string |
| `PUBLIC_URL` | `https://mcp.vanscoding.com` |

**不要**在 production 設定 `MCP_DEV_BYPASS_KEY`。

套用 secrets：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\deploy-fly.ps1 -SecretsOnly
```

## 部署

### 手動

```powershell
powershell -ExecutionPolicy Bypass -File scripts\deploy-fly.ps1
```

`--ha=false` 維持單機器（第一版無 volume）。

### 自動（GitHub Actions）

`git push origin main` → `.github/workflows/fly-deploy.yml` → `flyctl deploy`。

一次性設定 deploy token：

```powershell
flyctl tokens create deploy -x 999999h --app vans-mcp-server
```

GitHub → repo **Settings → Secrets and variables → Actions** → 新增 `FLY_API_TOKEN`。

CI 只 deploy 程式碼，不會覆寫 Fly secrets。

## 驗證

```powershell
curl https://vans-mcp-server.fly.dev/health
curl https://mcp.vanscoding.com/health
```

## 與 router 的關係

- App 分開：`vans-coding-router`（`ai.vanscoding.com`）與 `vans-mcp-server`（`mcp.vanscoding.com`）
- 共用 Neon：學生同一把 `vcr_sk_` 可打 LLM 與 MCP
- MCP 只讀 `api_keys` / `users`（與 session/class 過期規則），並可寫 `mcp_usage`
