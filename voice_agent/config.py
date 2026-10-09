# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/config.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：.env
# 主要公开符号：无；以导入副作用、常量、数据模型或包导出为主
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Runtime configuration for the voice capability agent.

This module loads `.env` from the project root before reading environment variables.
The batch ASR defaults are intentionally conservative for qwen3-asr-flash:
- qwen3-asr-flash official online recording-file ASR quota is 100 RPM.
- We default to 45 RPM + serial batch execution + retry-until-success for 429/5xx.
"""

from __future__ import annotations

import os
from pathlib import Path

from .env import env_bool, env_float, env_int, load_dotenv


# 导入配置前先读取项目根目录的 .env；系统环境变量具有更高优先级。
load_dotenv()

HOST = os.environ.get("CALLBACK_HOST", "127.0.0.1")
PORT = int(os.environ.get("CALLBACK_PORT", "18082"))

SAVE_DIR = Path(os.environ.get("SAVE_DIR", "received_callbacks"))
RESULT_JSONL = SAVE_DIR / "asr_results.jsonl"
RECEIVED_JSONL = SAVE_DIR / "asr_received.jsonl"

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "").strip()
DASHSCOPE_BASE_URL = os.environ.get(
    "DASHSCOPE_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
).rstrip("/")
DASHSCOPE_CHAT_COMPLETIONS_URL = DASHSCOPE_BASE_URL + "/chat/completions"
QWEN_AUDIO_ASR_URL = os.environ.get(
    "QWEN_AUDIO_ASR_URL",
    (
        "https://dashscope.aliyuncs.com"
        "/api/v1/services/aigc/multimodal-generation/generation"
    ),
).strip()

ASR_MODEL = os.environ.get("QWEN_ASR_MODEL", "qwen3-asr-flash-2026-02-10").strip()
# 流式 ASR（qwen-audio-3.0-asr-flash-streaming）WebSocket 双工端点：
# run-task → task-started → 二进制音频帧 → result-generated → finish-task。
QWEN_ASR_STREAMING_URL = os.environ.get(
    "QWEN_ASR_STREAMING_URL",
    "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
).strip()
# ASR 提供方切换：``dashscope``（阿里云 DashScope，默认，走原有链路）或
# ``local``（局域网 OpenAI 兼容自部署服务，如 vLLM 部署的 Qwen3-ASR-1.7B）。
# 两个提供方共存互不影响：切回 dashscope 只需把 QWEN_ASR_PROVIDER 改回
# dashscope，原有 QWEN_ASR_MODEL/QWEN_AUDIO_ASR_* 配置继续生效。
ASR_PROVIDER = os.environ.get("QWEN_ASR_PROVIDER", "dashscope").strip().lower()
if ASR_PROVIDER not in {"dashscope", "local"}:
    raise ValueError("QWEN_ASR_PROVIDER must be dashscope or local")
# 局域网 OpenAI 兼容 ASR（/v1/chat/completions）：无 API Key，音频以 base64
# 内嵌在 input_audio 中。这些配置仅 ASR_PROVIDER=local 时使用。
LOCAL_ASR_BASE_URL = os.environ.get(
    "QWEN_LOCAL_ASR_BASE_URL", "http://10.115.6.210:18000/v1"
).rstrip("/")
LOCAL_ASR_CHAT_COMPLETIONS_URL = LOCAL_ASR_BASE_URL + "/chat/completions"
LOCAL_ASR_MODEL = os.environ.get("QWEN_LOCAL_ASR_MODEL", "Qwen3-ASR-1.7B").strip()
# 自部署服务通常无鉴权；留空则请求不带 Authorization 头。
LOCAL_ASR_API_KEY = os.environ.get("QWEN_LOCAL_ASR_API_KEY", "").strip()
PLANNER_MODEL = os.environ.get("QWEN_PLANNER_MODEL", "qwen-plus").strip()
# Planner（语义规划）提供方切换：``dashscope``（阿里云 qwen-plus，默认）或
# ``local``（局域网 OpenAI 兼容自部署 qwen3.5-4b）。两个提供方共存：切回
# dashscope 只需改回 QWEN_PLANNER_PROVIDER=dashscope，原有 QWEN_PLANNER_MODEL
# 配置继续生效。注意：call summary/translation 模型（QWEN_CALL_SUMMARY_MODEL
# 等）是独立配置，不受此开关影响。
PLANNER_PROVIDER = os.environ.get("QWEN_PLANNER_PROVIDER", "dashscope").strip().lower()
if PLANNER_PROVIDER not in {"dashscope", "local"}:
    raise ValueError("QWEN_PLANNER_PROVIDER must be dashscope or local")
# 局域网 OpenAI 兼容 planner（/v1/chat/completions）：无 API Key。仅
# PLANNER_PROVIDER=local 时使用。
LOCAL_PLANNER_BASE_URL = os.environ.get(
    "QWEN_LOCAL_PLANNER_BASE_URL", "http://10.115.36.37:30000/v1"
).rstrip("/")
LOCAL_PLANNER_CHAT_COMPLETIONS_URL = LOCAL_PLANNER_BASE_URL + "/chat/completions"
LOCAL_PLANNER_MODEL = os.environ.get("QWEN_LOCAL_PLANNER_MODEL", "qwen3-8b-awq").strip()
LOCAL_PLANNER_API_KEY = os.environ.get("QWEN_LOCAL_PLANNER_API_KEY", "").strip()
# 本地 planner 思考模式（Qwen3 系列 thinking）默认关闭：规划任务只需直接
# JSON 输出，开思考会显著拉长延迟且可能把推理文本混入 content。仅
# PLANNER_PROVIDER=local 时生效（通过 chat_template_kwargs 传给 vLLM）。
LOCAL_PLANNER_ENABLE_THINKING = env_bool("QWEN_LOCAL_PLANNER_ENABLE_THINKING", False)
# 当前生效的 planner 模型名（供启动日志与诊断展示；业务调用走上述 provider 分发）。
PLANNER_DISPLAY_MODEL = (
    LOCAL_PLANNER_MODEL if PLANNER_PROVIDER == "local" else PLANNER_MODEL
)
# ``full`` 完全复现旧版 planner prompt；``compact`` 仅去除模型无需负责的
# executor/risk/side-effect/domain 元数据。代码默认 full 保证外部旧部署兼容，
# 本工程的 .env.agent 显式启用 compact 以降低上下文体积。
PLANNER_PROMPT_MODE = os.environ.get("PLANNER_PROMPT_MODE", "full").strip().lower()
if PLANNER_PROMPT_MODE not in {"full", "compact"}:
    raise ValueError("PLANNER_PROMPT_MODE must be full or compact")
# Planner Context Cache only changes how the request is packaged for Qwen.
# The capability set, planner rules and server-side validation remain unchanged.
# Default False preserves legacy deployments; .env.agent enables it for this package.
PLANNER_CONTEXT_CACHE_ENABLE = env_bool("PLANNER_CONTEXT_CACHE_ENABLE", False)
# 高频指令快捷路径：切换区域/信道、呼叫联系人等确定性指令跳过 LLM 规划，
# 直接构造能力计划（仍走 canonicalizer + schema 校验 + policy）。
# 回退 LLM 路径：PLANNER_SHORTCUT_ENABLE=false。
PLANNER_SHORTCUT_ENABLE = env_bool("PLANNER_SHORTCUT_ENABLE", True)
CALL_SUMMARY_MODEL = os.environ.get("QWEN_CALL_SUMMARY_MODEL", PLANNER_MODEL).strip()
CALL_TRANSLATION_MODEL = os.environ.get("QWEN_CALL_TRANSLATION_MODEL", PLANNER_MODEL).strip()
ASR_LANGUAGE = os.environ.get("QWEN_ASR_LANGUAGE", "zh").strip()
ENABLE_ITN = env_bool("QWEN_ASR_ENABLE_ITN", False)
ENABLE_CALL_SUMMARY_LLM = env_bool("ENABLE_CALL_SUMMARY_LLM", True)
ENABLE_CALL_TRANSLATION_LLM = env_bool("ENABLE_CALL_TRANSLATION_LLM", True)
ENABLE_LLM_FALLBACK = env_bool("ENABLE_LLM_FALLBACK", True)

# Qwen-Audio-3.0-ASR-Flash native HTTP options. These settings are ignored by
# qwen3-asr-flash, so switching models only requires changing QWEN_ASR_MODEL.
QWEN_AUDIO_ASR_ENABLE_CONTEXT = env_bool("QWEN_AUDIO_ASR_ENABLE_CONTEXT", True)
QWEN_AUDIO_ASR_ENABLE_HOTWORDS = env_bool("QWEN_AUDIO_ASR_ENABLE_HOTWORDS", True)
QWEN_AUDIO_ASR_ENTITY_HOTWORD_WEIGHT = env_int(
    "QWEN_AUDIO_ASR_ENTITY_HOTWORD_WEIGHT", 4
)
QWEN_AUDIO_ASR_TERM_HOTWORD_WEIGHT = env_int(
    "QWEN_AUDIO_ASR_TERM_HOTWORD_WEIGHT", 3
)
QWEN_AUDIO_ASR_CONTEXT_MAX_CHARS = env_int("QWEN_AUDIO_ASR_CONTEXT_MAX_CHARS", 400)
QWEN_AUDIO_ASR_EXTRA_HOTWORDS = os.environ.get(
    "QWEN_AUDIO_ASR_EXTRA_HOTWORDS", ""
).strip()

# Latest-wins queueing cancels only work that has not started; active worker tasks continue.
CANCEL_QUEUED_TASKS_ON_NEW_TASK = env_bool(
    "CANCEL_QUEUED_TASKS_ON_NEW_TASK", True
)

# 语音回调入口 ACK 前移：校验与背压检查通过后立即回 ACK，
# spool 持久化与 ASR 入队改由后台单线程串行执行（省去 ~125ms 同步
# SQLite fsync + 队列整理耗时）。同线程保证 FIFO 顺序；失败仅记录日志
# 与统计，不改变 200 ACK 语义（网关侧重发可由 spool 幂等去重兜底）。
# 设为 false 回退到旧的同步 persist→enqueue→ACK 行为。
VOICE_INGRESS_ASYNC_PERSIST = env_bool("VOICE_INGRESS_ASYNC_PERSIST", True)

# Base API timeouts. Actual ASR timeout adds X-DashScope-Wait-Timeout automatically.
HTTP_TIMEOUT_SECONDS = env_float("DASHSCOPE_TIMEOUT_SECONDS", 90.0)
# keep-alive session 传输层瞬态重试：仅对连接类错误（SSL EOF/连接重置，
# 生产多发于进程重启后的首个请求）快速重试新建连接，不重试超时与 HTTP
# 状态错误，避免长退避引发超时风暴。设 0 关闭。
HTTP_TRANSPORT_RETRIES = env_int("HTTP_TRANSPORT_RETRIES", 2)
PLANNER_TIMEOUT_SECONDS = env_float("QWEN_PLANNER_TIMEOUT_SECONDS", 60.0)
CALL_SUMMARY_TIMEOUT_SECONDS = env_float(
    "QWEN_CALL_SUMMARY_TIMEOUT_SECONDS", PLANNER_TIMEOUT_SECONDS
)
CALL_SUMMARY_MAX_OUTPUT_TOKENS = env_int(
    "QWEN_CALL_SUMMARY_MAX_OUTPUT_TOKENS", 256
)
CALL_SUMMARY_TEMPERATURE = env_float("QWEN_CALL_SUMMARY_TEMPERATURE", 0.0)
CALL_TRANSLATION_TIMEOUT_SECONDS = env_float(
    "QWEN_CALL_TRANSLATION_TIMEOUT_SECONDS", PLANNER_TIMEOUT_SECONDS
)
CALL_TRANSLATION_MAX_OUTPUT_TOKENS = env_int(
    "QWEN_CALL_TRANSLATION_MAX_OUTPUT_TOKENS", 1024
)
CALL_TRANSLATION_TEMPERATURE = env_float("QWEN_CALL_TRANSLATION_TEMPERATURE", 0.0)

DEFAULT_SAMPLE_RATE = int(os.environ.get("AUDIO_SAMPLE_RATE", "8000"))
DEFAULT_CHANNELS = 1
DEFAULT_SAMPLE_WIDTH = 2
MIN_AUDIO_SECONDS = env_float("MIN_AUDIO_SECONDS", 0.10)
MAX_DATA_URL_BYTES = env_int("MAX_DATA_URL_BYTES", 10 * 1024 * 1024)

MAX_QUEUE_SIZE = env_int("ASR_QUEUE_SIZE", 128)
WORKER_COUNT = env_int("ASR_WORKER_COUNT", 2)
LOG_QUEUE_SIZE = env_int("LOG_QUEUE_SIZE", 0)
HTTP_REQUEST_QUEUE_SIZE = env_int("HTTP_REQUEST_QUEUE_SIZE", 512)

CALLBACK_MAX_BODY_BYTES = env_int("CALLBACK_MAX_BODY_BYTES", 24 * 1024 * 1024)
CALLBACK_SPOOL_DIR = Path(os.environ.get("CALLBACK_SPOOL_DIR", str(SAVE_DIR / "callback_spool")))
CALLBACK_SPOOL_MAX_EVENTS = env_int("CALLBACK_SPOOL_MAX_EVENTS", 10_000)
CALLBACK_SPOOL_MAX_BYTES = env_int("CALLBACK_SPOOL_MAX_BYTES", 2 * 1024 * 1024 * 1024)
CALLBACK_MAX_AUDIO_BYTES = env_int("CALLBACK_MAX_AUDIO_BYTES", 16 * 1024 * 1024)
CALLBACK_SPOOL_RETENTION_SECONDS = env_float(
    "CALLBACK_SPOOL_RETENTION_SECONDS", 7 * 24 * 3600.0
)
STATE_DB_PATH = Path(os.environ.get("STATE_DB_PATH", str(Path("data") / "state.sqlite3")))
EVENT_JOURNAL_ENABLE = os.environ.get("EVENT_JOURNAL_ENABLE", "false").strip().lower()
if EVENT_JOURNAL_ENABLE not in {"false", "shadow", "true"}:
    raise ValueError("EVENT_JOURNAL_ENABLE must be one of: false, shadow, true")
EVENT_JOURNAL_PATH = Path(
    os.environ.get("EVENT_JOURNAL_PATH", str(STATE_DB_PATH))
)
EVENT_JOURNAL_FAIL_MODE = os.environ.get("EVENT_JOURNAL_FAIL_MODE", "open").strip().lower()
if EVENT_JOURNAL_FAIL_MODE not in {"open", "closed"}:
    raise ValueError("EVENT_JOURNAL_FAIL_MODE must be open or closed")
OTEL_PROPAGATION_ENABLE = env_bool("OTEL_PROPAGATION_ENABLE", False)
EVENT_QUERY_MAX_LIMIT = env_int("EVENT_QUERY_MAX_LIMIT", 200)

# the device-execution path.
CALL_STORE_ENABLE = os.environ.get("CALL_STORE_ENABLE", "true").strip().lower()
if CALL_STORE_ENABLE not in {"false", "shadow", "true"}:
    raise ValueError("CALL_STORE_ENABLE must be one of: false, shadow, true")
CALL_STORE_PATH = Path(
    os.environ.get("CALL_STORE_PATH", str(STATE_DB_PATH))
)
CALL_STORE_FAIL_MODE = os.environ.get("CALL_STORE_FAIL_MODE", "open").strip().lower()
if CALL_STORE_FAIL_MODE not in {"open", "closed"}:
    raise ValueError("CALL_STORE_FAIL_MODE must be open or closed")
CALL_CORRELATION_WINDOW_SECONDS = env_float("CALL_CORRELATION_WINDOW_SECONDS", 30.0)
CALL_CORRELATION_MIN_SCORE = env_float("CALL_CORRELATION_MIN_SCORE", 0.75)
if not 0.0 <= CALL_CORRELATION_MIN_SCORE <= 1.0:
    raise ValueError("CALL_CORRELATION_MIN_SCORE must be between 0 and 1")
CALL_PROJECTOR_POLL_SECONDS = env_float("CALL_PROJECTOR_POLL_SECONDS", 1.0)
CALL_QUERY_MAX_LIMIT = env_int("CALL_QUERY_MAX_LIMIT", 200)
CALL_AUDIO_RETENTION_DAYS = env_int("CALL_AUDIO_RETENTION_DAYS", 0)
CALL_TRANSCRIPT_RETENTION_DAYS = env_int("CALL_TRANSCRIPT_RETENTION_DAYS", 0)

# Call-content capabilities are available whenever CallStore is enabled.
CALL_RESOLVE_DEFAULT_SCOPE = os.environ.get(
    "CALL_RESOLVE_DEFAULT_SCOPE", "same_actor_terminal"
).strip()
CALL_RESOLVE_MAX_CANDIDATES = env_int("CALL_RESOLVE_MAX_CANDIDATES", 10)
CALL_CONTENT_WAIT_TIMEOUT_SECONDS = env_float("CALL_CONTENT_WAIT_TIMEOUT_SECONDS", 300.0)

OPERATION_EXECUTOR_MODE = "atomic"
OPERATION_STORE_PATH = Path(
    os.environ.get("OPERATION_STORE_PATH", str(STATE_DB_PATH))
)
OPERATION_DEFAULT_TIMEOUT_SECONDS = env_float("OPERATION_DEFAULT_TIMEOUT_SECONDS", 60.0)
OPERATION_MAX_RETRIES = env_int("OPERATION_MAX_RETRIES", 0)
OPERATION_AUDIT_ENABLE = env_bool("OPERATION_AUDIT_ENABLE", True)
OPERATION_QUERY_MAX_LIMIT = env_int("OPERATION_QUERY_MAX_LIMIT", 200)

CONTEXT_BUILDER_MODE = os.environ.get("CONTEXT_BUILDER_MODE", "off").strip().lower()
if CONTEXT_BUILDER_MODE not in {"off", "shadow", "active"}:
    raise ValueError("CONTEXT_BUILDER_MODE must be one of: off, shadow, active")
CONTEXT_STORE_PATH = Path(
    os.environ.get("CONTEXT_STORE_PATH", str(STATE_DB_PATH))
)
CONTEXT_MAX_ITEMS = env_int("CONTEXT_MAX_ITEMS", 30)
CONTEXT_MAX_TOKENS = env_int("CONTEXT_MAX_TOKENS", 3000)
CONTEXT_RECENT_CALL_DAYS = env_int("CONTEXT_RECENT_CALL_DAYS", 7)
CONTEXT_QUERY_MAX_LIMIT = env_int("CONTEXT_QUERY_MAX_LIMIT", 200)
CONTEXT_DEFAULT_TENANT_ID = os.environ.get("CONTEXT_DEFAULT_TENANT_ID", "").strip() or None

MEMORY_ENABLE = os.environ.get("MEMORY_ENABLE", "false").strip().lower()
if MEMORY_ENABLE not in {"false", "candidate", "active"}:
    raise ValueError("MEMORY_ENABLE must be one of: false, candidate, active")
MEMORY_STORE_PATH = Path(
    os.environ.get("MEMORY_STORE_PATH", str(STATE_DB_PATH))
)
MEMORY_AUTO_VERIFY = env_bool("MEMORY_AUTO_VERIFY", False)
if MEMORY_AUTO_VERIFY:
    raise ValueError("MEMORY_AUTO_VERIFY must remain false")
VECTOR_SEARCH_ENABLE = env_bool("VECTOR_SEARCH_ENABLE", False)
MEMORY_FTS_ENABLE = env_bool("MEMORY_FTS_ENABLE", False)
MEMORY_ADMIN_TOKEN = os.environ.get("MEMORY_ADMIN_TOKEN", "").strip()

# LangGraph thread-scoped working memory. SQLite is for lightweight deployments;
# production should set postgres and install the separate checkpoint package.
WORKING_MEMORY_BACKEND = os.environ.get("WORKING_MEMORY_BACKEND", "off").strip().lower()
if WORKING_MEMORY_BACKEND not in {"off", "sqlite", "postgres"}:
    raise ValueError("WORKING_MEMORY_BACKEND must be one of: off, sqlite, postgres")
WORKING_MEMORY_SQLITE_PATH = Path(
    os.environ.get("WORKING_MEMORY_SQLITE_PATH", str(Path("data") / "context" / "langgraph_checkpoints.sqlite3"))
)
WORKING_MEMORY_POSTGRES_DSN = os.environ.get("WORKING_MEMORY_POSTGRES_DSN", "").strip()

PLAN_MAX_STEPS = env_int("PLAN_MAX_STEPS", 8)
PLAN_MAX_REPLANS = env_int("PLAN_MAX_REPLANS", 2)
PLAN_MAX_MODEL_CALLS = env_int("PLAN_MAX_MODEL_CALLS", 3)
PLAN_TOTAL_TIMEOUT_SECONDS = env_float("PLAN_TOTAL_TIMEOUT_SECONDS", 120.0)
POLICY_MODE = os.environ.get("POLICY_MODE", "audit").strip().lower()
if POLICY_MODE not in {"audit", "enforce"}:
    raise ValueError("POLICY_MODE must be one of: audit, enforce")
AUTO_EXECUTE_MAX_RISK = os.environ.get("AUTO_EXECUTE_MAX_RISK", "R1").strip().upper()
if AUTO_EXECUTE_MAX_RISK not in {"R0", "R1", "R2", "R3", "R4", "R5"}:
    raise ValueError("AUTO_EXECUTE_MAX_RISK must be R0..R5")
POLICY_R2_ALLOWLIST = tuple(x.strip() for x in os.environ.get("POLICY_R2_ALLOWLIST", "").split(",") if x.strip())
PLANNING_STORE_PATH = Path(os.environ.get("PLANNING_STORE_PATH", str(STATE_DB_PATH)))
APPROVAL_STORE_PATH = Path(os.environ.get("APPROVAL_STORE_PATH", str(STATE_DB_PATH)))
APPROVAL_ADMIN_TOKEN = os.environ.get("APPROVAL_ADMIN_TOKEN", "").strip()
APPROVAL_ALLOWED_ROLES = tuple(x.strip().lower() for x in os.environ.get("APPROVAL_ALLOWED_ROLES", "approver,admin").split(",") if x.strip())
if not APPROVAL_ALLOWED_ROLES:
    raise ValueError("APPROVAL_ALLOWED_ROLES must contain at least one role")

# SQLite remains the rollback-safe default; production uses dual-write before postgres.
STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "sqlite").strip().lower()
if STORAGE_BACKEND not in {"sqlite", "dual", "postgres"}:
    raise ValueError("STORAGE_BACKEND must be one of: sqlite, dual, postgres")
POSTGRES_DSN = os.environ.get("POSTGRES_DSN", "").strip()
POSTGRES_POOL_SIZE = env_int("POSTGRES_POOL_SIZE", 8)
POSTGRES_FAIL_OPEN = env_bool("POSTGRES_FAIL_OPEN", True)
EVENT_PARTITION_INTERVAL = os.environ.get("EVENT_PARTITION_INTERVAL", "monthly").strip().lower()
if EVENT_PARTITION_INTERVAL not in {"monthly"}:
    raise ValueError("EVENT_PARTITION_INTERVAL currently supports monthly only")
OBJECT_STORE_ENABLE = env_bool("OBJECT_STORE_ENABLE", False)
OBJECT_STORE_BUCKET = os.environ.get("OBJECT_STORE_BUCKET", "").strip()
OBJECT_STORE_PREFIX = os.environ.get("OBJECT_STORE_PREFIX", "audio").strip()
OBJECT_STORE_ENDPOINT_URL = os.environ.get("OBJECT_STORE_ENDPOINT_URL", "").strip()
OBJECT_STORE_KMS_KEY_ID = os.environ.get("OBJECT_STORE_KMS_KEY_ID", "").strip()
OBJECT_ARCHIVE_QUEUE_SIZE = env_int("OBJECT_ARCHIVE_QUEUE_SIZE", 1024)
OBJECT_STORE_FILESYSTEM_ROOT = os.environ.get("OBJECT_STORE_FILESYSTEM_ROOT", "").strip()
OTEL_ENABLE = env_bool("OTEL_ENABLE", False)
RETENTION_AUDIO_DAYS = env_int("RETENTION_AUDIO_DAYS", 0)
RETENTION_TRANSCRIPT_DAYS = env_int("RETENTION_TRANSCRIPT_DAYS", 0)
RETENTION_DECISION_DAYS = env_int("RETENTION_DECISION_DAYS", 0)
RETENTION_AUDIT_DAYS = env_int("RETENTION_AUDIT_DAYS", 0)
STORAGE_READ_PERCENT_POSTGRES = env_int("STORAGE_READ_PERCENT_POSTGRES", 0)
if not 0 <= STORAGE_READ_PERCENT_POSTGRES <= 100:
    raise ValueError("STORAGE_READ_PERCENT_POSTGRES must be 0..100")
if STORAGE_BACKEND in {"dual", "postgres"} and not POSTGRES_DSN:
    raise ValueError("POSTGRES_DSN is required when STORAGE_BACKEND is dual/postgres")

CALLBACK_ACK_STATUS_CODE = env_int("CALLBACK_ACK_STATUS_CODE", 202)
if CALLBACK_ACK_STATUS_CODE not in {200, 202}:
    raise ValueError("CALLBACK_ACK_STATUS_CODE must be 200 or 202")
TERMINAL_SESSION_IDLE_TIMEOUT_SECONDS = env_float(
    "TERMINAL_SESSION_IDLE_TIMEOUT_SECONDS", 1800.0
)

MAX_LATEST_RESULTS = env_int("MAX_LATEST_RESULTS", 200)

# ============================================================
# 消息分类 / 四库归档 / 用户历史（CONTROL_IDS 体系）
# ============================================================
# CONTROL_IDS / LOCAL_CONTROL_ID 在 shared_contracts.control_ids 统一定义，
# 进程启动时一次性加载为内存集合，运行期 O(1) 查询。
from shared_contracts.control_ids import build_identity_config  # noqa: E402

MESSAGE_IDENTITY = build_identity_config()
MESSAGE_ARCHIVE_ENABLE = env_bool("MESSAGE_ARCHIVE_ENABLE", True)
MESSAGE_ARCHIVE_DIR = Path(
    os.environ.get("MESSAGE_ARCHIVE_DIR", str(Path("data") / "messages"))
)
MESSAGE_ARCHIVE_QUEUE_SIZE = env_int("MESSAGE_ARCHIVE_QUEUE_SIZE", 1024)
MESSAGE_ARCHIVE_BUSY_TIMEOUT_MS = env_int("MESSAGE_ARCHIVE_BUSY_TIMEOUT_MS", 5000)
# direct-text / direct-sms 测试入口的默认身份（需求 §25）：
# source=模拟用户终端，target=LOCAL_CONTROL_ID。
DIRECT_TEXT_DEFAULT_SOURCE_ID = (
    os.environ.get("DIRECT_TEXT_DEFAULT_SOURCE_ID", "5").strip()
)

# Batch mode: safest defaults for offline evaluation.
# qwen3-asr-flash official online quota is 100 RPM. 45 RPM keeps enough headroom for
# account sharing, server-side smoothing and second-level burst constraints.
BATCH_FORCE_SERIAL_ASR = env_bool("BATCH_FORCE_SERIAL_ASR", True)
BATCH_WORKERS = env_int("BATCH_WORKERS", 1)
BATCH_DEFAULT_EXTS = os.environ.get(
    "BATCH_AUDIO_EXTS", ".wav,.mp3,.m4a,.flac,.ogg,.pcm,.ulaw,.alaw"
).strip()

# DashScope / ASR rate-limit protection.
ASR_RATE_LIMIT_RPM = env_float("ASR_RATE_LIMIT_RPM", 45.0)
PLANNER_RATE_LIMIT_RPM = env_float("PLANNER_RATE_LIMIT_RPM", 30.0)
DASHSCOPE_WAIT_TIMEOUT_SECONDS = env_int("DASHSCOPE_WAIT_TIMEOUT_SECONDS", 120)

# Retry policy. Retry-until-success is only for transient ASR errors such as 429/5xx/network.
# Permanent errors such as invalid API key, invalid request, audio conversion failure still fail fast.
ASR_RETRY_UNTIL_SUCCESS = env_bool("ASR_RETRY_UNTIL_SUCCESS", True)
ASR_MAX_RETRIES = env_int("ASR_MAX_RETRIES", 0)  # 0 = unlimited when ASR_RETRY_UNTIL_SUCCESS=true
ASR_429_SLEEP_SECONDS = env_float("ASR_429_SLEEP_SECONDS", 65.0)
ASR_RETRY_BASE_SECONDS = env_float("ASR_RETRY_BASE_SECONDS", 8.0)
ASR_RETRY_MAX_SECONDS = env_float("ASR_RETRY_MAX_SECONDS", 300.0)
ASR_RETRY_JITTER_SECONDS = env_float("ASR_RETRY_JITTER_SECONDS", 3.0)
ASR_EMPTY_TEXT_MAX_RETRIES = env_int("ASR_EMPTY_TEXT_MAX_RETRIES", 2)

PLANNER_MAX_RETRIES = env_int("PLANNER_MAX_RETRIES", 3)

# ============================================================
# MCP Tool Auto-Execution
# ============================================================
# Capability operations call MCP tools through the validated HTTP boundary.
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://127.0.0.1:18083").rstrip("/")
MCP_PROTOCOL_VERSION = os.environ.get("MCP_PROTOCOL_VERSION", "2025-11-25").strip()
MCP_TOOL_TIMEOUT_SECONDS = env_float("MCP_TOOL_TIMEOUT_SECONDS", 10.0)


# ============================================================
# Audio diagnostics and preprocessing
# ============================================================
# Save every callback stage so raw G.711, decoded 8 kHz WAV and the exact WAV
# sent to ASR can be compared directly.
AUDIO_DEBUG_SAVE = env_bool("AUDIO_DEBUG_SAVE", True)
AUDIO_DEBUG_DIR = Path(os.environ.get("AUDIO_DEBUG_DIR", str(SAVE_DIR / "audio_debug")))
DEFAULT_CALLBACK_CODEC = os.environ.get("AUDIO_DEFAULT_CODEC", "pcmu").strip().lower()
AUDIO_PREPROCESS_ENABLE = env_bool("AUDIO_PREPROCESS_ENABLE", True)
ASR_TARGET_SAMPLE_RATE = env_int("ASR_TARGET_SAMPLE_RATE", 16000)
AUDIO_ANALYSIS_FRAME_MS = env_int("AUDIO_ANALYSIS_FRAME_MS", 20)
AUDIO_SILENCE_MIN_RMS = env_int("AUDIO_SILENCE_MIN_RMS", 120)
AUDIO_SILENCE_NOISE_MULTIPLIER = env_float("AUDIO_SILENCE_NOISE_MULTIPLIER", 3.0)
AUDIO_SILENCE_OVERALL_RMS_RATIO = env_float("AUDIO_SILENCE_OVERALL_RMS_RATIO", 0.12)
AUDIO_TRIM_PADDING_MS = env_int("AUDIO_TRIM_PADDING_MS", 160)
AUDIO_MIN_KEEP_SECONDS = env_float("AUDIO_MIN_KEEP_SECONDS", 0.35)
AUDIO_TARGET_RMS = env_float("AUDIO_TARGET_RMS", 3500.0)
AUDIO_TARGET_PEAK = env_float("AUDIO_TARGET_PEAK", 24500.0)
AUDIO_MIN_GAIN = env_float("AUDIO_MIN_GAIN", 0.25)
AUDIO_MAX_GAIN = env_float("AUDIO_MAX_GAIN", 6.0)
