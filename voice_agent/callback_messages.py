# =============================================================================
# 【维护注释】文件：voice_agent/callback_messages.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：callback_message
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

from types import MappingProxyType

CALLBACK_MESSAGES = MappingProxyType({
    "invalid_content_length": "invalid Content-Length",
    "body_too_large": "callback body exceeds configured limit",
    "invalid_voice_event": "invalid voice event: {error}",
    "queue_full": "asr queue full",
    "spool_error": "callback spool rejected the event",
    "accepted": "accepted_for_async_processing",
    "duplicate": "duplicate_already_accepted",
})


def callback_message(key: str, **values: object) -> str:
    return CALLBACK_MESSAGES[key].format(**values)
