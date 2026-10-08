# This service verifies a Classroom API Key through vans-auth

Signature checks, public-key fetch, Revocation List refresh, and the 600-second copy bound come from the `vans-auth` package. A legacy HMAC key is sent to the router with the Revocation List Credential. The key itself is the request body. This service does not read the router's tables for that key.

## Considered Options

- **Keep a private copy of the checks**: rejected. pokemon-world-mcp would drift from this service again.
- **Read `api_keys` for a legacy key**: rejected. The transition would keep the table coupling.
