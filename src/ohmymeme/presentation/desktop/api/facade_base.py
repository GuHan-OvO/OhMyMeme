"""Shared validation for desktop bridge facades."""

from collections.abc import Sequence

from ohmymeme.core.schemas.bridge import (
    BRIDGE_CONTRACT,
    BridgeError,
    JsonValue,
    SettingsPatch,
)

from .plugin_dispatch import ACTIONS, compatibility_dispatcher


class FacadeBase:
    """Validate a JSON-safe call and project failures to legacy sentinels."""

    def _call(
        self, method: str, args: Sequence[JsonValue], failure: JsonValue
    ) -> JsonValue:
        try:
            self._ensure_legacy()
            contract = getattr(self, "_contract", BRIDGE_CONTRACT)
            checked = contract.validate_input(method, args)
            legacy_args = tuple(
                (
                    value.model_dump(exclude_none=True)
                    if isinstance(value, SettingsPatch)
                    else value
                )
                for value in checked
            )
            if method in ACTIONS[self._surface]:
                if not hasattr(self, "_plugin_dispatcher"):
                    webui = getattr(self, "_webui", None)
                    self._plugin_dispatcher = compatibility_dispatcher(
                        self._legacy,
                        self._surface,
                        getattr(webui, "_plugin_action_registry", None),
                        getattr(webui, "_enabled_import_plugins", None),
                    )
                result = self._plugin_dispatcher.dispatch(method, legacy_args)
            else:
                result = getattr(self._legacy, method)(*legacy_args)
            return contract.validate_output(method, result)
        except RuntimeError as error:
            self._last_bridge_error = BridgeError(
                code="internal_runtime_error",
                message=str(error),
                details={"method": method},
            )
            return failure
        except (TypeError, ValueError):
            self._last_bridge_error = BridgeError(
                code="contract_violation",
                message="bridge payload rejected",
                details={"method": method},
            )
            return failure
