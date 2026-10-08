# The dev bypass key is local only

`MCP_DEV_BYPASS_KEY` remains a local shortcut. It is not a signed Classroom API Key, and it is not checked against the Revocation List. An environment that fetches the Revocation List does not accept it.

## Considered Options

- **Remove the bypass and require a router-issued key locally**: rejected. Local work should not need a router.
- **Accept the bypass wherever it is set**: rejected. A production environment would then have a key that skips the Revocation List.
