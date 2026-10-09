# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/services/__init__.py
# 所属层次：x86 网关服务层
# 主要职责：承接 HTTP 路由并协调 ADK backend，保持协议转换、设备调用与 API 层解耦。
# 项目内直接依赖：.gateway_service, .voice_delivery
# 主要公开符号：无；以导入副作用、常量、数据模型或包导出为主
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from .gateway_service import GatewayService
from .voice_delivery import VoiceDeliveryService

__all__ = ["GatewayService", "VoiceDeliveryService"]
