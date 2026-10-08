# Vans MCP Portal

The Agent Dungeon planning and communication portal. Student agents act on a student's Google Calendar, Gmail, Tasks, and Discord through MCP tools, not through Google's raw resources.

## Language

**Revocation List**:
This service's copy of the before-expiry refusals the router publishes. The service refreshes that copy from the router every 60 seconds. An entry names one key, one student, one Class Session, or one Class. A student call uses the copy. With no copy, the service refuses student calls. A copy older than 600 seconds is not used, and the service then refuses every student call. That bound is the same during a sitting.
_Avoid_: asking the router on each student call, a push from the router, reading the router's tables, a longer bound during a sitting

**Revocation List Credential**:
The one secret this service shares with pokemon-world-mcp to fetch the Revocation List and to ask the router to check a legacy Classroom API Key. The legacy key is the request body, not the secret. It is not a Classroom API Key or a Personal API Key.
_Avoid_: a student bearer, a public read, a database connection string

**Classroom API Key**:
The per-student credential this service accepts for one Class Session. The router issues it, and this service checks that issuance itself. It expires at the expiry the router fixed when it was issued. This service also refuses it when the student is disabled, when its Class is not active or past the Class end, when a newer key for that student in that sitting ended it, or while its sitting is closed. Opening that sitting accepts it again when its own expiry has not passed. Enabling the student again, or the Class being active with its end still ahead, accepts an unexpired key again without a new redeem. A key ended because a newer one was issued stays ended. The student sees a Key Refusal. This service does not open the router's database to accept the key. A legacy key is checked by asking the router.
_Avoid_: Personal API Key, a secret resolved by reading the router's key table, opening the router's database, the sitting's current expiry, a new redeem to restore a key after the sitting is opened, one shared 無效的 API 金鑰 for every cause

**Key Refusal**:
The notice this service shows when a Classroom API Key is refused before its expiry. A key ended by a newer one is 已在其他電腦兌換. A closed Class Session is 課堂已關閉. A disabled student is 學生已被停用. A Class that is not active or past its end is 課程已結束或停用. The key's own expiry remains「API 金鑰已過期，請至 Portal 重新取得邀請碼」. When more than one cause applies, the notice is the first that still blocks a new redeem: the disabled student, then the Class, then the closed sitting, then the key ended by a newer one.
_Avoid_: 無效的 API 金鑰 for every cause, a nickname or email on the Revocation List

**Personal API Key**:
A long-lived teacher or admin key checked only by the router. This service does not accept it.
_Avoid_: Classroom API Key

**Student Connection**:
This service's record of one student's Google or Discord authorization. The rows that exist on the router's database move to a database this service owns, and a connection that already works keeps working after that move. This service does not read the router's user table to find one.
_Avoid_: a Portal Google login, a connection left on the router's database, a student re-authorizing because the rows moved

**Tool Call Record**:
A record of one tool call this service handled. Existing rows move with the Student Connections to the database this service owns.
_Avoid_: a row left on the router's database, a record the router writes

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
