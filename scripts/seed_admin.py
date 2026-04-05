#!/usr/bin/env python3
"""Create the first admin user.

Run once after migrations are applied:

    python scripts/seed_admin.py \
        --email admin@example.com \
        --display-name "Admin" \
        --password hunter2

If ``--password`` is omitted you will be prompted (input hidden). If
``SUNDAY_VOICE_ADMIN_PASSWORD`` is set, it is used as the password.

The script is idempotent: re-running with the same email is a no-op and exits 0.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

# Make ``backend`` importable when run from the repo root.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.core.security import hash_password  # noqa: E402
from app.db.session import get_sessionmaker  # noqa: E402
from app.models import ROLE_ADMIN, Role, User  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed the first admin user.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument(
        "--password",
        default=None,
        help="Admin password. Falls back to $SUNDAY_VOICE_ADMIN_PASSWORD or prompt.",
    )
    return parser.parse_args()


def resolve_password(cli_value: str | None) -> str:
    if cli_value:
        return cli_value
    env_value = os.environ.get("SUNDAY_VOICE_ADMIN_PASSWORD")
    if env_value:
        return env_value
    return getpass.getpass("Admin password: ")


async def seed_admin(email: str, display_name: str, password: str) -> int:
    maker = get_sessionmaker()
    async with maker() as session:
        async with session.begin():
            existing = (
                await session.execute(select(User).where(User.email == email))
            ).scalar_one_or_none()
            if existing is not None:
                print(f"User {email!r} already exists (id={existing.id}); nothing to do.")
                return 0

            admin_role = (
                await session.execute(select(Role).where(Role.name == ROLE_ADMIN))
            ).scalar_one_or_none()
            if admin_role is None:
                print(
                    "Admin role is missing. Run `alembic upgrade head` first.",
                    file=sys.stderr,
                )
                return 1

            user = User(
                email=email,
                display_name=display_name,
                hashed_password=hash_password(password),
                role_id=admin_role.id,
                is_active=True,
            )
            session.add(user)
        print(f"Created admin user {email!r}.")
        return 0


def main() -> int:
    args = parse_args()
    password = resolve_password(args.password)
    if not password:
        print("Password is required.", file=sys.stderr)
        return 2
    return asyncio.run(seed_admin(args.email, args.display_name, password))


if __name__ == "__main__":
    raise SystemExit(main())
