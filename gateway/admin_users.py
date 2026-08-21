"""Hidden-prompt local management administrator creation CLI."""

from __future__ import annotations

import argparse
import getpass
from pathlib import Path
import sys

from werkzeug.security import generate_password_hash

from gateway.auth_store import AuthStore
from gateway.management_db import open_management_db


MIN_PASSWORD_LENGTH = 12


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid arguments\n")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="python -m gateway.admin_users")
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_SafeArgumentParser,
    )
    create_admin = subparsers.add_parser("create-admin")
    create_admin.add_argument("--username", required=True)
    create_admin.add_argument(
        "--database",
        default=str(Path("data") / "management.db"),
    )
    return parser


def main(argv=None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command != "create-admin":
        return 2
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        print(
            "Password must contain at least 12 characters.",
            file=sys.stderr,
        )
        return 2
    if password != confirmation:
        print("Passwords do not match.", file=sys.stderr)
        return 2
    password_hash = generate_password_hash(password, method="scrypt")
    connection = open_management_db(Path(arguments.database))
    try:
        user = AuthStore(connection).create_user(
            arguments.username,
            password_hash,
            "administrator",
            must_change_password=False,
        )
    except ValueError as error:
        print(f"Administrator creation failed: {error}", file=sys.stderr)
        return 1
    finally:
        connection.close()
    print(f"Administrator created: {user.username}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
