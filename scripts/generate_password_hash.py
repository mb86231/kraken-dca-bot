#!/usr/bin/env python3
"""Generate a bcrypt password hash for the web dashboard admin user.

Usage:
    python scripts/generate_password_hash.py
    python scripts/generate_password_hash.py "your-secure-password"
"""

import getpass
import sys

import bcrypt


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args:
        password = args[0]
    else:
        password = getpass.getpass("Enter password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords do not match.", file=sys.stderr)
            return 1

    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    # stdout carries only the hash so the output can be captured
    # (e.g. HASH=$(docker run ... generate_password_hash.py)); the hint goes
    # to stderr.
    print("Set this value as WEB_UI_PASSWORD_HASH in your environment:", file=sys.stderr)
    print(hashed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
