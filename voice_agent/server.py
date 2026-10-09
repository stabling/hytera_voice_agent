# =============================================================================
# 【维护注释】文件：voice_agent/server.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：., .contact_directory, .http_api, .logging_utils, .runtime, .slots, .utils, .zone_channel_directory
# 主要公开符号：run_server
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import logging

from . import config
from .contact_directory import load_contact_map
from .http_api import CallbackHandler, NonBlockingThreadingHTTPServer
from .logging_utils import configure_console_logging, log_event
from .runtime import CALL_STORE_RUNTIME, REMINDER_SCHEDULER, start_workers
from .slots import load_business_slots
from .utils import ensure_dirs, now_iso, update_stats
from .zone_channel_directory import load_zone_channel_map

logger = logging.getLogger(__name__)


def run_server() -> None:
    configure_console_logging()
    ensure_dirs()
    load_contact_map()
    load_zone_channel_map()
    load_business_slots()
    update_stats(service_started_at=now_iso())
    recovered = REMINDER_SCHEDULER.recover_pending()
    if recovered:
        log_event(logger, "待办提醒启动恢复", recovered_count=recovered)
    start_workers()
    server = NonBlockingThreadingHTTPServer((config.HOST, config.PORT), CallbackHandler)
    log_event(
        logger,
        "语音指令服务已启动完成",
        host=config.HOST,
        port=config.PORT,
        callback=f"http://127.0.0.1:{config.PORT}/voice/callback",
        workers=config.WORKER_COUNT,
        asr_model=(
            config.LOCAL_ASR_MODEL if config.ASR_PROVIDER == "local" else config.ASR_MODEL
        ),
        planner_model=config.PLANNER_DISPLAY_MODEL,
    )
    if config.ASR_PROVIDER == "dashscope" and not config.DASHSCOPE_API_KEY:
        logger.warning("DASHSCOPE_API_KEY 未设置：真实音频 ASR 不可用，文本 MCP 验收仍可用")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到停止信号")
    finally:
        CALL_STORE_RUNTIME.stop_projector()
        server.server_close()
        logger.info("语音指令服务已停止")


if __name__ == "__main__":
    run_server()
