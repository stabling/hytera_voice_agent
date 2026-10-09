# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/services/gateway_service.py
# 所属层次：x86 网关服务层
# 主要职责：承接 HTTP 路由并协调 ADK backend，保持协议转换、设备调用与 API 层解耦。
# 项目内直接依赖：shared_runtime.diagnostics, ..backend, ..config, ..models, ..adk.events, shared_runtime.event_publisher
# 主要公开符号：GatewayService
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

from shared_runtime.diagnostics import log_trace
from shared_contracts.control_ids import LOCAL_CONTROL_ID

from ..backend import HyteraAdkBackend
from ..config import GatewayConfig
from ..models import AdkEvent
from ..adk.events import adk_event_to_domain_event
from shared_runtime.event_publisher import build_event_publisher

logger = logging.getLogger("hytera_adk_gateway.service")


class GatewayService:
    """HTTP 网关服务层，管理 ADK UDP 后端的长生命周期。

    职责：
    - 管理后端生命周期（启动/停止）
    - 事件泵：持续从后端获取回调事件
    - 看门狗：监控事件泵和后端健康状态，必要时自动恢复
    - 提供短信发送和状态查询的高层接口
    """

    def __init__(self, cfg: GatewayConfig, *, debug_fields: bool = False) -> None:
        self.cfg = cfg
        self.backend = HyteraAdkBackend(cfg, debug_fields=debug_fields)
        self._stop = threading.Event()
        self._pump_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._events: Deque[Dict[str, Any]] = deque(maxlen=cfg.event_history_size)
        self._errors: Deque[str] = deque(maxlen=cfg.error_history_size)
        self.started_at: Optional[float] = None
        self.last_event_at: Optional[float] = None
        self.last_watchdog_at: Optional[float] = None
        self.watchdog_restarts: int = 0
        self.voice_auto_capture_start: dict[str, Any] | None = None
        self._transport_announced = False
        self._startup_complete_announced = False
        self._event_publisher = build_event_publisher(
            source_service="adk-gateway",
            default_path="data/context/context_journal.sqlite3",
        )

    # ---- 生命周期 --------------------------------------------------------

    def start(self) -> None:
        """启动 ADK 后端、事件泵和看门狗线程。"""
        if self.started_at is not None:
            return
        self.backend.start()
        self.started_at = time.time()
        self._stop.clear()
        self._pump_thread = threading.Thread(
            target=self._event_pump,
            name="hytera-adk-event-pump",
            daemon=False,
        )
        self._pump_thread.start()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="hytera-adk-watchdog",
            daemon=False,
        )
        self._watchdog_thread.start()
        logger.info("网关服务已启动")

    def stop(self) -> None:
        """停止所有后台线程并关闭 ADK 后端。"""
        self._stop.set()
        if self._pump_thread is not None:
            self._pump_thread.join(timeout=self.cfg.thread_join_timeout_seconds)
            self._pump_thread = None
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=self.cfg.thread_join_timeout_seconds)
            self._watchdog_thread = None
        self.backend.close()
        if self._transport_announced:
            logger.info("UDP 通信已断开")
        self._transport_announced = False
        self._startup_complete_announced = False
        self.started_at = None
        logger.info("网关服务已停止")

    # ---- 内部线程 --------------------------------------------------------

    def remember_error(self, message: str) -> None:
        """记录错误到内存环形缓冲区（用于 /health 接口展示）。"""
        with self._lock:
            self._errors.append(f"[{time.strftime('%H:%M:%S')}] {message}")
        logger.warning(message)

    def _remember_error(self, message: str) -> None:
        """兼容旧的内部调用路径；新代码使用 :meth:`remember_error`。"""
        self.remember_error(message)

    def _event_pump(self) -> None:
        """事件泵：持续从后端队列取出回调事件并处理。"""
        while not self._stop.is_set():
            try:
                event = self.backend.next_event(timeout=0.25)
            except Exception as exc:
                self.remember_error(f"事件泵异常: {type(exc).__name__}: {exc}")
                continue
            if event is None:
                continue
            self._handle_event(event)

    def _watchdog_loop(self) -> None:
        """看门狗：每 2 秒检查事件泵和后端状态，必要时自动恢复。

        FastAPI 仅处理 HTTP 请求。ADK DLL 拥有长生命周期的 UDP socket 和回调，
        因此需要看门狗线程在事件泵意外退出或后端异常关闭时尝试恢复。
        """
        while not self._stop.wait(self.cfg.watchdog_interval_seconds):
            self.last_watchdog_at = time.time()
            try:
                # 检查事件泵是否存活
                if self._pump_thread is None or not self._pump_thread.is_alive():
                    self.remember_error("事件泵线程已死亡，正在重启")
                    self._pump_thread = threading.Thread(
                        target=self._event_pump,
                        name="hytera-adk-event-pump",
                        daemon=False,
                    )
                    self._pump_thread.start()
                # 检查后端是否仍在运行
                if not self.backend.is_started:
                    self.remember_error("ADK 后端意外关闭，正在重启")
                    self.backend.close()
                    self.backend.start()
                    self.watchdog_restarts += 1
            except Exception as exc:
                self.remember_error(f"看门狗异常: {type(exc).__name__}: {exc}")

    def _handle_event(self, event: AdkEvent) -> None:
        """保存并向控制台输出关键 ADK/UDP 事件。

        TMP 消息、通道上下线和传输生命周期必须可见；高频音频帧仅进入
        内存事件历史，避免控制台被无意义刷屏。
        """
        item = event.to_dict()
        self.last_event_at = time.time()
        try:
            fact = adk_event_to_domain_event(event)
            if fact is not None:
                self._event_publisher.publish(fact)
                item["domain_event_id"] = fact.event_id
                item["context_confidence"] = fact.confidence
        except Exception as exc:
            logger.warning(
                "adk call event publish failed service=%s option=%s error=%s: %s",
                event.service,
                event.option,
                type(exc).__name__,
                exc,
            )
        with self._lock:
            self._events.append(item)

        if event.kind == "transport_ready":
            self._transport_announced = True
            logger.info(
                "UDP 通信已建立（ADK 监听器就绪） | listener_ip=%s ports=%s",
                event.metadata.get("listener_ip") or event.metadata.get("third_party_server_ip"),
                event.metadata.get("enabled_ports"),
            )
        elif event.kind == "channel_online":
            logger.info(
                "UDP 通信通道已上线 | channel_id=%s metadata=%s",
                event.channel_id,
                dict(event.metadata),
            )
        elif event.kind == "channel_offline":
            logger.warning("UDP 通信通道已断开 | channel_id=%s", event.channel_id)
        elif event.kind == "stopped":
            logger.info("UDP 通信已断开")
            self._transport_announced = False

        # 新消息必须输出到界面。TMP 服务包含短信/文本消息及其应答；即使
        # 某些 ADK 版本未解析出 text，也保留 option 和 ID 便于诊断。
        if event.service == "TMP" or event.text is not None:
            logger.info(
                "接收到新消息 | service=%s kind=%s option=%s channel_id=%s "
                "source_id=%s target_id=%s request_id=%s text=%r result_code=%s",
                event.service,
                event.kind,
                event.option,
                event.channel_id,
                event.source_id,
                event.target_id,
                event.request_id,
                event.text,
                event.result_code,
            )

        # 需求 §2.2/§35：入站 TMP 短信转发给智能体 /sms/callback 做分类与归档。
        self._maybe_forward_sms(event)

        selected_channel = getattr(self.backend, "selected_channel", None)
        selected_source = getattr(self.backend, "selected_channel_source", None)
        if (
            not self._startup_complete_announced
            and selected_channel is not None
            and selected_source == "adk_auto"
        ):
            self._startup_complete_announced = True
            logger.info(
                "已自动检测到 channel_id=%s，网关已启动完成",
                selected_channel,
            )

    # ---- 短信转发（需求 §2.2/§35） --------------------------------------

    def _maybe_forward_sms(self, event: AdkEvent) -> None:
        """入站 TMP 短信转发给智能体做统一分类/归档；失败只记日志。"""
        from ..agent_client import forward_sms_to_agent, sms_forward_enabled

        if not sms_forward_enabled():
            return
        text = str(event.text or "").strip()
        if event.service != "TMP" or not text:
            return
        if event.source_id is None:
            # 无源端点的 TMP 事件多为发送应答/回执，不属于入站短信。
            return
        option = str(event.option or "").upper()
        payload: Dict[str, Any] = {
            "event_type": "sms_received",
            "text": text,
            "source_id": event.source_id,
            "target_id": event.target_id,
            "channel_id": event.channel_id,
            "request_id": event.request_id,
            "occurred_at": event.at.isoformat(),
            "origin": "adk_sms",
        }
        if "GROUP" in option and event.target_id is not None:
            # 需求 §3：组消息 target_id=组ID、group_id=组ID。
            payload["group_id"] = event.target_id
        try:
            forward_sms_to_agent(payload, timeout=2.0)
        except Exception as exc:
            logger.warning(
                "入站短信转发失败 | source_id=%s target_id=%s error=%s: %s",
                event.source_id,
                event.target_id,
                type(exc).__name__,
                exc,
            )

    # ---- 短信接口 --------------------------------------------------------

    def send_private_sms(
        self,
        target_id: int,
        text,
        *,
        require_ack: bool = False,
        channel_id: Optional[int] = None,
        hex_encode: bool = True,
    ) -> Dict[str, Any]:
        """发送私人短信，并记录网关到 ADK 的关键路由参数。"""
        log_trace(
            logger,
            "网关私人短信服务调用",
            target_id=target_id,
            channel_id=channel_id,
            require_ack=require_ack,
            hex_encode=hex_encode,
            adk_service="TMP",
            adk_method="SendCommand",
        )
        service_started = time.perf_counter()
        result = self.backend.send_private_text(
            target_id, text, require_ack=require_ack, channel_id=channel_id,
            hex_encode=hex_encode,
        ).to_dict()
        timing_ms = dict(result.get("timing_ms") or {})
        timing_ms["gateway_service_ms"] = round((time.perf_counter() - service_started) * 1000.0, 3)
        result["timing_ms"] = timing_ms
        log_trace(
            logger,
            "网关私人短信服务结果",
            target_id=target_id,
            channel_id=result.get("channel_id", channel_id),
            adk_service="TMP",
            adk_method="SendCommand",
            result=result,
            timing_ms=result.get("timing_ms"),
        )
        return result

    def send_group_sms(
        self,
        group_id: int,
        text: str,
        *,
        channel_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """发送组呼短信。"""
        return self.backend.send_group_text(group_id, text, channel_id=channel_id).to_dict()

    # ---- 语音回调上下文 --------------------------------------------------

    def voice_callback_context(self) -> Dict[str, Any]:
        """推断语音包回调所需的 channel_id / target_id / group_id 上下文。

        RTP 包本身通常只包含媒体层字段，不一定带业务目标 ID。
        因此这里优先使用当前主动语音会话，其次使用最近 ADK 回调事件，
        最后回退到当前自动选择的 ADK channel。
        """
        context: Dict[str, Any] = {}
        voice = getattr(self.backend, "voice", None)
        if voice is not None:
            context["channel_id"] = getattr(voice, "channel_id", None)
            context["target_id"] = getattr(voice, "target_id", None)
            context["call_type"] = getattr(voice, "call_type", None)
            context["request_id"] = getattr(voice, "call_request_id", None)
            # 需求 §2.2：中转台主动发起的语音，source 固定为 LOCAL_CONTROL_ID。
            context["source_id"] = LOCAL_CONTROL_ID
            context["observed_source_id"] = LOCAL_CONTROL_ID
            context["context_method"] = "active_voice_session"
            context["context_confidence"] = 1.0
            if str(context.get("call_type") or "").lower() == "group":
                context["group_id"] = context.pop("target_id", None)
                context["observed_target_id"] = None
            else:
                context["observed_target_id"] = context.get("target_id")
            return {k: v for k, v in context.items() if v is not None}

        with self._lock:
            events = list(self._events)

        def _valid_id(value: Any) -> bool:
            return isinstance(value, int) and value > 0

        latest_with_ids: Dict[str, Any] | None = None
        for item in reversed(events):
            # 只认携带有效呼叫端点（source/target 任一为正）的事件：空闲广播
            # 报告的 _senderID=0/_targatID=0，且其 channel_id 是另一信道的
            # 空闲状态，不能覆盖最近的合法呼叫上下文。
            if _valid_id(item.get("source_id")) or _valid_id(item.get("target_id")):
                latest_with_ids = item
                break
        if latest_with_ids:
            option = str(latest_with_ids.get("option") or "").upper()
            source_id = latest_with_ids.get("source_id")
            target_id = latest_with_ids.get("target_id")
            channel_id = latest_with_ids.get("channel_id")
            if _valid_id(channel_id):
                context["channel_id"] = channel_id
            if _valid_id(source_id):
                # 无效（0）源不进回调，交给 payload 层回退到 RTP 提取的 Radio ID。
                context["source_id"] = source_id
                context["observed_source_id"] = source_id
            context["request_id"] = latest_with_ids.get("request_id")
            context["context_method"] = "recent_adk_event"
            context["context_confidence"] = float(latest_with_ids.get("context_confidence") or 0.8)
            evidence_id = latest_with_ids.get("domain_event_id")
            if evidence_id:
                context["evidence_event_ids"] = [evidence_id]
            # 需求 §35 场景 5：target/group 均缺失时由智能体侧补
            # target=LOCAL_CONTROL_ID。网关不得把 source 杜撰成 target——
            # 否则说话终端会被误判为接收方，消息被归类为 user_voice
            # 而拒绝进入 Planner（capabilities 为空）。
            if "GROUP" in option:
                if _valid_id(target_id):
                    context["group_id"] = target_id
            elif _valid_id(target_id):
                context["target_id"] = target_id
                context["observed_target_id"] = target_id
        if context.get("channel_id") is None:
            context["channel_id"] = getattr(self.backend, "selected_channel", None)
        return {k: v for k, v in context.items() if v is not None}

    # ---- 状态查询 --------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """返回服务健康状态信息。"""
        with self._lock:
            errors = list(self._errors)[-20:]
        return {
            "started": self.started_at is not None,
            "started_at": self.started_at,
            "transport": self.backend.transport_state(),
            "audio_devices": (
                self.backend.audio_devices()
                if self.started_at is not None
                else {"input": 0, "output": 0}
            ),
            "server_runtime": {
                "event_pump_alive": (
                    self._pump_thread is not None and self._pump_thread.is_alive()
                ),
                "watchdog_alive": (
                    self._watchdog_thread is not None and self._watchdog_thread.is_alive()
                ),
                "last_event_at": self.last_event_at,
                "last_watchdog_at": self.last_watchdog_at,
                "watchdog_restarts": self.watchdog_restarts,
            },
            "errors": errors,
        }
