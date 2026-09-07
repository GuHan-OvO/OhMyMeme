"""Per-container serialization for library-state mutations."""

from __future__ import annotations

import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Condition, Lock, get_ident
from time import monotonic
from typing import Final

from .remote_mutation_errors import (
    RemoteMutationBarrierError,
    RemoteMutationBusyError,
    RemoteMutationClosedError,
    RemoteMutationConflictError,
    RemoteMutationInvalidWorkerCountError,
    RemoteMutationReentrantError,
    RemoteMutationRootLockedError,
)

_ROOT_GUARD: Final = Lock()
_HELD_ROOTS: Final[dict[Path, weakref.ReferenceType]] = {}


def _release_root(root: Path, reference: weakref.ReferenceType) -> None:
    with _ROOT_GUARD:
        if _HELD_ROOTS.get(root) is reference:
            _HELD_ROOTS.pop(root, None)


class RemoteMutationLease:
    """One non-reentrant library-state lease held until commit and release."""

    def __init__(
        self,
        coordinator: RemoteMutationCoordinator,
        token: int,
        entrypoint: str,
        generation: int,
    ) -> None:
        self._coordinator = coordinator
        self._token = token
        self._released = False
        self._committed = False
        self._workers_remaining = 0
        self.entrypoint = entrypoint
        self.generation = generation

    @property
    def committed(self) -> bool:
        return self._committed

    def start_workers(self, count: int) -> None:
        """Declare the worker barrier owned by this mutation."""
        if count < 0:
            raise RemoteMutationInvalidWorkerCountError(count)
        self._coordinator._start_workers(self, count)

    def worker_done(self) -> None:
        """Arrive at the worker barrier."""
        self._coordinator._worker_done(self)

    def wait_for_workers(self, timeout: float | None = None) -> None:
        """Wait until all declared workers have arrived."""
        self._coordinator._wait_for_workers(self, timeout)

    def assert_generation(self) -> None:
        """Reject a stale manifest or mutation snapshot."""
        self._coordinator.assert_generation(self.generation)

    def commit(self, expected_generation: int | None = None) -> None:
        """Commit this mutation and advance the container generation."""
        self._coordinator._commit(self, expected_generation)

    def release(self) -> None:
        """Release the lease and record its terminal transcript event."""
        self._coordinator._release(self)


class RemoteMutationCoordinator:
    """Serialize library-state changes for one Container and data root."""

    def __init__(self, data_root: Path | str, group: str = "library-state") -> None:
        self.data_root = Path(data_root).resolve()
        self.group = group
        self._admission = Lock()
        self._state = Condition(Lock())
        self._generation = 0
        self._sequence = 0
        self._owner: int | None = None
        self._active: RemoteMutationLease | None = None
        self._closed = False
        self._transcript: list[dict[str, str | int | bool]] = []
        with _ROOT_GUARD:
            owner = _HELD_ROOTS.get(self.data_root)
            if owner is not None and owner() is not None:
                raise RemoteMutationRootLockedError("data-root")
            root = self.data_root
            _HELD_ROOTS[root] = weakref.ref(
                self, lambda reference, root=root: _release_root(root, reference)
            )

    @property
    def generation(self) -> int:
        with self._state:
            return self._generation

    def acquire(
        self, entrypoint: str, expected_generation: int | None = None
    ) -> RemoteMutationLease:
        """Acquire the non-blocking library-state lease."""
        with self._state:
            if self._closed:
                raise RemoteMutationClosedError()
            if self._owner == get_ident():
                raise RemoteMutationReentrantError()
        if not self._admission.acquire(blocking=False):
            raise RemoteMutationBusyError(self.group)
        with self._state:
            if self._closed:
                self._admission.release()
                raise RemoteMutationClosedError()
            actual = self._generation
            if expected_generation is not None and expected_generation != actual:
                self._admission.release()
                raise RemoteMutationConflictError(expected_generation, actual)
            self._sequence += 1
            lease = RemoteMutationLease(self, self._sequence, entrypoint, actual)
            self._owner = get_ident()
            self._active = lease
            self._record("acquire", lease, generation=actual)
            return lease

    @contextmanager
    def mutation(
        self, entrypoint: str, expected_generation: int | None = None
    ) -> Iterator[RemoteMutationLease]:
        """Hold a lease through all local, worker, and remote commit steps."""
        lease = self.acquire(entrypoint, expected_generation)
        try:
            yield lease
        finally:
            lease.release()

    def assert_generation(self, expected: int) -> None:
        """Verify that no committed mutation changed the captured generation."""
        with self._state:
            actual = self._generation
            if actual != expected:
                self._record(
                    "conflict", self._active, expected=expected, generation=actual
                )
                raise RemoteMutationConflictError(expected, actual)

    def validate_lease(
        self, lease: RemoteMutationLease, owner_required: bool = True
    ) -> None:
        """Verify that a lease belongs to this coordinator and is active."""
        with self._state:
            self._validate_lease(lease, owner_required=owner_required)

    def get_transcript(self) -> tuple[dict[str, str | int | bool], ...]:
        """Return an immutable snapshot of the mutation transcript."""
        with self._state:
            return tuple(dict(event) for event in self._transcript)

    def get_generation(self) -> int:
        """Return the current committed mutation generation."""
        return self.generation

    def get_mutation_transcript(self) -> tuple[dict[str, str | int | bool], ...]:
        """Return the mutation transcript under its explicit public name."""
        return self.get_transcript()

    def close(self) -> None:
        """Release the process-local data-root claim."""
        with self._state:
            if self._closed:
                return
            if self._active is not None:
                raise RemoteMutationBusyError(self.group)
            self._closed = True
        with _ROOT_GUARD:
            owner = _HELD_ROOTS.get(self.data_root)
            if owner is not None and owner() is self:
                _HELD_ROOTS.pop(self.data_root, None)

    def _start_workers(self, lease: RemoteMutationLease, count: int) -> None:
        with self._state:
            self._validate_lease(lease)
            lease._workers_remaining = count
            self._record("barrier_start", lease, parties=count, remaining=count)
            self._state.notify_all()

    def _worker_done(self, lease: RemoteMutationLease) -> None:
        with self._state:
            self._validate_lease(lease, owner_required=False)
            if lease._workers_remaining > 0:
                lease._workers_remaining -= 1
            self._record(
                "barrier_worker_done",
                lease,
                remaining=lease._workers_remaining,
            )
            self._state.notify_all()

    def _wait_for_workers(
        self, lease: RemoteMutationLease, timeout: float | None
    ) -> None:
        deadline = None if timeout is None else monotonic() + max(timeout, 0.0)
        with self._state:
            self._validate_lease(lease)
            while lease._workers_remaining:
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    raise RemoteMutationBarrierError(lease._workers_remaining)
                self._state.wait(remaining)
            self._record("barrier_complete", lease, remaining=0)

    def _commit(
        self, lease: RemoteMutationLease, expected_generation: int | None
    ) -> None:
        with self._state:
            self._validate_lease(lease)
            if lease._workers_remaining:
                raise RemoteMutationBarrierError(lease._workers_remaining)
            expected = lease.generation
            if expected_generation is not None:
                expected = expected_generation
            if expected != self._generation:
                self._record(
                    "conflict", lease, expected=expected, generation=self._generation
                )
                raise RemoteMutationConflictError(expected, self._generation)
            if lease._committed:
                return
            self._generation += 1
            lease._committed = True
            self._record("commit", lease, generation=self._generation)

    def _release(self, lease: RemoteMutationLease) -> None:
        with self._state:
            if lease._released:
                return
            self._validate_lease(lease)
            lease._released = True
            self._record(
                "release",
                lease,
                generation=self._generation,
                committed=lease._committed,
            )
            self._active = None
            self._owner = None
            self._state.notify_all()
        self._admission.release()

    def _validate_lease(
        self, lease: RemoteMutationLease, owner_required: bool = True
    ) -> None:
        if self._active is not lease or owner_required and self._owner != get_ident():
            raise RemoteMutationReentrantError()

    def _record(
        self,
        event: str,
        lease: RemoteMutationLease | None,
        **fields: str | int | bool,
    ) -> None:
        entry: dict[str, str | int | bool] = {
            "event": event,
            "group": self.group,
            "thread": get_ident(),
            "generation": self._generation,
        }
        if lease is not None:
            entry["token"] = lease._token
            entry["entrypoint"] = lease.entrypoint
        entry.update(fields)
        self._transcript.append(entry)
