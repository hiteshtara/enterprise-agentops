"""Explicit, idempotent demo identities.

DEMO ONLY. These passwords are published in the repository and the README. They
exist so the console can be explored locally; they are not credentials.

Nothing here runs on import.
"""

from app.database import Database, get_database
from app.identity import Role
from app.user_store import UserStore

# (email, display name, password, role)
DEMO_USERS: tuple[tuple[str, str, str, Role], ...] = (
    ("viewer@agentguard.local", "Val Viewer", "viewer-demo-password", Role.VIEWER),
    (
        "operator@agentguard.local",
        "Ola Operator",
        "operator-demo-password",
        Role.OPERATOR,
    ),
    (
        "approver@agentguard.local",
        "Ada Approver",
        "approver-demo-password",
        Role.APPROVER,
    ),
    ("admin@agentguard.local", "Avi Admin", "admin-demo-password", Role.ADMIN),
)


def seed_demo_users(
    database: Database | None = None,
) -> int:
    """Insert any missing demo users. Returns how many were created.

    Existing users are never updated, so a changed local password is preserved
    and re-running is safe.
    """
    target = database or get_database()

    store = UserStore(database=target)

    created = 0

    for email, display_name, password, role in DEMO_USERS:
        if store.get_by_email(email) is not None:
            continue

        store.create(
            email=email,
            display_name=display_name,
            password=password,
            role=role,
        )

        created += 1

    return created
