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
| `GOOGLE_CLIENT_ID` | Google OAuth Client（可與 router 共用） |
| `GOOGLE_CLIENT_SECRET` | Google OAuth Client secret |
| `SESSION_SECRET` | Connect link state 的 HMAC secret（Google + Discord 共用） |
| `OAUTH_TOKEN_ENCRYPTION_KEY` | Fernet key（加密 Google tokens 與 Discord bot tokens） |

`PUBLIC_URL` 與 `DISCORD_GUILD_ID` 放在 `fly.toml` 的 `[env]`，不要設成 Fly secret。

**不要**在 production 設定 `MCP_DEV_BYPASS_KEY`。

產生 Fernet key：

```powershell
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

套用 secrets：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\deploy-fly.ps1 -SecretsOnly
```

## Google Cloud Console（Calendar + Gmail connect）

與 Portal／Dungeon **登入**分開：這是 MCP 的 Google Portal 授權（同一組連線含 Calendar 與 Gmail）。

1. 啟用 **Google Calendar API** 與 **Gmail API**
2. OAuth Consent Screen 加入 scopes：
   - `https://www.googleapis.com/auth/calendar`
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/gmail.compose`（含建草稿與寄信）
   - `https://www.googleapis.com/auth/gmail.modify`（含移到垃圾桶；非永久刪除）
   - 以及 openid / email / profile
3. 同一個 OAuth Client 追加 Authorized redirect URIs：
   - `https://mcp.vanscoding.com/connect/google/callback`
   - `http://127.0.0.1:8080/connect/google/callback`（本機）
4. Testing 模式：把學生／測試帳號加進 Test users
5. 若學生先前只授權部分 Gmail scopes：請再用 `google_get_connect_url` **重連一次**

學生流程：Agent 呼叫 `google_get_connect_url` → 瀏覽器授權 → 可用 `calendar_*` 與 `gmail_*`。  
`gmail_send_email` / `gmail_trash_message` 必須 `confirm=true` 才會執行。  
篩選信件：用 `gmail_search_messages`（Gmail query）取得 `message_id`，再 trash。

## Discord 課堂群組（學生自有 Bot）

每位學生建立 **自己的** Discord Application + Bot（非共用一台課務 Bot）。

1. 在 `fly.toml` `[env]` 設定課堂伺服器 `DISCORD_GUILD_ID`
2. 學生：Discord Developer Portal 建立 Bot → Agent 呼叫 `discord_get_connect_url` → 瀏覽器貼上 Application ID + Bot Token（**勿貼進聊天**）→ 用成功頁的 invite 連結把 Bot 加入課堂伺服器
3. 可用 `discord_list_channels` / `discord_read_messages` / `discord_send_message`（send 需 `confirm=true`）
4. 若要讀訊息內文：在 Developer Portal 開啟 Bot 的 **Message Content Intent**
5. 結課：請學生在 Developer Portal **Reset Token**；可另清 Neon 中 `provider=discord_bot` 列

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
- MCP 讀 `api_keys` / `users`，寫 `mcp_usage` 與 `mcp_oauth_connections`
- Google **登入**（router／dungeon）與 Google **Calendar connect**（本服務）刻意分開
