# 本機演練：搬到 `vans_mcp_server`（#100）

2026-11-08，branch `t100-mcp-database`，Docker `postgres:18`（18.6），在 `127.0.0.1:25500` 跑完就刪掉。照 [mcp-database-move.md](mcp-database-move.md) 的順序做。

## 環境：照 Neon 的樣子搭

- `neondb_owner`：`LOGIN CREATEDB CREATEROLE`，不是 superuser，擁有 `neondb`，跟 Neon 一樣。
- `neondb`：`REVOKE CONNECT ... FROM PUBLIC`（正式環境已經撤銷）。
- 第 1 步的 SQL 用 `neondb_owner` 執行（建 `vans_mcp_server`、`CREATE ROLE vans_mcp_server_app LOGIN PASSWORD`、`GRANT ... WITH INHERIT TRUE, SET TRUE`、撤銷 PUBLIC CONNECT、`GRANT CONNECT`、`GRANT USAGE, CREATE ON SCHEMA public`）。全部成功。
  - `vans_mcp_server_app`：`rolsuper f, rolcreaterole f, rolcreatedb f`。
  - `vans_mcp_server` datacl：`{=T/neondb_owner,neondb_owner=CTc/neondb_owner,vans_mcp_server_app=c/neondb_owner}`（PUBLIC 只剩 TEMP，沒有 CONNECT）。
  - PG 18 上 `neondb_owner` 對新角色有兩筆 membership：建角色時自動給的 `ADMIN`（INHERIT/SET false），以及 `GRANT` 給的 `INHERIT, SET`。
- 舊資料：用服務自己的 `OAuthConnectionStore`／`UsageLogger` 以 `neondb_owner` 寫進 `neondb`。61 個 user_id（含開發身分 0）各一筆 Google 連線（Fernet 加密的 refresh／access token），其中每 3 個重新授權一次，讓 id 有缺號，跟正式環境的 upsert 一樣；20 筆 Discord bot；50,000 筆 `mcp_usage`（前 30,000 筆帶舊的 `api_key_id`）。結果 `mcp_oauth_connections` 81 列（max id 102），`mcp_usage` 50,000 列（6 MB）。

## 結果

| 步驟 | 結果 |
|------|------|
| `status`（搬移前） | 來源 50,000／81，目標 0／0，`paused: no` |
| `copy` | 兩張表 `match yes`，0.4 秒。目標的兩張表擁有者都是 `vans_mcp_server_app` |
| 再跑 `copy` | `mcp_usage` copied 0，仍然一致（可重跑） |
| 停寫期間舊服務還在寫 | 另一個程序每 50 ms 寫一筆 `mcp_usage`，每 10 輪 upsert 一次 user 1002 的 Google token。`pause` 之前有 62 輪寫入成功，之後 117 輪全部被拒（`InsufficientPrivilege`）。兩張表 relacl 變成 `{neondb_owner=rxtm/neondb_owner}` |
| `pause` 後 `status` | 來源 50,062，目標 50,000，兩張都 `NO`（1002 的 token 在來源更新過），`paused: yes` |
| `final-copy` | copied 62，兩張表 `match yes`，`READY`。程序內計時：`pause` 0.06 秒，`final-copy` 0.31 秒 |
| 切換後的服務 | uvicorn 用 `VANS_MCP_DATABASE_URL=vans_mcp_server` 啟動，`DATABASE_URL` 故意仍指向 `neondb`：log 有一行 WARNING `DATABASE_URL is set but not read`；health `database_configured: true, oauth_store_configured: true` |
| 既有授權 | 透過 `/mcp/` 呼叫 `google_get_connect_url`、`discord_get_connect_url`（開發身分 user 0）都是 `connected: true`。用同一把 `OAUTH_TOKEN_ENCRYPTION_KEY` 解開：61／61 refresh token、20／20 Discord bot token 都對（1002 是停寫前最後一次更新的 token） |
| 新寫入的位置 | 兩次工具呼叫寫進 `vans_mcp_server.mcp_usage`（id 接在 50,062 之後），`neondb` 沒有新增 |
| 切換後再跑 `copy` | 拒絕：`target mcp_usage has 2 rows the source does not; has the service already switched? Nothing was copied.` |
| `vans_mcp_server_app` 連 `neondb` | `FATAL: permission denied for database "neondb"  DETAIL: User does not have CONNECT privilege.` |
| `vans_mcp_server_app` 連 `vans_mcp_server` | 成功，`rolsuper f`，不是 `neondb_owner` 的成員 |
| `DROP TABLE mcp_usage, mcp_oauth_connections`（neondb） | 成功，剩 0 張；之後服務 health 正常，61／61 token 仍讀得到；server log 沒有 ERROR |

## 沒演練到的

- Neon 本身：網路延遲（估計 `final-copy` 數秒）、`neon_superuser`（本機沒有這個角色，正式環境用第 1 步的檢查 SQL 確認）。
- Fly 部署和 secrets（這次不碰正式環境）。

## 測試

`tests/test_mcp_database_postgres.py` 用同樣的 Neon 式角色，在兩個暫存資料庫上跑（8 個測試，設了 `VANS_MCP_TEST_TARGET_URL`、`VANS_MCP_TEST_SOURCE_URL` 才跑，沒設就 skip）：服務建表的擁有者、複製後 id／`user_id`／密文不變且服務讀得到、補差額不重複、切換後拒絕重跑、停寫和回復、`final-copy` 必須先停寫、CLI 不印密碼／token／密文。
