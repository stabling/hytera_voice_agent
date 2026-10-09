# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/gateway.py
# 所属层次：x86 ADK 网关核心
# 主要职责：实现 32 位 Windows/Python 3.10 侧的海能达 ADK HTTP 网关。对外行为必须优先兼容现网和真实终端。
# 项目内直接依赖：.backend, .models
# 主要公开符号：RadioGateway
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

from .backend import HyteraAdkBackend
from .models import AdkEvent, CommandResult


class RadioGateway:
    """Small application-facing facade over :class:`HyteraAdkBackend`."""

    def __init__(self, backend: HyteraAdkBackend) -> None:
        self.backend = backend

    def __enter__(self) -> "RadioGateway":
        self.backend.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.backend.close()

    def poll(self, timeout: float = 1.0) -> AdkEvent | None:
        return self.backend.next_event(timeout)

    def send_private(self, *args: object, **kwargs: object) -> CommandResult:
        return self.backend.send_private_text(*args, **kwargs)

    def send_group(self, *args: object, **kwargs: object) -> CommandResult:
        return self.backend.send_group_text(*args, **kwargs)

    def voice_private(self, target_id: int, **kwargs: object) -> CommandResult:
        return self.backend.start_voice_call(target_id, "private", **kwargs)

    def voice_group(self, target_id: int, **kwargs: object) -> CommandResult:
        return self.backend.start_voice_call(target_id, "group", **kwargs)

    def stop_voice(self) -> CommandResult:
        return self.backend.stop_voice_call()
