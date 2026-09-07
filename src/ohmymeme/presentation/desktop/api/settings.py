"""设置窗口桥接所有权。"""

from .facades import SettingsApi


def create_settings_api(webui, settings):
    """创建设置窗口既有 ABI 的桥接对象。"""
    return SettingsApi(webui, settings)
