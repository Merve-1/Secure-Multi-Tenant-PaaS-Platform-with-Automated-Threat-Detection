"""Approval workflow tests. They need no AWS access."""
import pytest

from core.auth import Caller, Membership
from core.permissions import allows
from core.workflow import (LOCKED_STATES, TRANSITIONS, InvalidTransition, available_actions,
                           current_state, is_locked, may_perform, next_state)

STATES = ["draft", "submitted", "approved", "changes_requested"]


# ------------------------------------------------------------ state machine
def test_legal_transitions():
    assert next_state("submit", "draft") == "submitted"
    assert next_state("submit", "changes_requested") == "submitted"
    assert next_state("approve", "submitted") == "approved"
    assert next_state("request_changes", "submitted") == "changes_requested"
    assert next_state("withdraw", "submitted") == "draft"
    assert next_state("reopen", "approved") == "draft"


def test_every_other_combination_is_rejected():
    legal = {(name, state) for name, t in TRANSITIONS.items() for state in t.from_states}
    for action in TRANSITIONS:
        for state in STATES:
            if (action, state) not in legal:
                with pytest.raises(InvalidTransition):
                    next_state(action, state)


def test_unknown_action_is_rejected():
    with pytest.raises(InvalidTransition):
        next_state("teleport", "draft")


def test_nothing_reaches_approved_without_being_submitted():
    reaches_approved = {state for t in TRANSITIONS.values() if t.to_state == "approved"
                        for state in t.from_states}
    assert reaches_approved == {"submitted"}


def test_decisions_only_happen_on_submitted_tasks():
    assert TRANSITIONS["approve"].from_states == {"submitted"}
    assert TRANSITIONS["request_changes"].from_states == {"submitted"}


def test_legacy_tasks_without_a_state_are_drafts():
    assert current_state({"id": "t"}) == "draft"
    assert current_state({"id": "t", "approval_state": None}) == "draft"
    assert current_state({"id": "t", "approval_state": "approved"}) == "approved"


def test_submitted_and_approved_tasks_are_locked():
    assert LOCKED_STATES == {"submitted", "approved"}
    assert is_locked("submitted") and is_locked("approved")
    assert not is_locked("draft") and not is_locked("changes_requested")


def test_only_admins_may_reopen():
    assert allows("admin", "approval.reopen")
    assert not allows("member", "approval.reopen")
    assert not allows("reviewer", "approval.reopen")


# ------------------------------------------------------ who may do what
def membership(role, user_id="u-me", email="me@x.com"):
    return Membership("org-1", Caller(user_id, email), role)


def task(state="draft", **extra):
    base = {"id": "t1", "created_by": "u-author", "owner": "author@x.com",
            "approval_state": state}
    return {**base, **extra}


AUTHOR = dict(user_id="u-author", email="author@x.com")


def test_author_can_submit_a_draft_and_a_rejected_task():
    assert available_actions(membership("member", **AUTHOR), task("draft")) == ["submit"]
    assert available_actions(membership("member", **AUTHOR), task("changes_requested")) == ["submit"]


def test_member_cannot_submit_someone_elses_task():
    other = membership("member", user_id="u-other", email="other@x.com")
    assert available_actions(other, task("draft")) == []


def test_assignee_can_submit():
    assignee = membership("member", user_id="u-assignee", email="author@x.com")
    assert available_actions(assignee, task("draft")) == ["submit"]


def test_reviewer_cannot_submit():
    assert available_actions(membership("reviewer"), task("draft")) == []


def test_reviewer_can_decide_on_a_submitted_task():
    submitted = task("submitted", submitted_by="u-author")
    assert available_actions(membership("reviewer"), submitted) == ["approve", "request_changes"]


def test_reviewer_has_no_actions_on_other_states():
    for state in ("draft", "approved", "changes_requested"):
        assert available_actions(membership("reviewer"), task(state)) == []


def test_member_cannot_decide():
    submitted = task("submitted", submitted_by="u-author")
    assert not may_perform(membership("member"), submitted, "approve")
    assert not may_perform(membership("member"), submitted, "request_changes")


def test_nobody_reviews_their_own_submission():
    submitted = task("submitted", submitted_by="u-admin")
    admin = membership("admin", user_id="u-admin", email="admin@x.com")
    assert not may_perform(admin, submitted, "approve")
    assert not may_perform(admin, submitted, "request_changes")
    assert available_actions(admin, submitted) == ["withdraw"]


def test_admin_can_decide_on_someone_elses_submission():
    submitted = task("submitted", submitted_by="u-author")
    admin = membership("admin", user_id="u-admin", email="admin@x.com")
    assert available_actions(admin, submitted) == ["withdraw", "approve", "request_changes"]


def test_only_the_submitter_or_an_admin_can_withdraw():
    submitted = task("submitted", submitted_by="u-author")
    assert may_perform(membership("member", **AUTHOR), submitted, "withdraw")
    other = membership("member", user_id="u-other", email="other@x.com")
    assert not may_perform(other, submitted, "withdraw")
    assert not may_perform(membership("reviewer"), submitted, "withdraw")


def test_approved_tasks_can_only_be_reopened_by_an_admin():
    approved = task("approved", submitted_by="u-author")
    assert available_actions(membership("admin"), approved) == ["reopen"]
    assert available_actions(membership("member", **AUTHOR), approved) == []
    assert available_actions(membership("reviewer"), approved) == []