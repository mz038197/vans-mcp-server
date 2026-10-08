# This service accepts a Classroom API Key only

vans-mcp-server accepts a Classroom API Key, a ticket the router signs, and checks that issuance itself. It rejects a Personal API Key. A Personal API Key stays an opaque secret that only the router verifies by hash.

## Considered Options

- **Keep verifying every `api_keys` row, including a Personal API Key**: rejected. This service was reading the router's tables and applying its own copy of the validity rules. A Personal API Key has no Class Session and is not a student credential here.
