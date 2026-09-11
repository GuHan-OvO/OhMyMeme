# pyright: basic

from importlib.metadata import entry_points


class PluginRegistry:
    def __init__(self, descriptors, builtins=None, discovered_entry_points=None):
        self._descriptors = tuple(descriptors)
        self._builtins = dict(builtins or {})
        self._records = {}
        candidates = (
            tuple(discovered_entry_points)
            if discovered_entry_points is not None
            else self._discover_entry_points()
        )
        for descriptor in self._descriptors:
            self._records[descriptor.id] = self._discover(descriptor, candidates)

    def _discover_entry_points(self):
        discovered = entry_points()
        if hasattr(discovered, "select"):
            return tuple(discovered.select(group="ohmymeme.plugins.v1"))
        return tuple(
            candidate
            for candidate in discovered
            if candidate.group == "ohmymeme.plugins.v1"
        )

    def _discover(self, descriptor, candidates):
        if descriptor.id in self._builtins:
            return {
                "descriptor": descriptor,
                "factory": self._builtins[descriptor.id],
                "instance": None,
                "quarantine": None,
                "source": "builtin",
                "started": False,
            }
        matches = tuple(
            candidate
            for candidate in candidates
            if candidate.group == descriptor.group and candidate.name == descriptor.name
        )
        if len(matches) != 1:
            reason = "missing_entry_point" if not matches else "duplicate_entry_point"
            return self._quarantined(descriptor, reason)
        candidate = matches[0]
        if candidate.value != descriptor.value:
            return self._quarantined(descriptor, "entry_point_mismatch")
        return {
            "descriptor": descriptor,
            "entry_point": candidate,
            "factory": None,
            "instance": None,
            "quarantine": None,
            "source": "entry_point",
            "started": False,
        }

    def _quarantined(self, descriptor, reason):
        return {
            "descriptor": descriptor,
            "factory": None,
            "instance": None,
            "quarantine": reason,
            "source": "quarantined",
            "started": False,
        }

    def descriptors(self):
        return tuple(record["descriptor"] for record in self._records.values())

    def status(self):
        return tuple(
            {
                "id": descriptor.id,
                "quarantine": record["quarantine"],
                "source": record["source"],
                "started": record["started"],
            }
            for descriptor, record in (
                (descriptor, self._records[descriptor.id])
                for descriptor in self._descriptors
            )
        )

    def get(self, provider_id):
        record = self._records.get(provider_id)
        if record is None or record["quarantine"] is not None:
            return None
        instance = record["instance"]
        if instance is not None:
            return instance
        factory = record["factory"]
        if factory is None:
            try:
                factory = record["entry_point"].load()
            except Exception as error:
                self._quarantine_record(record, f"load_failed:{type(error).__name__}")
                return None
            record["factory"] = factory
        if not callable(factory):
            self._quarantine_record(record, "factory_not_callable")
            return None
        try:
            record["instance"] = factory()
        except Exception as error:
            self._quarantine_record(record, f"factory_failed:{type(error).__name__}")
            return None
        return record["instance"]

    def start(self, provider_id, context):
        record = self._records.get(provider_id)
        if record is None or record["started"]:
            return False
        instance = self.get(provider_id)
        if instance is None or not callable(getattr(instance, "start", None)):
            if instance is not None:
                self._quarantine_record(record, "invalid_lifecycle")
            return False
        try:
            instance.start(context)
        except Exception as error:
            self._quarantine_record(record, f"start_failed:{type(error).__name__}")
            return False
        record["started"] = True
        return True

    def stop(self, provider_id):
        record = self._records.get(provider_id)
        if record is None or not record["started"]:
            return False
        record["started"] = False
        instance = record["instance"]
        if instance is None or not callable(getattr(instance, "stop", None)):
            self._quarantine_record(record, "invalid_lifecycle")
            return False
        try:
            instance.stop()
        except Exception as error:
            self._quarantine_record(record, f"stop_failed:{type(error).__name__}")
            return False
        return True

    def close(self):
        for descriptor in self._descriptors:
            self.stop(descriptor.id)

    def _quarantine_record(self, record, reason):
        record["factory"] = None
        record["instance"] = None
        record["quarantine"] = reason
        record["source"] = "quarantined"
        record["started"] = False
