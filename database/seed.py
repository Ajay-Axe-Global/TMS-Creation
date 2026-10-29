"""
Idempotent bootstrap, safe to run on every boot (main.py does): creates tables,
and the first admin login if no admin exists yet.

Clients (customer + warehouse, with their TMS customer no. and trade lane) are created
by an admin in Admin > Clients — none are seeded, since a seeded client would reappear
after being renamed.
"""
import logging
import os
import secrets

from database.db import SessionLocal, init_db
from database.lookups import seed_lookups
from database.models import User
from helpers.addresses import seed_if_empty as seed_addresses
from helpers.jwt_utils import hash_password

log = logging.getLogger(__name__)

DEFAULT_ADMIN_USERNAME = "admin"


def seed():
    init_db()
    session = SessionLocal()
    try:
        seed_lookups(session)       # customer / warehouse name suggestions for Admin > Clients
        seed_addresses(session)     # the standard TMS depot addresses, once (an empty list only)
        if not session.query(User).filter_by(role="admin").first():
            password = os.getenv("ADMIN_INITIAL_PASSWORD") or secrets.token_urlsafe(12)
            session.add(User(name="Administrator", username=DEFAULT_ADMIN_USERNAME,
                             password_hash=hash_password(password), role="admin"))
            session.commit()
            # Printed (not logged to file) so the password never lands in logs/.
            print("=" * 60)
            print(f"First-boot admin created -> username: {DEFAULT_ADMIN_USERNAME}  password: {password}")
            print("Change it after logging in.")
            print("=" * 60)
    finally:
        session.close()


if __name__ == "__main__":
    seed()
