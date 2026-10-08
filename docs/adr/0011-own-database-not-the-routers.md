# This service keeps its own database and does not open the router's

Tool Call Records and Student Connections move off the router's database into a database this service owns. After that move, this service does not open the router's database. A Classroom API Key is checked from its signature and the Revocation List. A legacy key is sent to the router at `POST /internal/legacy-key`. A connection that already works must still work after the move, so the token encryption key stays the one that can read the moved ciphertext. Where the new database lives, whether the move pauses writes, and which student identifier the moved rows keep, are not decided here.

## Considered Options

- **Keep writing `mcp_usage` and `mcp_oauth_connections` on the router's database**: rejected. This service stops opening that database altogether.
