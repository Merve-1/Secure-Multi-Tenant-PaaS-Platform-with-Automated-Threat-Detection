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
        "task.read", "task.write", "task.delete",
        "review",
    }),
    "member": frozenset({
        "org.read", "member.read",
        "project.read", "project.write",
        "task.read", "task.write",
    }),
    "reviewer": frozenset({
        "org.read", "member.read",
        "project.read", "task.read",
        "review",
    }),
}


def allows(role: str, permission: str) -> bool:
    return permission in PERMISSIONS.get(role, frozenset())