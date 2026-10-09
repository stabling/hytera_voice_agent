# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/callbacks/manager.py
# 所属层次：网关回调可靠投递层
# 主要职责：负责回调接收、合并、重试、熔断、Outbox 和投递状态，避免网络波动影响 ADK 主链路。
# 项目内直接依赖：.lifecycle, .merger, .payload, .source, .transport
# 主要公开符号：VoiceCallbackManager
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

from .lifecycle import CallbackLifecycleMixin
from .merger import CallbackMergeMixin
from .payload import CallbackPayloadMixin
from .source import CallbackSourceMixin
from .transport import CallbackTransportMixin


class VoiceCallbackManager(
    CallbackLifecycleMixin,
    CallbackSourceMixin,
    CallbackMergeMixin,
    CallbackPayloadMixin,
    CallbackTransportMixin,
):
    """增量读取抓包音频、合并连续 RTP 并推送 HTTP 回调。"""
