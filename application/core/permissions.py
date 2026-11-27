"""Roles and what each role may do. This is the only place permissions are defined.

Anything not listed is denied. The role of a user in an organization is stored
in DynamoDB (the membership item), never taken from the request or the token.
"""
from enum import Enum


class Role(str, Enum):
    admin = "admin"
    member = "member"
    reviewer = "reviewer"


PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": frozenset({
        "org.read", "org.update", "org.delete",
        "member.read", "member.manage",
        "project.read", "project.write", "project.delete",
        "task.read", "task.create", "task.write", "task.delete",
        "review", "approval.reopen",
    }),
    "member": frozenset({
        "org.read", "member.read",
        "project.read", "project.write",
        # Tasks: members work on their own tasks (created by them or assigned to
        # them). "task.write" / "task.delete" without _own would mean ANY task.
        "task.read", "task.create", "task.write_own", "task.delete_own",
    }),
    "reviewer": frozenset({
        "org.read", "member.read",
        "project.read", "task.read",
        "review",
    }),
}


def allows(role: str, permission: str) -> bool:
    return permission in PERMISSIONS.get(role, frozenset())