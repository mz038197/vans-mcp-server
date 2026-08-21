# Vans MCP Portal

The Agent Dungeon planning and communication portal. Student agents act on a student's Google Calendar, Gmail, Tasks, and Discord through MCP tools, not through Google's raw resources.

## Language

**Calendar Event**:
An event on the student's primary Google Calendar.
_Avoid_: Appointment, meeting, Google Event resource

**Attendee**:
A person invited onto a Calendar Event, identified by email.
_Avoid_: Guest, Invitee, 邀請人, Google EventAttendee

**Attendee list**:
The complete set of Attendees on a Calendar Event. Setting it replaces the previous set; it is not a list of people to add.
_Avoid_: Invitation (the email Google sends), delta, guest list
