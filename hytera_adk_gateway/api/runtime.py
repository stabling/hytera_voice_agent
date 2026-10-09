# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/api/runtime.py
# 所属层次：x86 网关 API 装配层
# 主要职责：负责 FastAPI 应用工厂、依赖注入、安全中间件、请求/响应模型和生命周期装配。
# 项目内直接依赖：..callbacks, ..capture, ..config, ..services
# 主要公开符号：start_auto_voice_capture, callback_settings_from_config, GatewayApplicationRuntime
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""FastAPI 进程内资源的创建、启动与反向释放。"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI

from ..callbacks import VoiceCallbackManager, VoiceCallbackSettings
from ..capture import TSharkVoiceCaptureManager
from ..config import GatewayConfig, load_config
from ..services import GatewayService

logger = logging.getLogger("hytera_adk_gateway.api")


def start_auto_voice_capture(
    manager: TSharkVoiceCaptureManager,
    cfg: GatewayConfig,
) -> dict[str, Any]:
    """按配置启动外部 TShark 抓包；禁用时返回可观测状态而不创建进程。"""
    if not cfg.voice_capture_auto_start:
        return {"ok": True, "enabled": False, "message": "voice_capture.auto_start=false"}

    iface = cfg.voice_capture_iface or os.environ.get("HYTERA_VOICE_CAPTURE_IFACE")
    if not iface:
        return {
            "ok": False,
            "enabled": True,
            "error": "未配置 voice_capture.iface，请先运行 tshark -D",
        }

    result = manager.start(
        iface=iface,
        ports=cfg.voice_capture_ports,
        src_ip=cfg.voice_capture_src_ip,
        dst_ip=cfg.voice_capture_dst_ip,
        host=cfg.voice_capture_host,
        strict_direction=cfg.voice_capture_strict_direction,
        out_dir=cfg.voice_capture_out_dir,
        tshark_path=cfg.voice_capture_tshark_path,
    )
    result.setdefault("enabled", True)
    result.setdefault("capture_root", str(manager.root_dir.resolve()))
    return result


def callback_settings_from_config(cfg: GatewayConfig) -> VoiceCallbackSettings:
    """把网关配置投影为回调子系统所需的最小设置。"""
    return VoiceCallbackSettings(
        enabled=cfg.voice_callback_enabled,
        callback_url=cfg.voice_callback_url,
        interval_seconds=cfg.voice_callback_interval_seconds,
        timeout_seconds=cfg.voice_callback_timeout_seconds,
        max_packets_per_tick=cfg.voice_callback_max_packets_per_tick,
        merge_continuous_audio=cfg.voice_callback_merge_continuous_audio,
        merge_gap_seconds=cfg.voice_callback_merge_gap_seconds,
        merge_max_packets=cfg.voice_callback_merge_max_packets,
        merge_max_audio_bytes=cfg.voice_callback_merge_max_audio_bytes,
        merge_flush_on_idle_tick=cfg.voice_callback_merge_flush_on_idle_tick,
        merge_flush_on_batch_end=cfg.voice_callback_merge_flush_on_batch_end,
        fallback_read_audio_raw=cfg.voice_callback_fallback_read_audio_raw,
    )


class GatewayApplicationRuntime:
    """集中持有应用级长生命周期资源，避免路由直接管理线程或厂商后端。"""

    def __init__(self, config_path: str, *, debug_fields: bool = False) -> None:
        self.config_path = config_path
        self.debug_fields = debug_fields
        self.initial_config = load_config(config_path)
        self.capture_manager = TSharkVoiceCaptureManager(
            self.initial_config.voice_capture_root,
            sms_ports=[
                self.initial_config.ports["tmp_slot1"],
                self.initial_config.ports["tmp_slot2"],
            ],
        )
        self.service: GatewayService | None = None
        self.callback_manager = VoiceCallbackManager(
            self.capture_manager,
            get_context=self._voice_callback_context,
        )

    def _voice_callback_context(self) -> dict[str, Any]:
        """延迟读取服务上下文，避免回调管理器反向依赖 FastAPI 实例。"""
        if self.service is None:
            return {}
        return self.service.voice_callback_context()

    @asynccontextmanager
    async def lifespan(self, application: FastAPI) -> AsyncIterator[None]:
        """按依赖顺序启动资源，并在退出时按相反顺序释放。"""
        cfg = load_config(self.config_path)
        service = GatewayService(cfg, debug_fields=self.debug_fields)
        self.service = service

        application.state.gateway_runtime = self
        application.state.gateway_service = service
        application.state.voice_capture_manager = self.capture_manager
        application.state.voice_callback_manager = self.callback_manager

        try:
            service.start()
            auto_result = start_auto_voice_capture(self.capture_manager, cfg)
            application.state.voice_auto_capture_start = auto_result
            service.voice_auto_capture_start = auto_result
            if not auto_result.get("ok"):
                service.remember_error(f"自动语音抓包启动失败: {auto_result}")

            self.callback_manager.configure(callback_settings_from_config(cfg))
            self.callback_manager.start()
            yield
        finally:
            self.callback_manager.stop()
            try:
                self.capture_manager.stop(timeout=cfg.thread_join_timeout_seconds)
            except Exception as exc:  # 厂商/外部进程退出不能阻断服务释放
                logger.warning("停止语音抓包失败: %s", exc)
            service.stop()
            self.service = None
