# 搬到 `vans_mcp_server` 資料庫（#100 runbook）

> **已完成（2026-11-08）。** 這份是搬移當時的紀錄，搬移工具 `table_move.py` 已經刪除。需要重看時，到 git 歷史裡找（commit `e4c2bb1` 裡還有）。結果記在 vans_coding_router#100。正式環境遇到的問題：Neon 上的 `neondb_owner` 有 `pg_write_all_data`，所以停寫改用 trigger（見下面 `pause` 那一列）。

vans-mcp-server 的 `mcp_usage` 和 `mcp_oauth_connections` 從 router 的 `neondb` 搬到同一個 Neon 專案 VCRouter-db 底下的 `vans_mcp_server`，服務改用只能連它的 `vans_mcp_server_app`（router ADR 0069–0071、0073、0081 第 5 步）。列原樣複製：`id`、`user_id`、密文都不變，`OAUTH_TOKEN_ENCRYPTION_KEY` 不動，學生不用重新授權。

本機演練結果：[mcp-database-move-rehearsal.md](mcp-database-move-rehearsal.md)。

## 程式怎麼看設定

- 服務只讀 `VANS_MCP_DATABASE_URL`。health 的 `database_configured`、`oauth_store_configured`，以及 Google／Discord 連線頁的「需要資料庫」判斷都看它。
- router 的 `DATABASE_URL` **不再讀**，沒有 fallback。它還在環境裡時，啟動會記一筆 WARNING：`DATABASE_URL is set but not read ... Remove the DATABASE_URL Fly secret`。不會擋啟動，所以第 10 步再刪也行。
- 這份程式一上線就是切換。**在第 7 步之前不要把 `t100-mcp-database` merge 進 `main`**：push `main` 會觸發 GitHub Actions 部署。

## 搬移工具

`src/vans_mcp_server/table_move.py`，在 repo 根目錄用 `uv run python -m vans_mcp_server.table_move <command>` 執行。連線字串從環境變數讀，不印出來（只印 `user@host/db`、筆數、是否一致）：

| 變數 | 內容 |
|------|------|
| `VANS_MCP_MOVE_SOURCE_URL` | `neondb`，用兩張表的擁有者 `neondb_owner`（就是現在 Fly 上的 `DATABASE_URL`）。用 direct endpoint（host 沒有 `-pooler`） |
| `VANS_MCP_DATABASE_URL` | `vans_mcp_server`，用 `vans_mcp_server_app` |

| 指令 | 做什麼 |
|------|--------|
| `status` | 兩邊筆數、內容是否一致（md5）、`neondb` 是否停寫。不寫入 |
| `copy` | 用服務自己的 DDL 在目標建表（擁有者是 `vans_mcp_server_app`），複製來源。可重跑：`mcp_usage` 只補缺的 id，`mcp_oauth_connections` 在同一個 transaction 裡整張換成來源的列。identity 序號接在最大 id 後面。目標有來源沒有、或比來源新的列（代表服務已經切過去）就拒絕，什麼都不寫 |
| `pause` | 停寫：在兩張表掛上 trigger，拒絕所有 `INSERT`、`UPDATE`、`DELETE`、`TRUNCATE`（錯誤代碼 `insufficient_privilege`）。建 trigger 時會等進行中的寫入結束。讀照常。router 不碰這兩張表。只撤銷權限不夠：Neon 上 `neondb_owner` 屬於 `neon_superuser`，它有 `pg_write_all_data`，撤銷自己的權限後還是寫得進去（第一次正式搬移時發現） |
| `final-copy` | 必須已經 `pause`；補差額，結尾兩邊完全一致才印 `READY`，否則失敗 |
| `resume` | 拿掉 `pause` 的 trigger（切換前回復用） |

## 停寫期間學生會看到什麼

停寫從第 5 步到第 7 步新機器通過 health 為止。舊機器還連著 `neondb`：

- 能讀：已連線的 Google／Discord 工具照常，只要 Google access token 還沒過期。
- 不能寫：
  - `mcp_usage` 寫不進去，那幾筆只記在 log（`failed to write mcp_usage; falling back to log`）。這是 ERROR，每次工具呼叫各送一筆 Signal。
  - Google／Discord 連線頁存不進去，回 500。
  - access token 過期需要 refresh 時，存新 token 失敗，那次工具呼叫失敗。

所以要在沒有課的時段做。

**停寫時間**：`pause` 加 `final-copy` 在本機不到 0.5 秒，到 Neon 估計數秒。真正佔時間的是第 7 步的部署：過去 8 次 CI 部署每次 61–115 秒。整段停寫約 2–3 分鐘（用 B 方案約 1 分鐘）。

## 步驟

### 0. 前提

- [ ] 沒有課的時段，至少留 30 分鐘。
- [ ] #98 已上線：`curl https://mcp.vanscoding.com/health` 的 `auth` 是 `key_check`。
- [ ] 本機有這個 repo 的 `t100-mcp-database`，`uv sync --extra dev` 過；`flyctl auth whoami` 正常；有 `psql`（Neon SQL Editor 也可以，但第 2 步要用 psql 真的連一次）。
- [ ] 手上有 `neondb_owner` 的 direct 連線字串（Neon Console → Connect，取消 Connection pooling）。

### 1. 建立資料庫和角色（Neon，任何時間都可以，不影響服務）

用 `neondb_owner` 連 `neondb`。角色**用 SQL 建**：Console 建的角色會加入 `neon_superuser`，ADR 0073 不允許。

```sql
CREATE DATABASE vans_mcp_server;
CREATE ROLE vans_mcp_server_app LOGIN;
\password vans_mcp_server_app
GRANT vans_mcp_server_app TO neondb_owner WITH INHERIT TRUE, SET TRUE;
REVOKE CONNECT ON DATABASE vans_mcp_server FROM PUBLIC;
GRANT CONNECT ON DATABASE vans_mcp_server TO vans_mcp_server_app;
```

`\password` 只把雜湊送到伺服器，密碼不會留在 SQL 歷史。在 SQL Editor 裡做的話就照 #91 用 `CREATE ROLE vans_mcp_server_app LOGIN PASSWORD '<openssl rand -hex 24>'`。

接著**另開一個連線**，用 `neondb_owner` 連 `vans_mcp_server`（Neon 上 `\c` 不一定能換資料庫）：

```sql
GRANT USAGE, CREATE ON SCHEMA public TO vans_mcp_server_app;
```

檢查（回到 `neondb`）：

```sql
SELECT rolname, rolsuper, rolcreaterole, rolcreatedb,
       pg_has_role('vans_mcp_server_app', 'neon_superuser', 'MEMBER') AS in_neon_superuser
FROM pg_roles WHERE rolname = 'vans_mcp_server_app';
-- 預期：f, f, f, f

SELECT datname, datacl FROM pg_database WHERE datname IN ('neondb', 'vans_mcp_server');
-- 預期：兩個都沒有 "=c"（PUBLIC 的 CONNECT），vans_mcp_server 有 vans_mcp_server_app=c
```

### 2. 驗收：新角色連 `neondb` 被拒

```powershell
psql "postgresql://vans_mcp_server_app:<pw>@<host>/neondb?sslmode=require" -c "select 1"
# 預期：FATAL: permission denied for database "neondb"  DETAIL: User does not have CONNECT privilege.
psql "postgresql://vans_mcp_server_app:<pw>@<host>/vans_mcp_server?sslmode=require" -c "select current_user"
# 預期：vans_mcp_server_app
```

### 3. 設定 Fly secret（窗口開始時）

```powershell
flyctl secrets set "VANS_MCP_DATABASE_URL=postgresql://vans_mcp_server_app:<pw>@<host>/vans_mcp_server?sslmode=require" --app vans-mcp-server
```

舊程式不讀這個名字，機器重啟一次後行為不變。同時在 `~\.vans-mcp-server\fly.secrets.env` 加上 `VANS_MCP_DATABASE_URL=`。`DATABASE_URL` 先留著，舊機器在第 7 步之前還要用它。

### 4. 第一次複製（不停寫）

```powershell
$env:VANS_MCP_MOVE_SOURCE_URL = "<neondb_owner direct URL to neondb>"
$env:VANS_MCP_DATABASE_URL    = "<vans_mcp_server_app URL to vans_mcp_server>"
uv run python -m vans_mcp_server.table_move copy
```

預期兩張表 `match yes`，`neondb paused: no`。跑幾次都可以。

### 5. 停寫

```powershell
uv run python -m vans_mcp_server.table_move pause
```

預期 `neondb paused: yes`。想親手確認的話，用 `neondb_owner` 在 `neondb` 跑：

```sql
BEGIN;
INSERT INTO mcp_usage (tool_name, success, created_at) VALUES ('pause_check', true, now()::text);
-- 預期：ERROR: writes paused for the vans_mcp_server move
ROLLBACK;
```

### 6. 補差額

```powershell
uv run python -m vans_mcp_server.table_move final-copy
```

必須看到兩張表 `match yes` 和 `READY`。失敗的話**不要切換**，跑 `resume` 回到原狀，查原因。

### 7. 切換

**A（預設）**：把 `t100-mcp-database` merge 進 `main` 並 push。GitHub Actions 部署，約 1–2 分鐘。

**B（停寫較短）**：窗口前先 build 好 image：`flyctl deploy --build-only --push --image-label t100 --remote-only --app vans-mcp-server`。這一步改成 `flyctl deploy --image registry.fly.io/vans-mcp-server:t100 --app vans-mcp-server --ha=false`，事後再 merge／push `main`（CI 會用同一份程式再部署一次）。

等新機器通過 health：

```powershell
curl https://mcp.vanscoding.com/health
# 預期："database_configured": true, "oauth_store_configured": true
flyctl logs --app vans-mcp-server
# 預期有一行 WARNING "DATABASE_URL is set but not read"（第 10 步刪掉後消失）
```

### 8. 驗證既有授權

- [ ] 一位之前連過 Google 的學生（或老師的 Personal API Key）呼叫 `google_get_connect_url` 得到 `connected: true`，再跑一次 `calendar_list_events` 或 `tasks_list_tasklists` 成功。這同時驗證了密文和 `OAUTH_TOKEN_ENCRYPTION_KEY` 對得上。
- [ ] 連過 Discord 的人 `discord_list_channels` 成功。
- [ ] 新的工具呼叫寫進 `vans_mcp_server`：用 `vans_mcp_server_app` 跑 `SELECT count(*), max(created_at) FROM mcp_usage`，數字在長；`neondb` 的 `mcp_usage` 筆數不再變。
- [ ] 第 2 步再做一次：`vans_mcp_server_app` 連 `neondb` 被拒。
- [ ] `uv run python -m vans_mcp_server.table_move copy` 現在應該拒絕（`has the service already switched?`），表示目標已經有新資料。

**回復**（第 9 步之前才有）：重新部署舊 image（`flyctl releases --app vans-mcp-server --image` 找上一版，再 `flyctl deploy --image <舊 image> --ha=false`；或 revert merge 再 push），然後跑 `resume`。切換後寫進 `vans_mcp_server` 的列不會搬回去。

### 9. 從 `neondb` 刪掉兩張表

確認第 8 步都正常後，用 `neondb_owner` 連 `neondb`：

```sql
DROP TABLE mcp_usage, mcp_oauth_connections;
SELECT count(*) FROM pg_tables WHERE tablename IN ('mcp_usage', 'mcp_oauth_connections');
-- 預期：0
```

不另外 `pg_dump` 留備份：備份檔裡就有 refresh token 密文，而這次搬移就是要把它從 router 那邊拿掉。需要的話，Neon 的 point-in-time restore 就是備份。

### 10. 移除 router 的 `DATABASE_URL`

```powershell
flyctl secrets unset DATABASE_URL --app vans-mcp-server
flyctl secrets list --app vans-mcp-server   # 不再有 DATABASE_URL
```

也要從 `~\.vans-mcp-server\fly.secrets.env` 刪掉 `DATABASE_URL`，不然 `scripts\deploy-fly.ps1` 會把它設回去。機器重啟後，log 不再出現 `DATABASE_URL is set but not read`，health 仍是 `database_configured: true`。

### 11. 收尾

- [ ] 在 #100 勾選驗收項目，附上第 2、6、8 步的輸出（不要貼連線字串）。
- [ ] 下一步是 router 換成 `vans_coding_router_app`（#101，ADR 0081 第 6 步），它的驗收包含連 `vans_mcp_server` 被拒。
- [ ] 搬移完成後，在後續 commit 刪掉 `src/vans_mcp_server/table_move.py` 和 `tests/test_mcp_database_postgres.py` 裡搬移的部分。
