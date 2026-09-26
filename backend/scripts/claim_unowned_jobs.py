"""
One-off: give every job that has no owner to one user.

Jobs created before sign-in existed have no `user_id`, so only admins can see
them. Run this once after your first sign-in (so your `users` doc exists):

    ./venv/bin/python backend/scripts/claim_unowned_jobs.py --email you@gmail.com
    ./venv/bin/python backend/scripts/claim_unowned_jobs.py --email you@gmail.com --apply

Dry run by default. Idempotent: it only ever touches jobs whose user_id is
missing or null, so re-running after --apply changes nothing.

Reads MONGO_URL / DB_NAME from the environment, falling back to backend/.env.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

from dotenv import dotenv_values
from pymongo import MongoClient

_BACKEND_ENV = pathlib.Path(__file__).resolve().parents[1] / ".env"


def _env(name: str) -> str:
    value = os.environ.get(name) or dotenv_values(_BACKEND_ENV).get(name)
    if not value:
        sys.exit(f"{name} is not set (environment or {_BACKEND_ENV})")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--email", required=True, help="owner to assign (must have signed in once)")
    parser.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    args = parser.parse_args()

    db = MongoClient(_env("MONGO_URL"))[_env("DB_NAME")]
    email = args.email.strip().lower()

    users = list(db.users.find({"email": email}, {"_id": 0, "user_id": 1, "email": 1}))
    if not users:
        sys.exit(f"No user with email {email}. Sign in to the app once first, then re-run.")
    if len(users) > 1:
        sys.exit(f"{len(users)} users share {email}; resolve that by hand first.")
    user = users[0]

    unowned = {"user_id": None}  # matches missing AND null
    count = db.jobs.count_documents(unowned)
    print(f"{count} unowned job(s) -> {email} (user_id {user['user_id']})")
    for job in db.jobs.find(unowned, {"_id": 0, "job_id": 1, "status": 1, "video_title": 1}).limit(5):
        print(f"  {job['job_id'][:8]}  {job['status']:18}  {(job.get('video_title') or '')[:60]}")
    if count > 5:
        print(f"  ... and {count - 5} more")

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply to assign them.")
        return

    result = db.jobs.update_many(
        unowned, {"$set": {"user_id": user["user_id"], "user_email": user["email"]}},
    )
    print(f"\nAssigned {result.modified_count} job(s).")


if __name__ == "__main__":
    main()
