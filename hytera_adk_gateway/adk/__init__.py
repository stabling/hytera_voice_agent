# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/adk/__init__.py
# 所属层次：ADK 设备适配层
# 主要职责：封装海能达 ADK/CLR 交互、通道、消息、事件和语音底层能力。该层属于设备协议边界，重构只能做兼容性增强，不应改变既有 ADK 调用语义。
# 项目内直接依赖：.backend, .shared
# 主要公开符号：无；以导入副作用、常量、数据模型或包导出为主
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Hytera ADK 适配层。

对外仅暴露 ``HyteraAdkBackend``；内部按生命周期、消息、语音、事件、
通道、反射与运行时诊断拆分，避免单文件巨型类。
"""

from .backend import HyteraAdkBackend
from .shared import ADK_SEND_ERRORS, AdkRuntimeError

__all__ = ["HyteraAdkBackend", "AdkRuntimeError", "ADK_SEND_ERRORS"]
