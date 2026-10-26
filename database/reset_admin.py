"""
Reset (or create) an admin password from the command line — for when the
first-boot password was lost.

    python -m database.reset_admin "NewPassword123"            # user "admin"
    python -m database.reset_admin "NewPassword123" someadmin  # a specific admin
"""
import sys

from database.db import SessionLocal, init_db
from database.models import User
from helpers.jwt_utils import hash_password


def main():
    if len(sys.argv) < 2 or len(sys.argv[1]) < 8:
        sys.exit('Usage: python -m database.reset_admin "<new password, min 8 chars>" [username]')
    password = sys.argv[1]
    username = sys.argv[2] if len(sys.argv) > 2 else "admin"

    init_db()
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(username=username).first()
        if not user:
            sys.exit(f'No user "{username}" found.')
        if user.role != "admin":
            sys.exit(f'"{username}" is not an admin — use the admin panel for normal users.')
        user.password_hash = hash_password(password)
        user.is_active = True
        session.commit()
        print(f'Password reset for admin "{username}".')
    finally:
        session.close()


if __name__ == "__main__":
    main()
