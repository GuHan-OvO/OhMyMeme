"""Typed errors for Container-owned library-state mutation leases."""


class RemoteMutationBusyError(RuntimeError):
    """Another mutation or container owns the library-state resource."""

    def __init__(self, group: str = "library-state") -> None:
        self.group = group
        super().__init__(group)

    def __str__(self) -> str:
        return f"remote_mutation_busy:{self.group}"


class RemoteMutationRootLockedError(RemoteMutationBusyError):
    """A second Container attempted to claim the same data root."""

    def __str__(self) -> str:
        return f"remote_mutation_root_locked:{self.group}"


class RemoteMutationReentrantError(RuntimeError):
    """The current mutation owner attempted to acquire the lease again."""

    def __str__(self) -> str:
        return "remote_mutation_reentrant:library-state"


class RemoteMutationConflictError(RuntimeError):
    """A mutation observed a generation different from its snapshot."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(expected, actual)

    def __str__(self) -> str:
        return f"remote_mutation_conflict:{self.expected}:{self.actual}"


class RemoteMutationClosedError(RuntimeError):
    """The container has stopped accepting library-state mutations."""

    def __str__(self) -> str:
        return "remote_mutation_closed:library-state"


class RemoteMutationBarrierError(RuntimeError):
    """A mutation attempted to commit before all workers arrived."""

    def __init__(self, remaining: int) -> None:
        self.remaining = remaining
        super().__init__(remaining)

    def __str__(self) -> str:
        return f"remote_mutation_barrier_pending:{self.remaining}"


class RemoteMutationInvalidWorkerCountError(RuntimeError):
    """A worker barrier was declared with a negative party count."""

    def __init__(self, count: int) -> None:
        self.count = count
        super().__init__(count)

    def __str__(self) -> str:
        return f"remote_mutation_invalid_worker_count:{self.count}"


class RemoteMutationWorkerError(RuntimeError):
    """A sync worker was invoked without an owning mutation lease."""

    def __init__(self, worker: str) -> None:
        self.worker = worker
        super().__init__(worker)

    def __str__(self) -> str:
        return f"remote_mutation_worker_unbound:{self.worker}"
