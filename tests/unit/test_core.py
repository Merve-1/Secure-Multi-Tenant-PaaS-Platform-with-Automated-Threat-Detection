"""Unit tests that need no AWS access.

Run from the repository root:
    pip install pytest boto3
    pytest
(pytest.ini sets pythonpath = application so `import core` works.)
"""
from core.db import build_update, task_key, task_prefix, tpk
from core.permissions import PERMISSIONS, Role, allows


def test_exactly_three_roles():
    assert {r.value for r in Role} == set(PERMISSIONS) == {"admin", "member", "reviewer"}


def test_admin_can_do_everything_listed():
    every_permission = set().union(*PERMISSIONS.values())
    for permission in every_permission:
        assert allows("admin", permission)


def test_member_works_on_own_tasks_but_not_others_or_management():
    for permission in ("project.read", "project.write", "task.read", "task.create",
                       "task.write_own", "task.delete_own"):
        assert allows("member", permission)
    for permission in ("project.delete", "task.write", "task.delete", "member.manage",
                       "org.update", "org.delete", "review"):
        assert not allows("member", permission)


def test_reviewer_reads_and_reviews_but_never_writes():
    for permission in ("org.read", "project.read", "task.read", "review"):
        assert allows("reviewer", permission)
    for permission in ("project.write", "project.delete", "task.create", "task.write",
                       "task.delete", "task.write_own", "task.delete_own",
                       "member.manage", "org.update"):
        assert not allows("reviewer", permission)


def test_unknown_role_or_permission_is_denied():
    assert not allows("owner", "org.read")
    assert not allows("", "org.read")
    assert not allows("admin", "billing.refund")


def test_build_update_aliases_names_and_removes_nulls():
    expression, names, values = build_update({"name": "x", "due_date": None})
    assert expression == "SET #f0 = :v0 REMOVE #f1"
    assert names == {"#f0": "name", "#f1": "due_date"}
    assert values == {":v0": "x"}


def test_keys_always_start_with_the_tenant():
    assert tpk("org-1") == "TENANT#org-1"
    key = task_key("org-1", "p1", "t1")
    assert key["PK"] == "TENANT#org-1"
    assert key["SK"] == "TASK#p1#t1"
    assert key["SK"].startswith(task_prefix("p1"))
    # A task of another project never matches this project's prefix.
    assert not task_key("org-1", "p2", "t1")["SK"].startswith(task_prefix("p1"))