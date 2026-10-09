# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/config.py
# 所属层次：x86 ADK 网关核心
# 主要职责：实现 32 位 Windows/Python 3.10 侧的海能达 ADK HTTP 网关。对外行为必须优先兼容现网和真实终端。
# 项目内直接依赖：.settings
# 主要公开符号：无；以导入副作用、常量、数据模型或包导出为主
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""兼容配置入口。

新代码可从 :mod:`hytera_adk_gateway.settings` 导入；本模块保留原公共路径，
确保外部调用方无需修改。
"""

from .settings import (
    AUXILIARY_INI_ONLY_PORT_KEYS,
    CORE_PORT_KEYS,
    PORT_KEYS,
    ConfigError,
    GatewayConfig,
    load_config,
)

__all__ = [
    "AUXILIARY_INI_ONLY_PORT_KEYS",
    "CORE_PORT_KEYS",
    "PORT_KEYS",
    "ConfigError",
    "GatewayConfig",
    "load_config",
]
