# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/adk/shared.py
# 所属层次：ADK 设备适配层
# 主要职责：封装海能达 ADK/CLR 交互、通道、消息、事件和语音底层能力。该层属于设备协议边界，重构只能做兼容性增强，不应改变既有 ADK 调用语义。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：AdkRuntimeError, ClrReflectionMethod
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

from typing import Any


class AdkRuntimeError(RuntimeError):
    """Hytera 原生 ADK 初始化、调用或状态错误。"""


ADK_SEND_ERRORS: dict[int, str] = {
    -1: "PACKET_ERROR_BUFFER_INVALID",
    -2: "PACKET_ERROR_BUFFER_SIZE_ERROR",
    -3: "PACKET_ERROR_OPTION_CODE_NOT_FOUND",
    -4: "CONNECTION_ERROR_GET_CONNECTION_FAIL",
    -5: "CONNECTION_ERROR_NOT_ESTABLISH",
    -6: "SOCKET_ERROR_SEND_FAIL",
    -7: "SOCKET_ERROR_SET_SUBNET_ERROR",
    -8: "SOCKET_ERROR_OPEN_FAIL",
    -9: "COMMAND_ERROR_SAME_COMMAND_EXIST",
    -10: "COMMAND_ERROR_DATA_FORMAT_ERROR",
    -11: "COMMAND_ERROR_CHANNEL_NOT_EXISTS",
    -12: "COMMAND_ERROR_MESSAGE_QUEUE_NOT_EXISTS",
    -13: "COMMAND_ERROR_QUEUE_FAIL",
    -14: "COMMAND_ERROR_NO_COMMAND",
    -15: "COMMAND_ERROR_TYPE_ERROR",
    -16: "COMMAND_ERROR_ID_ERROR",
    -17: "SYSTEM_ERROR_CONFIGE_REPEATER_ERROR",
}


class ClrReflectionMethod:
    """将 CLR ``MethodInfo.Invoke`` 包装为普通 Python 可调用对象。"""

    def __init__(self, target: Any, method_info: Any, display_name: str) -> None:
        self._target = target
        self._method_info = method_info
        self.__name__ = display_name

    def __call__(self, *args: Any) -> Any:
        import System  # type: ignore

        return self._method_info.Invoke(
            self._target,
            System.Array[System.Object](list(args)),
        )
