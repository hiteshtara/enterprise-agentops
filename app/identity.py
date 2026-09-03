"""Identity and authorization domain model.

Provider-neutral: nothing here knows about passwords, tokens, HTTP, or any
identity provider. A future Cognito/OIDC integration supplies the same `User`
value object and the rest of the system is unaffected.
"""

from dataclasses import dataclass
from enum import Enum


class Role(str, Enum):
    VIEWER = "VIEWER"
    OPERATOR = "OPERATOR"
    APPROVER = "APPROVER"
    ADMIN = "ADMIN"


class Permission(str, Enum):
    VIEW_RUNS = "VIEW_RUNS"
    VIEW_AUDIT = "VIEW_AUDIT"
    VIEW_TOOLS = "VIEW_TOOLS"
    VIEW_APPROVALS = "VIEW_APPROVALS"
    RUN_AGENT = "RUN_AGENT"
    APPROVE_WRITE = "APPROVE_WRITE"
    APPROVE_DANGEROUS = "APPROVE_DANGEROUS"
    RECONCILE_RUNS = "RECONCILE_RUNS"
    ADMINISTER = "ADMINISTER"


READ_ONLY: frozenset[Permission] = frozenset(
    {
        Permission.VIEW_RUNS,
        Permission.VIEW_AUDIT,
        Permission.VIEW_TOOLS,
        Permission.VIEW_APPROVALS,
    }
)

# The single source of truth for what each role may do. Every authorization
# decision in the system resolves through this table -- never through a
# role-name comparison at a call site.
#
# Deliberate choices:
#   - APPROVE_DANGEROUS is ADMIN-only. An APPROVER can release a WRITE but not
#     a destructive action; that separation is the point of the tier.
#   - RECONCILE_RUNS is ADMIN-only because it force-fails live runs.
#   - Every role can read. Nothing is hidden from a VIEWER; the difference
#     between roles is what they may *cause*, not what they may see.
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: READ_ONLY,
    Role.OPERATOR: READ_ONLY | {Permission.RUN_AGENT},
    Role.APPROVER: READ_ONLY | {Permission.RUN_AGENT, Permission.APPROVE_WRITE},
    Role.ADMIN: frozenset(Permission),
}


def permissions_for(role: Role) -> frozenset[Permission]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def has_permission(role: Role, permission: Permission) -> bool:
    return permission in permissions_for(role)


@dataclass(frozen=True)
class User:
    """An authenticated identity. Never carries a password or a token."""

    user_id: str
    email: str
    display_name: str
    role: Role
    active: bool
    created_at: str

    @property
    def permissions(self) -> frozenset[Permission]:
        return permissions_for(self.role)

    def can(self, permission: Permission) -> bool:
        return has_permission(self.role, permission)


class PermissionDenied(Exception):
    """Raised when an authenticated user lacks a required permission."""

    def __init__(self, permission: Permission, detail: str | None = None) -> None:
        self.permission = permission

        super().__init__(
            detail or f"This action requires the {permission.value} permission."
        )


def require_permission(user: User, permission: Permission) -> None:
    if not user.can(permission):
        raise PermissionDenied(permission)
