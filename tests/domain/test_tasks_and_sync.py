import pytest

from ohmymeme.core.domain import (
    InvalidTaskTransitionError,
    MemeFilename,
    Sha256,
    SyncEntry,
    SyncPlan,
    SyncOperation,
    Task,
    TaskId,
    TaskKind,
    TaskState,
    UnsupportedSyncPlanError,
    UnsupportedTaskStateError,
)


def _entry():
    return SyncEntry(MemeFilename("one.png"), Sha256("a" * 64))


def test_task_state_rejects_unknown_values_deterministically():
    # Given: an unrecognized persisted task state
    # When: the state parser receives it
    # Then: callers receive a typed stable error instead of a fallback state
    with pytest.raises(UnsupportedTaskStateError) as captured:
        TaskState.parse("paused")

    assert captured.value.state == "paused"
    assert str(captured.value) == "unsupported_task_state:paused"


def test_task_transition_accepts_only_declared_states():
    # Given: a pending sync task
    # When: its lifecycle advances through the declared transition
    # Then: the resulting task is immutable and terminal tasks cannot restart
    pending = Task(TaskId("sync-1"), TaskKind.SYNC_PUSH, TaskState.PENDING)
    completed = pending.transition(TaskState.RUNNING).transition(TaskState.COMPLETED)

    assert pending.state is TaskState.PENDING
    assert completed.state is TaskState.COMPLETED
    with pytest.raises(InvalidTaskTransitionError):
        completed.transition(TaskState.RUNNING)


def test_sync_plan_rejects_unknown_operations_and_duplicate_entries():
    # Given: a valid immutable sync entry
    # When: callers request an unsupported plan or duplicate the entry
    # Then: invalid sync work cannot be scheduled
    entry = _entry()
    entries = [entry]
    plan = SyncPlan.from_operation("push", entries)
    entries.clear()

    assert plan.operation is SyncOperation.PUSH
    assert plan.entries == (entry,)
    with pytest.raises(UnsupportedSyncPlanError) as captured:
        SyncPlan.from_operation("archive", ())
    assert captured.value.operation == "archive"
    with pytest.raises(UnsupportedSyncPlanError):
        SyncPlan(SyncOperation.PUSH, (entry, entry))


@pytest.mark.parametrize(
    ("operation", "entries", "reason"),
    (
        (SyncOperation.PUSH, (), "entries_required"),
        (SyncOperation.PULL, (), "entries_required"),
        (SyncOperation.CLEANUP, (), "entries_required"),
        (SyncOperation.DELETE_ALL, (_entry(),), "entries_not_allowed"),
        (SyncOperation.UPLOAD_INDEX, (_entry(),), "entries_not_allowed"),
    ),
)
def test_sync_plan_rejects_each_operation_entry_semantic_mismatch(
    operation, entries, reason
):
    # Given: an operation with an entry cardinality that contradicts its meaning
    # When: the SyncPlan parses the requested work
    # Then: no semantically invalid plan can be scheduled
    with pytest.raises(UnsupportedSyncPlanError) as captured:
        SyncPlan(operation, entries)

    assert captured.value.operation == operation.value
    assert captured.value.reason == reason
    assert str(captured.value) == f"unsupported_sync_plan:{operation.value}:{reason}"


@pytest.mark.parametrize(
    ("operation", "entries"),
    (
        (SyncOperation.PUSH, (_entry(),)),
        (SyncOperation.PULL, (_entry(),)),
        (SyncOperation.CLEANUP, (_entry(),)),
        (SyncOperation.DELETE_ALL, ()),
        (SyncOperation.UPLOAD_INDEX, ()),
    ),
)
def test_sync_plan_accepts_each_declared_operation_semantics(operation, entries):
    # Given: one valid entry cardinality for every supported operation
    # When: each plan is constructed
    # Then: all declared operation variants remain usable
    plan = SyncPlan(operation, entries)

    assert plan.operation is operation
    assert plan.entries == entries
