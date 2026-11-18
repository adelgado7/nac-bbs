# NAC-BBS roles and board moderation

This guide covers assigning BBS operators and moderating public board posts.
Run `bbs-admin.py` from the BBS project directory on `nac-bbs-01` as the
`nac-bbs` service account. The utility edits the live SQLite database, so keep
its access limited to trusted system operators.

## Roles

| Role | BBS permissions |
| --- | --- |
| `user` | Register, read and post on active public boards, and use normal BBS commands |
| `sysop` | User permissions plus hide and restore board posts |
| `admin` | Same moderation permissions as `sysop` |
| `superadmin` | Moderation, role assignment, and board creation/archive/restore |

An account has one assigned role. Assign `sysop` to a trusted community
moderator. Reserve `superadmin` for people responsible for the whole BBS.

## Assign Darrel or Rocky over the air

As the BBS superadmin, send a direct message to NAC-BBS using each person's
exact registered display name:

```text
ADMIN ROLE GRANT Darrel's exact display name|sysop
ADMIN ROLE GRANT Rocky's exact display name|sysop
ADMIN ROLE LIST
```

The `sysop` role grants board moderation. OTA role commands can grant `sysop`
or `admin`, and can remove those roles. They cannot grant or remove
`superadmin`; use the local terminal for that higher-privilege change. The BBS
records role changes in its admin audit log. `HELP ADMIN` shows the command
summary to superadmins.

## Assign from the local terminal

Use this method if you prefer the SSH terminal. First make sure the person has
registered with the BBS, then look up the exact
display name and full public key:

```sh
python3 bbs-admin.py find Darrel
python3 bbs-admin.py user "Darrel's exact BBS display name"
```

The `user` command prints the complete public key. Copy that key exactly and
assign the moderation role:

```sh
python3 bbs-admin.py grant-role FULL_PUBLIC_KEY sysop
```

Type `GRANT` when prompted. Repeat the lookup and grant for Rocky. Use each
person's exact registered display name; do not substitute or guess a radio key.
Then confirm the assignments:

```sh
python3 bbs-admin.py roles
```

Role changes are written to the admin audit log. To remove a role while
leaving the person's BBS account intact:

```sh
python3 bbs-admin.py revoke-role FULL_PUBLIC_KEY
```

Type `REVOKE` when prompted. The system will not remove the last superadmin.

## OTA moderation commands

Assigned `sysop`, `admin`, and `superadmin` users can send these direct
messages to NAC-BBS:

```text
ADMIN MOD LIST
ADMIN MOD LIST 2
ADMIN MOD HIDE 123|Reason for hiding the post
ADMIN MOD RESTORE 123
HELP MOD
```

Post IDs appear in the moderation list. Hiding a post removes it from public
board browsing and reading, but does not erase it from the database. The
moderator, action, time, and optional reason are recorded for review. A
superadmin can restore a post if it was hidden by mistake.

Only superadmins can create, archive, or restore boards over the air with
`ADMIN BOARD`. Moderators cannot change roles or board configuration.

## Local terminal moderation

The host-side utility can also list and moderate posts:

```sh
python3 bbs-admin.py board posts
python3 bbs-admin.py board posts --page 2
python3 bbs-admin.py board hide-post 123 --reason "Off-topic"
python3 bbs-admin.py board restore-post 123
```

These commands require shell access to the BBS host. The local utility records
moderation actions in the audit log as well.

## Upgrade

On an existing installation, fetch the feature branch and run the additive
migration against the live database before restarting the service:

```sh
cd ~/meshcore-bbs
git fetch origin
git switch --track -c feature/bbs-moderation origin/feature/bbs-moderation
python3 migrations/004_board_moderation.py ~/meshcore-bbs/bbs.db
sudo systemctl restart nac-bbs.service
sudo systemctl is-active nac-bbs.service
```

The migration preserves existing posts and can be run again safely. The BBS
also checks for the moderation columns during startup.
