# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/api/routers/voice.py
# 所属层次：x86 网关 HTTP 路由层
# 主要职责：定义 FastAPI 对外 HTTP 契约并把请求交给服务层。现有路径、方法、字段和状态语义由兼容契约测试保护。
# 项目内直接依赖：...callbacks, ...capture, ..voice_schemas
# 主要公开符号：create_voice_router
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""语音抓包、状态查询与 WAV 投递路由。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ...callbacks import VoiceCallbackManager
from ...capture import TSharkVoiceCaptureManager
from ..voice_schemas import VoiceCaptureStartRequest, VoiceCaptureStopRequest, VoiceInfoRequest

logger = logging.getLogger("hytera_adk_gateway.api.voice")


def create_voice_router(
    get_backend: Callable[[], Any],
    *,
    capture_root: str | Path = "voice_capture",
    capture_manager: TSharkVoiceCaptureManager | None = None,
    callback_manager: VoiceCallbackManager | None = None,
    prefix: str = "/voice",
    default_stop_timeout: float = 3.0,
) -> APIRouter:
    """创建语音 API；依赖通过参数注入，避免路由直接持有全局状态。"""

    router = APIRouter(prefix=prefix, tags=["语音"])
    capture = capture_manager or TSharkVoiceCaptureManager(capture_root)

    @router.post("/capture/start", summary="启动 TShark 语音抓包")
    def voice_capture_start(req: VoiceCaptureStartRequest) -> dict[str, Any]:
        try:
            result = capture.start(
                iface=req.iface,
                ports=req.ports,
                src_ip=req.src_ip,
                dst_ip=req.dst_ip,
                host=req.host,
                strict_direction=req.strict_direction,
                out_dir=req.out_dir,
                tshark_path=req.tshark_path,
            )
            result.setdefault("capture_root", str(capture.root_dir.resolve()))
        except Exception as exc:
            logger.exception("启动语音抓包失败")
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"{type(exc).__name__}: {exc}",
                    "hint": "请先使用 tshark -D 确认真实网卡编号或名称。",
                },
            ) from exc
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result)
        return {"ok": True, "data": result}

    @router.post("/capture/stop", summary="停止 TShark 语音抓包")
    def voice_capture_stop(req: VoiceCaptureStopRequest | None = None) -> dict[str, Any]:
        try:
            result = capture.stop(timeout=req.timeout if req else default_stop_timeout)
        except Exception as exc:
            logger.exception("停止语音抓包失败")
            raise HTTPException(
                status_code=400, detail={"error": f"{type(exc).__name__}: {exc}"}
            ) from exc
        return {"ok": True, "data": result}

    @router.post("/info", summary="查询语音状态并按需更新回调参数")
    def voice_info(req: VoiceInfoRequest | None = None) -> dict[str, Any]:
        request = req or VoiceInfoRequest()
        try:
            backend_state = get_backend().transport_state()
        except Exception as exc:
            backend_state = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        callback_state: dict[str, Any] | None = None
        if callback_manager is not None:
            try:
                has_update = any(
                    value is not None
                    for value in (
                        request.callback_url,
                        request.callback_enabled,
                        request.callback_interval_seconds,
                        request.callback_timeout_seconds,
                        request.callback_max_packets_per_tick,
                    )
                )
                callback_state = (
                    callback_manager.update(
                        enabled=request.callback_enabled,
                        callback_url=request.callback_url,
                        interval_seconds=request.callback_interval_seconds,
                        timeout_seconds=request.callback_timeout_seconds,
                        max_packets_per_tick=request.callback_max_packets_per_tick,
                    )
                    if has_update
                    else callback_manager.status()
                )
            except Exception as exc:
                callback_state = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        try:
            capture_info = capture.info(limit=request.limit, refresh_wav=request.refresh_wav)
            capture_info.setdefault("capture_root", str(capture.root_dir.resolve()))
        except Exception as exc:
            logger.exception("读取语音抓包信息失败")
            raise HTTPException(
                status_code=400, detail={"error": f"{type(exc).__name__}: {exc}"}
            ) from exc

        return {
            "ok": True,
            "data": {
                "capture": capture_info,
                "backend": backend_state,
                "callback": callback_state,
            },
        }

    @router.get("/callback/outbox", summary="查询可靠回调 Outbox 状态")
    def callback_outbox_status() -> dict[str, Any]:
        if callback_manager is None:
            raise HTTPException(status_code=503, detail="callback manager unavailable")
        return {"ok": True, "data": callback_manager.status().get("outbox")}

    @router.get("/callback/dead-letters", summary="查询回调 DEAD_LETTER")
    def callback_dead_letters(limit: int = 100) -> dict[str, Any]:
        if callback_manager is None:
            raise HTTPException(status_code=503, detail="callback manager unavailable")
        return {
            "ok": True,
            "data": callback_manager.dead_letters(limit=max(1, min(1000, int(limit)))),
        }

    @router.post("/private/send-wav", summary="预留 WAV 私呼语音接口（未实现）")
    async def voice_private_send_wav(
        target_id: int = Form(..., description="目标终端 Radio ID"),
        file: UploadFile = File(..., description="WAV 文件"),
        channel_id: int | None = Form(None, description="可选 ADK runtime channel ID"),
        frame_ms: int = Form(60, description="RTP 分包时长（毫秒）"),
        payload_type: int = Form(0, description="RTP PT；0 表示 PCMU"),
        ssrc: int = Form(0x12345678, description="RTP SSRC"),
        transmit: bool = Form(True, description="是否实际发送 RTP"),
        start_call: bool = Form(True, description="发送前是否发起呼叫"),
        stop_call: bool = Form(True, description="发送后是否结束呼叫"),
        packet_interval_ms: int = Form(60, description="RTP 包发送间隔"),
        max_packets: int = Form(0, description="最多发送包数；0 表示全部"),
    ) -> dict[str, Any]:
        del (
            target_id,
            file,
            channel_id,
            frame_ms,
            payload_type,
            ssrc,
            transmit,
            start_call,
            stop_call,
            packet_interval_ms,
            max_packets,
        )
        raise HTTPException(
            status_code=501,
            detail="send-wav 接口已保留，等待后续实现",
        )

    return router
