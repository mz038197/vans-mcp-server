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

**Message**:
A single Gmail message in the student's mailbox, identified by message_id. Search, trash, Unread changes, and User Label changes operate on one Message or several Messages.
_Avoid_: Email, mail, thread

**Thread**:
A Gmail conversation of Messages, identified by thread_id. Summarize operates on a Thread.
_Avoid_: Conversation, chain, Message

**User Label**:
A named tag in the mailbox, identified by the exact name shown in Gmail (a slash is part of that name, not a folder), not Google's internal id. The student or the agent may create or delete it; adding a name that does not yet exist creates it. Adding or removing it on a Message does not require confirmation.
_Avoid_: Tag, folder, category, System Label, Gmail label id, parent/child label

**User Label deletion**:
Permanently destroying one User Label by name. It strips that name from every Message that had it; Messages themselves are not Trashed. It requires confirmation. A name that does not exist is not treated as already deleted.
_Avoid_: Removing a User Label from Messages, Trash, cascade, nested delete

**System Label**:
A Gmail-owned label such as INBOX, UNREAD, TRASH, SPAM, or STARRED. The portal does not expose generic System Label changes.
_Avoid_: Folder, User Label

**Unread**:
A Message the student has not marked read. Setting or clearing Unread does not require confirmation.
_Avoid_: Unseen, new

**Trash**:
Moving one or more Messages to Trash. It is not a User Label change, it requires one confirmation for the whole set, and it is not permanent deletion.
_Avoid_: Delete, remove, archive, TRASH label mutation
