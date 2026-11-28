"""Condition fragments that repeat a rule INSIDE the DynamoDB write.

Handlers check permissions first, to return a clean 403 or 409. These fragments
are a second, atomic check: if the task changed hands, or was submitted for
review, between the read and the write, the write is refused instead of
silently overwriting it.

Each helper adds the names/values it needs to the dicts it is given and returns
text to append to a ConditionExpression.
"""
from core.auth import Membership
from core.permissions import allows
from core.workflow import UNLOCKED_STATES


def own_guard(m: Membership, action: str, values: dict, names: dict) -> str:
    """Ownership: roles with only the "_own" permission (member) must be the
    creator or the assignee. Admins need no extra condition."""
    if allows(m.role, f"task.{action}"):
        return ""
    values[":me"] = m.caller.user_id
    values[":email"] = m.caller.email
    names["#own"] = "owner"
    return " AND (created_by = :me OR #own = :email)"


def unlocked_guard(values: dict, names: dict) -> str:
    """The task must not be submitted or approved (legacy tasks have no state)."""
    names["#as"] = "approval_state"
    values[":unl1"], values[":unl2"] = UNLOCKED_STATES
    return " AND (attribute_not_exists(#as) OR #as IN (:unl1, :unl2))"