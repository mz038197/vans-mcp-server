# /health stays successful when the Revocation List copy is missing or old

Fly checks `GET /health` every 15 seconds. This service still returns success there when it has no Revocation List copy, and when the copy is older than 600 seconds. The body includes how old the copy is. Student calls are refused in those cases. A failed health check would restart a process that is up and waiting on the router.

## Considered Options

- **Fail `/health` until the copy is fresh**: rejected. A router that cannot serve the list would send this service into restarts.
