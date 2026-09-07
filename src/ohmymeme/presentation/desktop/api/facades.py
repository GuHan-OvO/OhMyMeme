"""Public bridge facade compatibility exports."""

from .main_facade import MainBridgeFacade
from .settings_facade import SettingsBridgeFacade

JsApi = MainBridgeFacade
SettingsApi = SettingsBridgeFacade

__all__ = ["JsApi", "MainBridgeFacade", "SettingsApi", "SettingsBridgeFacade"]
