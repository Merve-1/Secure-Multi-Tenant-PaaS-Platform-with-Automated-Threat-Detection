"""The approval workflow as a small state machine, with no AWS or HTTP code.

    draft --submit--> submitted --approve--> approved --reopen--> draft
      ^                  |  |
      |   <--withdraw----+  +--request_changes--> changes_requested --submit--> submitted

Two kinds of rule live here:
  * which transitions exist (TRANSITIONS, next_state)
  * who may perform an action on a given task (may_perform, available_actions)

Tasks created before the workflow existed have no approval_state and are
treated as drafts. Handlers repeat the important rules inside the DynamoDB
write, so a race cannot slip past these checks.
"""
from dataclasses import dataclass
from enum import Enum

from core.auth import Membership
from core.permissions import allows


class ApprovalState(str, Enum):
    draft = "draft"
    submitted = "submitted"
    approved = "approved"
    changes_requested = "changes_requested"


class InvalidTransition(Exception):
    """The action is not possible from the task's current state."""


@dataclass(frozen=True)
class Transition:
    from_states: frozenset
    to_state: str
    permission: str  # the role permission the action needs


# Insertion order is the order actions are offered to the UI.
TRANSITIONS: dict[str, Transition] = {
    "submit": Transition(frozenset({"draft", "changes_requested"}), "submitted", "task.create"),
    "withdraw": Transition(frozenset({"submitted"}), "draft", "task.create"),
    "approve": Transition(frozenset({"submitted"}), "approved", "review"),
    "request_changes": Transition(frozenset({"submitted"}), "changes_requested", "review"),
    "reopen": Transition(frozenset({"approved"}), "draft", "approval.reopen"),
}

REVIEW_ACTIONS = frozenset({"approve", "request_changes"})

# States in which a task can still be edited or deleted. A tuple, because
# guards.py unpacks it into two condition values.
UNLOCKED_STATES = (ApprovalState.draft.value, ApprovalState.changes_requested.value)
LOCKED_STATES = frozenset({ApprovalState.submitted.value, ApprovalState.approved.value})


def current_state(task: dict) -> str:
    return task.get("approval_state") or ApprovalState.draft.value


def is_locked(state: str) -> bool:
    return state in LOCKED_STATES


def next_state(action: str, state: str) -> str:
    transition = TRANSITIONS.get(action)
    if transition is None or state not in transition.from_states:
        raise InvalidTransition(
            f"Cannot {action.replace('_', ' ')} a task that is {state.replace('_', ' ')}")
    return transition.to_state


def is_author(m: Membership, task: dict) -> bool:
    return task.get("created_by") == m.caller.user_id or task.get("owner") == m.caller.email


def may_perform(m: Membership, task: dict, action: str) -> bool:
    """Role permission plus the actor rule. State is checked by next_state.

    submit    anyone who can edit any task, or the task's creator or assignee
    withdraw  anyone who can edit any task, or the person who submitted it
    approve / request_changes   never the person who submitted the task
    reopen    whoever holds approval.reopen
    """
    transition = TRANSITIONS.get(action)
    if transition is None or not allows(m.role, transition.permission):
        return False
    if action == "submit":
        return allows(m.role, "task.write") or is_author(m, task)
    if action == "withdraw":
        return allows(m.role, "task.write") or task.get("submitted_by") == m.caller.user_id
    if action in REVIEW_ACTIONS:
        return task.get("submitted_by") != m.caller.user_id
    return True


def available_actions(m: Membership, task: dict) -> list[str]:
    """Actions this caller could perform on this task right now (for the UI)."""
    state = current_state(task)
    return [action for action, t in TRANSITIONS.items()
            if state in t.from_states and may_perform(m, task, action)]