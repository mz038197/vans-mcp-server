# A Revocation List copy older than 600 seconds is refused

This service refuses student calls until it has a Revocation List copy. A failed refresh keeps the previous copy for 600 seconds from the last success, then the service refuses every student call. The bound is the same during a sitting.

Each failed refresh sends one ERROR Signal through the existing forwarder. A student refusal is not an ERROR.

## Considered Options

- **A longer bound during a sitting**: rejected. The refusals on the copy would stay unset on MCP for as long as the refresh stayed down.
- **Keep the last copy with no age limit**: rejected. A refresh that never recovers would leave those refusals unset for good.
- **One Signal at the first failure and another when the copy is too old**: rejected. Each failed refresh is its own Signal.
