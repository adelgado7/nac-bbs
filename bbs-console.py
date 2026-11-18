"""Run the NAC-BBS command handler in the service owner's terminal."""

import argparse
from contextlib import closing
import os
from pathlib import Path
import pwd
import re
import sqlite3
import stat

import bbs


def read_allowed_keys():
    path = Path.home() / ".config" / "nac-bbs" / "console-identities"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding="utf-8") as config:
        metadata = os.fstat(config.fileno())
        if (not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077):
            raise ValueError(f"{path} must be a private file owned by nac-bbs (mode 600).")
        return set(config.read().split())


def terminal_text(text):
    # Keep user content from issuing control sequences in the SSH terminal.
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", required=True, metavar="NAME",
                        help="registered BBS display name")
    args = parser.parse_args()

    if pwd.getpwuid(os.getuid()).pw_name != "nac-bbs" or os.getuid() != os.geteuid():
        parser.exit(1, "Run this console as the nac-bbs service account.\n")

    try:
        allowed_keys = read_allowed_keys()
        if not bbs.DB_FILE.is_file():
            raise ValueError(f"BBS database is missing at {bbs.DB_FILE}.")
        identity = bbs.get_user_by_name(args.identity)
        if identity is None or identity["public_key"] not in allowed_keys:
            raise ValueError("Select a registered identity whose full public key is in console-identities.")

        public_key = identity["public_key"]
        with closing(bbs.get_db()) as conn:
            superadmin = bbs._is_superadmin(public_key, conn)
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"Cannot open console: {terminal_text(str(error))}\n")

    display_name = terminal_text(identity["display_name"])
    role = " (superadmin)" if superadmin else ""
    print(f"NAC-BBS local console as {display_name}{role}. Commands use the live BBS database.")
    print("Type HELP for BBS commands or EXIT to leave.")
    while True:
        command = input("NAC-BBS> ").strip()
        if command.upper() in {"EXIT", "QUIT"}:
            break
        if command:
            try:
                reply = bbs.process_command(command, public_key, identity["advertised_name"])
            except sqlite3.Error as error:
                print(f"Database error: {terminal_text(str(error))}")
            else:
                print(terminal_text(reply))


if __name__ == "__main__":
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        print()
