from copy import deepcopy
from pathlib import Path
from threading import RLock

from ohmymeme.core.imports import ImportPath


class OperationLog:
    def __init__(self, operation=None):
        # Provider logs are operation output, not global logging handlers.
        self.operation = operation

    def log(self, message, *args):
        # Format first, then redact before adding the output to the queue.
        if self.operation:
            text = str(message) % args if args else str(message)
            self.operation.emit_log(self.operation.serialize_log(text))

    debug = info = warning = error = log


class ImportRuntime:
    def __init__(self, initial, active_status, request_fields):
        # No host service or durable path is retained by a plugin instance.
        self._state = deepcopy(initial)
        self._lock = RLock()
        self._cancel = False
        self._running = False
        self._context = None
        self._active_status = active_status
        self._request_fields = frozenset(request_fields)
        self._log = OperationLog()

    def start(self, context):
        # Start reserves only; the host owns the actual worker and scope lifetime.
        unknown = set(context.request or {}) - self._request_fields
        if unknown:
            raise ValueError("request: " + ", ".join(sorted(unknown)))
        with self._lock:
            if self._running:
                return False
            if any(proc.poll() is None for proc in getattr(self, "_processes", ())):
                return False
            self._reset_state()
            self._context = context
            self._log = OperationLog(context.operation)
            self._state["status"] = self._active_status
            self._running = True
        return True

    def finish(self):
        # Only called after import_media and all of its parallel work have exited.
        with self._lock:
            self._context = None
            self._log = OperationLog()
            self._running = False

    def _sanitize(self, value):
        # All progress and exception text shares the policy redaction boundary.
        if self._context and self._context.operation:
            return self._context.operation.serialize_progress(value)
        return value

    def _publish(self):
        # The host controls where the serialized snapshot is delivered.
        if self._context:
            self._context.progress(self._sanitize(deepcopy(self._state)))

    def _submit(self, paths):
        # Keep the legacy batch statistics while admitting only through the sink.
        context = self._context
        context.operation.require("import.submit")
        result = context.sink.import_batch(
            tuple(ImportPath(Path(path), Path(path).stem) for path in paths),
            cancelled=self._check_cancel,
        )
        return {"ids": list(result.imported_ids), "rejected": result.rejected}
