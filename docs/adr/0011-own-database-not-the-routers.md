# This service keeps its own database and does not open the router's

Tool Call Records and Student Connections move off the router's database into a database this service owns. After that move, this service does not open the router's database. A Classroom API Key is checked from its signature and the Revocation List. A legacy key is sent to the router at `POST /internal/legacy-key`. A connection that already works must still work after the move, so the token encryption key stays the one that can read the moved ciphertext. The new database is in the Neon project VCRouter-db, beside `neondb` and `vans_signals`, and this service cannot read `neondb`. The move copies the tables, pauses writes, copies the gap, then `DATABASE_URL` points only there. Moved rows keep `users.id`. A tool-call row that already exists keeps its `api_keys` id as history. A new row for a signed key stores that key's own identity. A new row for a legacy key stores no key identity.

## Considered Options

- **Keep writing `mcp_usage` and `mcp_oauth_connections` on the router's database**: rejected. This service stops opening that database altogether.
