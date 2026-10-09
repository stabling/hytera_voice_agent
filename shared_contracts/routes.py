# =============================================================================
# 【维护注释】文件：shared_contracts/routes.py
# 所属层次：跨进程稳定契约层
# 主要职责：定义 x86 网关、x64 智能体和 MCP 共享的数据模型、路由、标识符、安全与事件契约；应保持向后兼容。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：无；以导入副作用、常量、数据模型或包导出为主
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Stable local HTTP route constants shared by both processes."""

LOCAL_API_TOKEN_HEADER = "X-Hytera-Local-Token"

GATEWAY_HEALTH_PATH = "/health"
GATEWAY_PRIVATE_SMS_PATH = "/sms/private"
GATEWAY_GROUP_SMS_PATH = "/sms/group"
GATEWAY_PROTOBUF_RESULT_SMS_PATH = "/adk/protobuf/result-sms"
GATEWAY_SEND_WAV_PATH = "/voice/private/send-wav"

AGENT_HEALTH_PATH = "/health"
AGENT_VOICE_EVENT_PATH = "/voice/callback"
GATEWAY_CALLBACK_OUTBOX_STATUS_PATH = "/voice/callback/outbox"
GATEWAY_CALLBACK_DEAD_LETTER_PATH = "/voice/callback/dead-letters"
AGENT_CALLBACK_SPOOL_STATUS_PATH = "/voice/callback/spool"
AGENT_PLAN_TEXT_PATH = "/plan/text"
AGENT_RESULTS_PATH = "/results/latest"

DEFAULT_GATEWAY_BASE_URL = "http://127.0.0.1:18081"
DEFAULT_AGENT_BASE_URL = "http://127.0.0.1:18082"
DEFAULT_MCP_BASE_URL = "http://127.0.0.1:18083"
