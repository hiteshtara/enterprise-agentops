"""Persistence for local identities.

The only component that touches password hashes. Nothing it returns to the rest
of the application carries a hash: authentication happens here and callers
receive a `User` value object.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.database import Database, get_database
from app.db_models import UserRecord
from app.identity import Role, User
from app.security import hash_password, verify_password


def to_user(record: UserRecord) -> User:
    return User(
        user_id=record.user_id,
        email=record.email,
        display_name=record.display_name,
        role=Role(record.role),
        active=record.active,
        created_at=record.created_at,
    )


def user_to_dict(user: User) -> dict[str, Any]:
    """API-safe projection. There is no branch here that can emit a hash."""
    return {
        "user_id": user.user_id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role.value,
        "active": user.active,
        "created_at": user.created_at,
        "permissions": sorted(p.value for p in user.permissions),
    }


class UserStore:
    def __init__(
        self,
        database: Database | None = None,
    ) -> None:
        self._database = database or get_database()

    def create(
        self,
        email: str,
        display_name: str,
        password: str,
        role: Role,
        active: bool = True,
    ) -> User:
        record = UserRecord(
            user_id=str(uuid4()),
            email=email.strip().lower(),
            display_name=display_name,
            password_hash=hash_password(password),
            role=role.value,
            active=active,
            created_at=datetime.now(UTC).isoformat(),
        )

        with self._database.session() as session:
            session.add(record)
            session.commit()
            session.refresh(record)

            return to_user(record)

    def get(self, user_id: str) -> User | None:
        with self._database.session() as session:
            record = session.get(UserRecord, user_id)

            return None if record is None else to_user(record)

    def get_by_email(self, email: str) -> User | None:
        with self._database.session() as session:
            record = session.scalar(
                select(UserRecord).where(UserRecord.email == email.strip().lower())
            )

            return None if record is None else to_user(record)

    def list_users(self) -> list[User]:
        with self._database.session() as session:
            statement = select(UserRecord).order_by(UserRecord.email)

            return [to_user(record) for record in session.scalars(statement)]

    def authenticate(self, email: str, password: str) -> User | None:
        """Return the user when the password matches and the account is active.

        A wrong password, an unknown email, and a deactivated account are all
        reported the same way -- as None -- so the caller cannot use the result
        to enumerate accounts.
        """
        with self._database.session() as session:
            record = session.scalar(
                select(UserRecord).where(UserRecord.email == email.strip().lower())
            )

            if record is None:
                # Still hash-compare against a dummy value so a missing account
                # does not answer measurably faster than a wrong password.
                verify_password(password, "$2b$12$" + "." * 53)

                return None

            if not verify_password(password, record.password_hash):
                return None

            if not record.active:
                return None

            return to_user(record)
