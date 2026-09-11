# pyright: basic

from collections import namedtuple
from typing import Protocol


class PluginCapabilities(namedtuple("PluginCapabilitiesBase", "names")):
    __slots__ = ()

    def __new__(cls, names):
        return super().__new__(cls, tuple(names))

    def _replace(self, /, **kwargs):
        return type(self)(kwargs.get("names", self.names))

    def allows(self, capability):
        return capability in self.names


class PluginDescriptor(
    namedtuple(
        "PluginDescriptorBase",
        "id api_version package_root group name value capabilities",
    )
):
    __slots__ = ()

    @property
    def entry_point(self):
        return self.group, self.name, self.value


class PluginContext(namedtuple("PluginContextBase", "descriptor capabilities")):
    __slots__ = ()


class ImportPluginContext(
    namedtuple("ImportPluginContextBase", "descriptor sink progress is_cancelled")
):
    __slots__ = ()


class SyncPluginContext(
    namedtuple("SyncPluginContextBase", "descriptor progress is_cancelled")
):
    __slots__ = ()


class LanPluginContext(
    namedtuple(
        "LanPluginContextBase", "descriptor byte_transport progress is_cancelled"
    )
):
    __slots__ = ()


class PluginLifecycle(Protocol):
    def start(self, context): ...

    def stop(self): ...


class ImportSink(Protocol):
    def import_path(self, request): ...

    def import_bytes(self, request): ...


class ImportProvider(PluginLifecycle, Protocol):
    def import_media(self, context): ...


class SyncProvider(PluginLifecycle, Protocol):
    def create_backend(self, context): ...
