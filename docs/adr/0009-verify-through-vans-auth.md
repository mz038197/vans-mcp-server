# This service verifies a Classroom API Key through vans-auth

Signature checks, public-key fetch, Revocation List refresh, and the 600-second copy bound come from the `vans-auth` package. A legacy HMAC key is sent to the router with the Revocation List Credential. This service does not read the router's tables for that key. The router answers with the same acceptance, expiry notice, or Key Refusal a signed key would get. An acceptance includes the student's integer id and not an email, a name, or the key id. A refusal is only the notice. This service shows that answer and does not decide the cause again.

## Considered Options

- **Keep a private copy of the checks**: rejected. pokemon-world-mcp would drift from this service again.
- **Read `api_keys` for a legacy key**: rejected. The transition would keep the table coupling.
- **Answer the legacy check with only valid or invalid**: rejected. A legacy key would then hide the cause that a signed key shows.
