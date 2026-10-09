# =============================================================================
# 【维护注释】文件：voice_agent/messages/service.py
# 所属层次：消息归档服务门面
# 主要职责：分类（内存）→ 端点补全 → 异步归档提交；出站消息 source 固定
#           LOCAL_CONTROL_ID；direct-text 测试语义（需求 §3/§25）。
# 项目内直接依赖：..config, .classification, .metrics, .store, .writer
# 主要公开符号：MessageArchiveService, get_message_archive_service
# 兼容性：submit_* 只做内存入队（archive_enqueue_ms 指标）；数据库写入在后台线程。
# 修改原则：中转台主动下发消息 source_id 永远 = LOCAL_CONTROL_ID；origin 仅审计。
# 排障顺序：先看 classify_and_annotate 返回值，再看 ArchiveWriter 指标/告警。
# =============================================================================
"""消息归档服务（分类 + 端点补全 + 异步归档）。"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from shared_contracts.control_ids import IdentityConfig, build_identity_config
from shared_runtime.json_utils import canonical_json

from .classification import (
    CATEGORY_CONTROL_SMS,
    CATEGORY_CONTROL_VOICE,
    CATEGORY_USER_SMS,
    CATEGORY_USER_VOICE,
    MEDIUM_SMS,
    MEDIUM_VOICE,
    ClassificationResult,
    MessageClassifier,
)
from .metrics import MessageMetrics
from .store import ArchiveRecord, MessageArchiveStore
from .writer import ArchiveWriter

logger = logging.getLogger(__name__)

STATUS_ASR_FAILED = "asr_failed"
STATUS_ARCHIVED = "archived"
STATUS_REJECTED_ENDPOINT_INCOMPLETE = "endpoint_incomplete"

DIRECTION_INBOUND = "inbound"
DIRECTION_OUTBOUND = "outbound"

TEXT_SOURCE_ASR = "asr"
TEXT_SOURCE_SMS = "sms"

# direct-text / direct-sms 测试入口默认身份（需求 §25）。
DEFAULT_DIRECT_TEXT_SOURCE_ID = "5"


def _now_iso() -> str:
    from shared_contracts.models import utc_now_iso

    return utc_now_iso()


class MessageArchiveService:
    """统一分类与异步归档入口。

    - ``classify_and_annotate``：同步、纯内存（需求 §9），返回带
      ``message_category`` / ``endpoint_complete`` 等字段的可注入 data。
    - ``submit_voice_result`` / ``submit_sms_event`` / ``submit_outbound_sms``：
      只入队，不等待 SQLite（需求 §14）。
    """

    def __init__(
        self,
        *,
        base_dir: Any,
        identity: IdentityConfig | None = None,
        enabled: bool = True,
        queue_size: int = 1024,
        direct_text_source_id: str = DEFAULT_DIRECT_TEXT_SOURCE_ID,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.identity = identity or build_identity_config()
        self.classifier = MessageClassifier(self.identity)
        self.metrics = MessageMetrics()
        self.enabled = bool(enabled)
        self.direct_text_source_id = str(direct_text_source_id)
        self.store = MessageArchiveStore(base_dir, busy_timeout_ms=busy_timeout_ms)
        self.writer = ArchiveWriter(
            self.store, self.metrics, queue_size=queue_size
        )
        if self.enabled:
            self.writer.start()

    # ---- 停机 -----------------------------------------------------------
    def stop(self, *, timeout: float = 5.0) -> None:
        self.writer.flush(timeout=timeout)
        self.writer.stop(timeout=timeout)

    # ---- 分类（同步热路径，纯内存） --------------------------------------
    def classify_and_annotate(
        self,
        data: dict[str, Any],
        *,
        medium: str = MEDIUM_VOICE,
        complete_direct_text: bool = False,
        default_target_to_local_control: bool = False,
    ) -> ClassificationResult:
        """对回调 data 做端点补全 + 分类，并把结果字段写回 data。

        - ``complete_direct_text=True``（/text/input、/sms/callback 等命令入口）：
          缺省 source 取 ``direct_text_source_id``，缺省 target 取 LOCAL_CONTROL_ID
          （需求 §25：测试控制入口具有明确控制上下文）。
        - ``default_target_to_local_control=True``（网关语音/短信回调链路）：
          回调到达本系统即意味着接收方是中转台自己，target 与 group 均缺失时
          补 target=LOCAL_CONTROL_ID（需求 §35 场景 1/5：入站私语音/私短信
          终端→中转台）；组呼回调带 group_id，不会命中此回退。
        - 其余情况：不做身份杜撰；缺失即 endpoint_complete=False（需求 §8）。
        """
        with self.metrics.time_block("classify_ms"):
            self.metrics.inc("classify_total")
            source_id = data.get("source_id")
            target_id = data.get("target_id")
            group_id = data.get("group_id")
            origin = data.get("origin")
            if complete_direct_text:
                if source_id is None or str(source_id).strip() == "":
                    source_id = self.direct_text_source_id
                if target_id is None or str(target_id).strip() == "":
                    target_id = self.identity.local_control_id
            elif (
                default_target_to_local_control
                and group_id is None
                and (target_id is None or str(target_id).strip() == "")
            ):
                # 需求 §35 场景 5：入站私语音，接收方为中转台（LOCAL_CONTROL_ID）。
                target_id = self.identity.local_control_id
            result = self.classifier.classify(
                medium=medium,
                source_id=source_id,
                target_id=target_id,
                group_id=group_id,
                origin=origin,
            )
        if not result.endpoint_complete:
            self.metrics.inc("endpoint_incomplete_count")
        data["message_category"] = result.category
        data["message_medium"] = result.medium
        data["endpoint_complete"] = result.endpoint_complete
        data["classification_reason"] = result.classification_reason
        data["source_id"] = result.source_id
        data["target_id"] = result.target_id
        if result.group_id is not None:
            data["group_id"] = result.group_id
        return result

    # ---- 归档提交（只入队） ----------------------------------------------
    def _enqueue(self, record: ArchiveRecord) -> bool:
        if not self.enabled:
            return False
        enqueue_started = time.perf_counter()
        with self.metrics.time_block("archive_enqueue_ms"):
            self.metrics.inc("archive_enqueue_total")
            accepted = self.writer.submit(record)
        archive_enqueue_ms = round((time.perf_counter() - enqueue_started) * 1000.0, 3)
        logger.info(
            "message_archive_enqueue_timing category=%s event_id=%s archive_enqueue_ms=%.3f accepted=%s",
            record.category, record.event_id, archive_enqueue_ms, accepted,
        )
        if not accepted:
            logger.error(
                "message_archive_enqueue_rejected category=%s event_id=%s",
                record.category,
                record.event_id,
            )
        return accepted

    def _base_record(
        self,
        result: ClassificationResult,
        data: dict[str, Any],
        *,
        text: str | None,
        text_source: str,
        status: str,
        direction: str,
    ) -> ArchiveRecord:
        return ArchiveRecord(
            category=result.category,
            medium=result.medium,
            source_id=result.source_id,
            target_id=result.target_id,
            group_id=result.group_id,
            channel_id=data.get("channel_id"),
            event_id=(
                str(data.get("accepted_event_id") or data.get("event_id") or "") or None
            ),
            request_id=(
                str(data.get("request_id") or data.get("trace_id") or "") or None
            ),
            session_id=(
                str(
                    data.get("capture_session_id")
                    or data.get("session_id")
                    or ""
                )
                or None
            ),
            call_id=(str(data.get("call_id") or "") or None),
            direction=direction,
            classification_reason=result.classification_reason,
            origin=result.origin,
            text=text,
            text_source=text_source,
            status=status,
            endpoint_complete=result.endpoint_complete,
            occurred_at=(
                str(data.get("occurred_at") or data.get("created_at") or "") or None
            ),
            recorded_at=_now_iso(),
            metadata={
                key: data[key]
                for key in ("input_mode", "asr_model", "call_type", "origin")
                if data.get(key) is not None
            },
        )

    def submit_voice_result(
        self,
        result: ClassificationResult,
        data: dict[str, Any],
        asr: dict[str, Any] | None,
        *,
        request_id: Any = None,
    ) -> bool:
        """语音处理完成后归档（含 ASR 文本；语音库只存文本，需求 §13）。

        语音文本只有 ASR 完成后才存在，因此归档在 process() 返回后提交；
        回调 spool 仍是崩溃恢复源（需求 §15），提交本身只做内存入队。
        """
        asr = asr or {}
        asr_ok = bool(asr.get("ok"))
        text = str(asr.get("text") or "") or None
        status = STATUS_ARCHIVED if asr_ok else STATUS_ASR_FAILED
        record = self._base_record(
            result,
            {**data, "request_id": request_id or data.get("request_id")},
            text=text,
            text_source=TEXT_SOURCE_ASR,
            status=status,
            direction=DIRECTION_INBOUND,
        )
        return self._enqueue(record)

    def submit_sms_event(
        self,
        result: ClassificationResult,
        data: dict[str, Any],
        *,
        text: str,
        request_id: Any = None,
        status: str = STATUS_ARCHIVED,
    ) -> bool:
        """入站短信归档（无需 ASR，先归档再进入处理链，需求 §14）。"""
        record = self._base_record(
            result,
            {**data, "request_id": request_id or data.get("request_id")},
            text=text,
            text_source=TEXT_SOURCE_SMS,
            status=status,
            direction=DIRECTION_INBOUND,
        )
        return self._enqueue(record)

    def submit_outbound_sms(
        self,
        *,
        target_id: Any,
        content: str,
        target_type: str | None = None,
        request_id: Any = None,
        ok: bool = True,
        data: dict[str, Any] | None = None,
    ) -> bool:
        """中转台主动下发短信：source 固定 LOCAL_CONTROL_ID（需求 §3）。"""
        from shared_contracts.control_ids import normalize_endpoint_id

        data = data or {}
        target = normalize_endpoint_id(target_id)
        is_group = str(target_type or "").strip().lower() in {"group", "all"}
        group = target if is_group else None
        with self.metrics.time_block("classify_ms"):
            self.metrics.inc("classify_total")
            result = self.classifier.classify(
                medium=MEDIUM_SMS,
                source_id=self.identity.local_control_id,
                target_id=target,
                group_id=group,
                origin="agent",
            )
        if not result.endpoint_complete:
            self.metrics.inc("endpoint_incomplete_count")
        record = ArchiveRecord(
            category=result.category,
            medium=MEDIUM_SMS,
            source_id=self.identity.local_control_id,
            target_id=result.target_id,
            group_id=result.group_id,
            channel_id=data.get("channel_id"),
            event_id=(str(data.get("event_id") or "") or None),
            request_id=(str(request_id) if request_id is not None else None),
            direction=DIRECTION_OUTBOUND,
            classification_reason=result.classification_reason,
            origin="agent",
            text=content,
            text_source=TEXT_SOURCE_SMS,
            status=STATUS_ARCHIVED if ok else "send_failed",
            endpoint_complete=result.endpoint_complete,
            occurred_at=_now_iso(),
            recorded_at=_now_iso(),
            metadata={"target_type": target_type} if target_type else {},
        )
        return self._enqueue(record)

    # ---- 观测 -----------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "identity": self.identity.as_dict(),
            "metrics": self.metrics.snapshot(),
            "stores": self.store.stats(),
        }


_DEFAULT_SERVICE: MessageArchiveService | None = None
_DEFAULT_SERVICE_LOCK = threading.Lock()


def get_message_archive_service() -> MessageArchiveService:
    """进程级单例（默认配置来自 voice_agent.config）。"""
    global _DEFAULT_SERVICE
    with _DEFAULT_SERVICE_LOCK:
        if _DEFAULT_SERVICE is None:
            from .. import config

            _DEFAULT_SERVICE = MessageArchiveService(
                base_dir=config.MESSAGE_ARCHIVE_DIR,
                identity=config.MESSAGE_IDENTITY,
                enabled=config.MESSAGE_ARCHIVE_ENABLE,
                queue_size=config.MESSAGE_ARCHIVE_QUEUE_SIZE,
                direct_text_source_id=config.DIRECT_TEXT_DEFAULT_SOURCE_ID,
            )
        return _DEFAULT_SERVICE


def reset_message_archive_service_for_tests() -> None:
    """测试钩子：释放单例（会停止后台写线程）。"""
    global _DEFAULT_SERVICE
    with _DEFAULT_SERVICE_LOCK:
        if _DEFAULT_SERVICE is not None:
            try:
                _DEFAULT_SERVICE.stop(timeout=2.0)
            except Exception:
                pass
            _DEFAULT_SERVICE = None
