# Calendar Attendee list avoids Optional in MCP schema

Cursor and VS Code reject `anyOf: [array, null]` from `list[str] | None` in MCP client validation, so a valid `["user@example.com"]` never reaches the server. We publish `attendees` as `list[str]` with default `[]` instead of Optional. That gives up a three-state field (omit / empty / replace): create treats empty as no Attendees; update treats empty as leave unchanged; `clear_attendees=true` is the explicit empty replace. Google EventAttendee stays behind the adapter.
