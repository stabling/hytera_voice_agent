# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/adk/backend.py
# 所属层次：ADK 设备适配层
# 主要职责：封装海能达 ADK/CLR 交互、通道、消息、事件和语音底层能力。该层属于设备协议边界，重构只能做兼容性增强，不应改变既有 ADK 调用语义。
# 项目内直接依赖：.channels, .events, .lifecycle, .messaging, .reflection, .runtime, .voice
# 主要公开符号：HyteraAdkBackend
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

from .channels import ChannelMixin
from .events import EventMixin
from .lifecycle import LifecycleMixin
from .messaging import MessagingMixin
from .reflection import ReflectionMixin
from .runtime import RuntimeMixin
from .voice import VoiceMixin


class HyteraAdkBackend(
    LifecycleMixin,
    MessagingMixin,
    VoiceMixin,
    EventMixin,
    ChannelMixin,
    ReflectionMixin,
    RuntimeMixin,
):
    """Hytera V4 CLR 后端门面。

    业务代码只依赖该门面；具体能力由内部 mixin 分层实现。这样既保留原有
    公共 API，也避免生命周期、设备协议、通道策略和诊断逻辑互相缠绕。
    """

    REQUIRED = ("ADKCoreEngine_CLR.dll", "ADKCore.dll", "Audio.dll", "ADKCore.ini")
