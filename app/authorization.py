"""Authorization policy for governed actions.

Keeps the mapping from a tool's risk tier to the permission needed to release
it in one place, so no route or service compares role names itself.
"""

from app.identity import Permission, PermissionDenied, User
from app.tool_registry import ToolRisk

# A READ tool never reaches an approval, so it has no entry here.
RISK_PERMISSIONS: dict[str, Permission] = {
    ToolRisk.WRITE.value: Permission.APPROVE_WRITE,
    ToolRisk.DANGEROUS.value: Permission.APPROVE_DANGEROUS,
}


def permission_for_risk(risk: str) -> Permission:
    """The permission required to resolve an approval at this risk tier.

    An unrecognised risk falls back to the strictest permission rather than
    allowing the action: a new tier must be granted explicitly.
    """
    return RISK_PERMISSIONS.get(risk, Permission.APPROVE_DANGEROUS)


def ensure_can_resolve_approval(user: User, risk: str) -> None:
    """Raise PermissionDenied unless the user may decide this approval.

    Applies to both approve and reject: releasing an action and blocking one
    are the same authority.
    """
    permission = permission_for_risk(risk)

    if not user.can(permission):
        raise PermissionDenied(
            permission,
            detail=(
                f"Resolving a {risk} approval requires the {permission.value} "
                f"permission."
            ),
        )
