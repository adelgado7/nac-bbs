# NAC-BBS email and chat notifications

NAC-BBS queues email alerts for first-time registrations and new BBSMail addressed
to the registered user **Andy Delgado**. The queue is stored in the BBS SQLite
database, so a temporary network or mail-service failure does not block the
local registration or mail save. The worker retries delivery through the Pi's
existing `mail` command. It checks the notification switches again before
delivery, so alerts disabled while queued are suppressed.

The default destination is `delgado@apollos-dev.com`. Set
`BBS_NOTIFICATION_EMAIL` in the NAC-BBS service environment to use another
destination. The Pi must have its local `mail` command configured, as used by
the boot-report script.

## View or change alert settings

A superadmin can issue these commands over MeshCore:

- `ADMIN NOTIFY STATUS`
- `ADMIN NOTIFY MAIL OFF` or `ADMIN NOTIFY MAIL ON`
- `ADMIN NOTIFY REGISTRATION OFF` or `ADMIN NOTIFY REGISTRATION ON`
- `ADMIN NOTIFY CHAT OFF` or `ADMIN NOTIFY CHAT ON`

All three alerts are on by default. Settings are stored in SQLite and take
effect immediately; no service restart is needed. Chat alerts are sent as a
direct MeshCore message when a user's unread mailbox changes from empty to
nonempty. Additional mail arriving while unread mail remains does not send
another chat notice. The notice says to send `NEW`; it contains no mail
contents. If the recipient cannot be reached, the notice stays queued and is
retried.

Registration email contains the local date/time and the new BBS display name.
Mail-arrival email contains the local date/time, sender name, and mailbox item
number. Neither email nor chat notices contain the private BBSMail message text.
