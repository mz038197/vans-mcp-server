# This service keeps a copy of the Revocation List

vans-mcp-server keeps a Revocation List in memory and refreshes it from the router. A student call uses that copy and does not ask the router whether the Classroom API Key is still accepted. The router's own calls use its records.

## Considered Options

- **Ask the router on every student call**: rejected. Each tool call would wait on the router.
- **Take a push from the router**: rejected. This service would have to stay on a live channel from the router.
