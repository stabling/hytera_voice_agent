# =============================================================================
# 【维护注释】文件：voice_agent/capabilities/__init__.py
# 所属层次：能力注册与执行门面
# 主要职责：能力契约、注册表和本地原子插件执行门面。
# =============================================================================
from .executor import LocalCapabilityExecutor
from .models import CapabilitySpec
from .plugin_api import LocalCapabilityContext, LocalCapabilityPlugin, LocalCapabilityPluginRegistry
from .registry import CapabilityRegistry

__all__ = [
    "LocalCapabilityExecutor",
    "CapabilitySpec",
    "CapabilityRegistry",
    "LocalCapabilityContext",
    "LocalCapabilityPlugin",
    "LocalCapabilityPluginRegistry",
]
