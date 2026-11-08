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
| `VANS_MCP_DATABASE_URL` | `mcp_usage` 與 `mcp_oauth_connections` 所在的 Neon：VCRouter-db 的 `vans_mcp_server`，角色 `vans_mcp_server_app`（router ADR 0071、0073）。從 `neondb` 搬過來的步驟見 [mcp-database-move.md](mcp-database-move.md) |
| `VCR_SERVICE_CREDENTIAL` | 呼叫 router Key Check 的 Service Credential（`openssl rand -hex 32`） |
| `GOOGLE_CLIENT_ID` | Google OAuth Client（可與 router 共用） |
| `GOOGLE_CLIENT_SECRET` | Google OAuth Client secret |
| `SESSION_SECRET` | Connect link state 的 HMAC secret（Google + Discord 共用） |
| `OAUTH_TOKEN_ENCRYPTION_KEY` | Fernet key（加密 Google tokens 與 Discord bot tokens） |

`PUBLIC_URL` 與 `DISCORD_GUILD_ID` 放在 `fly.toml` 的 `[env]`，不要設成 Fly secret。

`VCR_URL` 預設 `https://ai.vanscoding.com`，正式環境不用設定。

router 的 `DATABASE_URL` 不再讀取，搬移完成後從 Fly secrets 移除（還在的話啟動時記一筆 WARNING）。

**不要**在 production 設定 `MCP_DEV_BYPASS_KEY`：有 `FLY_APP_NAME`（每台 Fly 機器都有）又設定它時，服務拒絕啟動。

### Service Credential（Key Check）

金鑰由 router 的 `POST /internal/key-check` 驗證（router ADR 0079、0082）。router 那邊的產生與輪換步驟見 router 的 `guide/DEPLOYMENT.md`「Service Credential（Key Check）」。

1. `openssl rand -hex 32` 產生 credential。
2. 算出 SHA-256，加進 router 的 Fly secret `VCR_SERVICE_TOKENS`：`{"<sha256>": "vans-mcp-server", ...}`，部署 router。
3. 設定本服務的 Fly secret `VCR_SERVICE_CREDENTIAL`（原文）。

輪換：先在 `VCR_SERVICE_TOKENS` 加新雜湊並部署 router，再換本服務的 secret，最後刪掉舊雜湊。credential 設錯時 router 回 401，MCP 對所有人回「驗證服務暫時無法使用，請稍後再試」並送 ERROR Signal。

上線前通知老師：Key Check 會擋「課堂尚未開始」的學生，MCP 在開課時間前不能用。

產生 Fernet key：

```powershell
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

套用 secrets：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\deploy-fly.ps1 -SecretsOnly
```

## Google Cloud Console（Calendar + Gmail + Tasks connect）

與 Portal／Dungeon **登入**分開：這是 MCP 的 Google Portal 授權（同一組連線含 Calendar、Gmail 與 Tasks）。

1. 啟用 **Google Calendar API**、**Gmail API** 與 **Google Tasks API**
2. OAuth Consent Screen 加入 scopes：
   - `https://www.googleapis.com/auth/calendar`
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/gmail.compose`（含建草稿與寄信）
   - `https://www.googleapis.com/auth/gmail.modify`（含移到垃圾桶、標已讀／未讀、改使用者標籤；非永久刪除）
   - `https://www.googleapis.com/auth/tasks`
   - 以及 openid / email / profile
3. 同一個 OAuth Client 追加 Authorized redirect URIs：
   - `https://mcp.vanscoding.com/connect/google/callback`
   - `http://127.0.0.1:8080/connect/google/callback`（本機）
4. Testing 模式：把學生／測試帳號加進 Test users
5. 若學生先前只授權部分 scopes（例如尚未含 Tasks）：請再用 `google_get_connect_url` **重連一次**

學生流程：Agent 呼叫 `google_get_connect_url` → 瀏覽器授權 → 可用 `calendar_*`、`gmail_*` 與 `tasks_*`。  
`gmail_send_email` / `gmail_trash_message` / `gmail_delete_label` / `tasks_delete_task` 必須 `confirm=true` 才會執行。  
篩選信件：用 `gmail_search_messages`（Gmail query；結果含 `unread` 與使用者標籤名稱）取得 id，再 `gmail_mark_read` / `gmail_mark_unread` / `gmail_modify_labels`（不必 confirm；`add_labels` 沒有的名稱會新建）或 `gmail_trash_message(message_ids, confirm=true)`（一批最多 25 封）。列出 User Label：`gmail_list_labels`。毀掉一個 User Label：`gmail_delete_label(name, confirm=true)`。  
完成任務：用 `tasks_update_task` 並傳 `status=completed`。

### 已知坑：refresh token 失效但 `connected=true`

- **症狀**：`gmail_*`／`calendar_*` 報 `oauth2.googleapis.com/token` **400**；`google_get_connect_url` 卻說 already connected。
- **原因**：DB 仍有 `refresh_token_enc`，但 Google 端 refresh 已撤銷／失效；舊版在 `connected` 時把 `connect_url` 設成 `null`，無法重連。
- **正確**：`google_get_connect_url` **一律回傳** `connect_url`（已連線也可重授權覆寫 token）。若 production 尚未部署此修正，可暫時刪除 `mcp_oauth_connections` 中該 `user_id`＋`provider=google` 列後再呼叫一次。

## Discord 課堂群組（學生自有 Bot）

每位學生建立 **自己的** Discord Application + Bot（非共用一台課務 Bot）。

1. 在 `fly.toml` `[env]` 設定課堂伺服器 `DISCORD_GUILD_ID`
2. 學生：Discord Developer Portal 建立 Bot → Agent 呼叫 `discord_get_connect_url` → 瀏覽器貼上 Application ID + Bot Token（**勿貼進聊天**）→ 用成功頁的 invite 連結把 Bot 加入課堂伺服器
3. 可用 `discord_list_channels` / `discord_read_messages` / `discord_send_message`（send 需 `confirm=true`）
4. 若要讀訊息內文：在 Developer Portal 開啟 Bot 的 **Message Content Intent**
5. 結課：請學生在 Developer Portal **Reset Token**；可另清 `vans_mcp_server` 中 `provider=discord_bot` 列

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
- 學生同一把 Classroom API Key（`vcr_sk_`）可打 LLM 與 MCP；老師可用 Personal API Key
- MCP 透過 router 的 Key Check 驗證金鑰，不讀 router 的資料表；通過結果快取 60 秒，router 連不上或超過 3 秒時拒絕
- MCP 寫 `mcp_usage`（新紀錄不存 `api_key_id`）與 `mcp_oauth_connections`，兩者都以 router 的 `user_id`（學生為 `student_id`）為鍵，放在自己的 `vans_mcp_server` 資料庫；`vans_mcp_server_app` 連不上 router 的 `neondb`
- Google **登入**（router／dungeon）與 Google **Calendar connect**（本服務）刻意分開
