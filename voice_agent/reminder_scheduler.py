# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：voice_agent/reminder_scheduler.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：.logging_utils, .terminal_delivery, .mcp_result, .request_ids, .operations.models
# 主要公开符号：configure_operation_runtime, ReminderScheduler
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""待办提醒定时调度器。

当语音意图分类检测到 TODO_REMINDER 且同时包含时间点和事项描述时，
本模块负责在事件发生前 1 分钟自动发送提醒短信到来源终端。

示例："提醒我10分钟后开会" → 立即回复"待办提醒已设置：10分钟后开会"，9分钟后自动发送"1分钟后开会"

可靠性保障：
- 调度信息持久化到 JSON 文件，进程重启后自动恢复未完成的提醒
- 发送失败时自动重试（最多 3 次，间隔 5 秒）
- 已过期的未发送提醒在启动时立即补发
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .logging_utils import log_event
from .terminal_delivery import deliver_mcp_result_to_terminal
from .mcp_result import build_mcp_result
from .request_ids import next_voice_request_id
from .operations.models import AtomicOperation

_OPERATION_RUNTIME = None

def configure_operation_runtime(runtime: Any) -> None:
    global _OPERATION_RUNTIME
    _OPERATION_RUNTIME = runtime


logger = logging.getLogger(__name__)

# 持久化文件路径（与 voice_agent 同目录）
_PERSIST_FILE = Path(__file__).resolve().parent / "reminder_tasks.json"
_MAX_RETRIES = 3
_RETRY_INTERVAL_SECONDS = 5.0
# 提前提醒时间：在事件发生前 1 分钟发送提醒短信
_REMINDER_LEAD_SECONDS = 60
# 已发送记录的幂等窗口：窗口内同一请求或同一事项不再重复调度。
# 同一回调被重复处理时，首条提醒可能已发完（状态变 sent），
# 仅查 pending 会让第二次处理穿透去重，因此 sent 记录在窗口内同样视为重复。
_SENT_DEDUP_WINDOW_SECONDS = 600


class ReminderScheduler:
    """管理待办提醒的定时发送任务（含持久化和重试）。"""

    def __init__(self) -> None:
        self._timers: list[threading.Timer] = []
        self._lock = threading.Lock()
        self._history: list[dict[str, Any]] = []
        self._load_persisted()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load_persisted(self) -> None:
        """从磁盘加载历史提醒记录。"""
        try:
            if _PERSIST_FILE.exists():
                data = json.loads(_PERSIST_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._history = data
        except Exception as exc:
            logger.warning("加载提醒持久化文件失败: %s", exc)

    def _save_persisted(self) -> None:
        """将当前历史写入磁盘（仅保留最近 50 条）。"""
        try:
            records = self._history[-50:]
            _PERSIST_FILE.write_text(
                json.dumps(records, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("保存提醒持久化文件失败: %s", exc)

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------

    def schedule_reminder(
        self,
        *,
        task: str,
        time_desc: str,
        target_datetime: str,
        terminal_id: int,
        channel_id: int | None = None,
        request_id: int | str | None = None,
        origin_command_id: str | None = None,
    ) -> dict[str, Any]:
        """调度一个定时待办提醒（幂等：同一请求或同一事项不重复调度）。

        Args:
            task: 待办事项描述（如"开会"、"巡逻"）
            time_desc: 时间可读描述（如"20分钟后"、"15点"）
            target_datetime: 目标发送时间 "YYYY-MM-DD HH:MM"
            terminal_id: 接收提醒的终端 Radio ID
            channel_id: 可选的信道 ID
            request_id: 触发本次调度的语音请求 ID（幂等去重主键）

        Returns:
            调度结果字典
        """
        try:
            target_dt = datetime.strptime(target_datetime, "%Y-%m-%d %H:%M")
        except (ValueError, TypeError) as exc:
            return {
                "ok": False,
                "error": f"无效的目标时间: {target_datetime} ({exc})",
            }

        now = datetime.now()
        total_delay = (target_dt - now).total_seconds()
        if total_delay < 0:
            return {
                "ok": False,
                "error": f"目标时间已过: {target_datetime}",
            }

        # 幂等去重：同一语音请求或相同事项+时间+终端的 pending 提醒只调度一次，
        # 避免同一句话被重复处理时提醒短信发两次。
        with self._lock:
            duplicate = self._find_duplicate(task, target_datetime, terminal_id, request_id)
        if duplicate is not None:
            log_event(
                logger,
                "待办提醒重复调度已跳过",
                task=task,
                time_desc=time_desc,
                target_datetime=target_datetime,
                terminal_id=terminal_id,
                request_id=request_id,
                existing_scheduled_at=duplicate.get("scheduled_at"),
            )
            return {
                "ok": True,
                "scheduled": False,
                "duplicate": True,
                "target_datetime": target_datetime,
                "delay_seconds": duplicate.get("delay_seconds"),
                "sms_content": duplicate.get("sms_content"),
                "terminal_id": terminal_id,
            }

        # 提前 1 分钟发送提醒；若总延迟不足 1 分钟则立即发送
        fire_delay = max(0.1, total_delay - _REMINDER_LEAD_SECONDS)
        # 构建提醒短信内容（极简：仅时间+事件）
        sms_content = f"1分钟后{task}"

        record: dict[str, Any] = {
            "task": task,
            "time_desc": time_desc,
            "target_datetime": target_datetime,
            "terminal_id": terminal_id,
            "channel_id": channel_id,
            "request_id": request_id,
            "origin_command_id": origin_command_id or f"imported:{request_id or target_datetime}",
            "sms_content": sms_content,
            "scheduled_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "delay_seconds": round(fire_delay, 1),
            "status": "pending",
        }

        self._start_timer(record, fire_delay)

        log_event(
            logger,
            "待办提醒已调度",
            task=task,
            time_desc=time_desc,
            target_datetime=target_datetime,
            terminal_id=terminal_id,
            fire_delay_seconds=round(fire_delay, 1),
            sms_content=sms_content,
        )

        return {
            "ok": True,
            "scheduled": True,
            "target_datetime": target_datetime,
            "delay_seconds": round(fire_delay, 1),
            "sms_content": sms_content,
            "terminal_id": terminal_id,
        }

    def _find_duplicate(
        self,
        task: str,
        target_datetime: str,
        terminal_id: int,
        request_id: int | str | None,
    ) -> dict[str, Any] | None:
        """在锁内查找已存在的等价提醒（调用方需持有 self._lock）。

        pending 记录无条件视为重复；sent 记录在幂等窗口内视为重复，
        避免同一回调被重复处理时（首条已发完、状态变 sent）穿透去重再发一次。
        """
        normalized_task = str(task or "").strip()
        now = datetime.now()
        for record in self._history:
            status = record.get("status")
            if status == "pending":
                fresh = True
            elif status == "sent":
                try:
                    scheduled_at = datetime.strptime(
                        str(record.get("scheduled_at") or ""), "%Y-%m-%d %H:%M:%S"
                    )
                except ValueError:
                    continue
                fresh = (now - scheduled_at).total_seconds() <= _SENT_DEDUP_WINDOW_SECONDS
            else:
                continue
            if not fresh:
                continue
            if (
                request_id is not None
                and record.get("request_id") is not None
                and record.get("request_id") == request_id
            ):
                return record
            if (
                record.get("task") == normalized_task
                and record.get("target_datetime") == target_datetime
                and record.get("terminal_id") == terminal_id
            ):
                return record
        return None

    def _start_timer(self, record: dict[str, Any], delay_seconds: float) -> None:
        """创建并启动一个定时器。"""

        def _fire() -> None:
            self._execute_reminder(record)

        timer = threading.Timer(max(0.1, delay_seconds), _fire)
        timer.daemon = True
        timer.name = f"todo-reminder-{record.get('task', '')[:10]}-{record.get('target_datetime', '')}"

        with self._lock:
            self._timers.append(timer)
            if record not in self._history:
                self._history.append(record)
            # 清理已完成的旧定时器
            self._timers = [t for t in self._timers if t.is_alive()]
            self._save_persisted()

        timer.start()

    # ------------------------------------------------------------------
    # 执行（含重试）
    # ------------------------------------------------------------------

    def _execute_reminder(self, record: dict[str, Any]) -> None:
        """定时器到期后执行：发送提醒短信到终端（失败自动重试）。

        注意：channel_id 刻意传 None，让 ADK 网关自动发现终端当前信道。
        创建提醒时记录的 channel_id 可能已过期（终端切换信道），
        与确认短信（nodes.py schedule_reminder）保持一致的投递策略。
        """
        # 原子状态转换 pending → sending：同一条记录即使被多个定时器/恢复路径
        # 重复触发，也只会有一个执行者真正发送。
        with self._lock:
            if record.get("status") != "pending":
                log_event(
                    logger,
                    "待办提醒重复触发已跳过发送",
                    task=record.get("task"),
                    terminal_id=record.get("terminal_id"),
                    status=record.get("status"),
                )
                return
            record["status"] = "sending"

        terminal_id = record.get("terminal_id") or 0
        sms_content = record.get("sms_content") or ""

        log_event(
            logger,
            "待办提醒触发，准备发送短信",
            task=record.get("task"),
            terminal_id=terminal_id,
            sms_content=sms_content,
        )

        scheduled_command = None
        delivery_operation = None
        if _OPERATION_RUNTIME is not None and getattr(_OPERATION_RUNTIME, "enabled", False):
            scheduled_command = _OPERATION_RUNTIME.create_scheduled_command(
                source_ref=f"reminder:{record.get('request_id') or record.get('target_datetime')}:{terminal_id}",
                actor_id=str(terminal_id),
                parent_command_id=str(record.get("origin_command_id") or "imported:unknown"),
                payload_redacted={
                    "target_datetime": record.get("target_datetime"),
                    "terminal_id": terminal_id,
                    "task_length": len(str(record.get("task") or "")),
                },
            )
            record["scheduled_command_id"] = scheduled_command.command_id
            args = {
                "target_id": str(terminal_id),
                "reminder_ref": str(record.get("request_id") or record.get("target_datetime") or ""),
            }
            delivery_operation = AtomicOperation(
                origin_command_id=scheduled_command.command_id,
                operation_type="reminder.deliver",
                step_id="step-delivery",
                actor_id=str(terminal_id),
                target_type="radio",
                target_id=str(terminal_id),
                arguments=args,
                resolved_arguments=args,
                executor_kind="delivery",
                risk_level="R2",
                side_effect=True,
            )

        def _send_with_retries(_operation: AtomicOperation | None = None) -> dict[str, Any]:
            last_error = ""
            for attempt in range(1, _MAX_RETRIES + 1):
                try:
                    request_id = next_voice_request_id()
                    mcp_result = build_mcp_result(
                        "sms_send",
                        {
                            "target_id": str(terminal_id),
                            "target_type": "private",
                            "content": sms_content,
                        },
                        request_id=request_id,
                    )
                    delivery = deliver_mcp_result_to_terminal(
                        mcp_result,
                        target_id=int(terminal_id),
                        channel_id=None,
                        encode=False,
                    )
                    if delivery.get("ok"):
                        return {"ok": True, "result": delivery, "attempts": attempt}
                    last_error = delivery.get("error") or "gateway returned ok=false"
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                if attempt < _MAX_RETRIES:
                    time.sleep(_RETRY_INTERVAL_SECONDS)
            return {"ok": False, "error": last_error, "attempts": _MAX_RETRIES}

        if delivery_operation is not None:
            audited = _OPERATION_RUNTIME.execute_local(delivery_operation, _send_with_retries)
            outcome = audited.response or {"ok": audited.ok, "error": audited.error}
            record["operation_id"] = delivery_operation.operation_id
        else:
            outcome = _send_with_retries()
        if outcome.get("ok"):
            record["status"] = "sent"
            record["delivery_result"] = outcome.get("result")
            record["sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self._lock:
                self._save_persisted()
            return
        last_error = str(outcome.get("error") or "reminder delivery failed")
        # 所有重试均失败
        record["status"] = "failed"
        record["error"] = last_error
        record["failed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_event(
            logger,
            "待办提醒短信最终发送失败",
            task=record.get("task"),
            terminal_id=terminal_id,
            error=last_error,
        )
        with self._lock:
            self._save_persisted()

    # ------------------------------------------------------------------
    # 启动恢复
    # ------------------------------------------------------------------

    def recover_pending(self) -> int:
        """进程启动时恢复未完成的提醒。

        - 目标时间未到：重新调度定时器
        - 目标时间已过且未发送：立即补发

        Returns:
            恢复的提醒数量
        """
        now = datetime.now()
        recovered = 0
        with self._lock:
            pending = [r for r in self._history if r.get("status") == "pending"]

        # 恢复去重：历史上因重复处理产生的等价 pending 记录只恢复首条，
        # 其余标记为取消，避免同一提醒补发两次。
        seen_keys: set[tuple[str, str, object]] = set()
        for record in pending:
            key = (
                str(record.get("task") or "").strip(),
                str(record.get("target_datetime") or ""),
                record.get("terminal_id"),
            )
            if key in seen_keys:
                record["status"] = "cancelled_duplicate"
                log_event(
                    logger,
                    "重复待办提醒已取消",
                    task=record.get("task"),
                    target_datetime=record.get("target_datetime"),
                    terminal_id=record.get("terminal_id"),
                )
                continue
            seen_keys.add(key)

            target_str = record.get("target_datetime") or ""
            try:
                target_dt = datetime.strptime(target_str, "%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                record["status"] = "expired_invalid_time"
                continue

            # 提醒触发时间 = 事件时间 - 提前量
            fire_delay = (target_dt - now).total_seconds() - _REMINDER_LEAD_SECONDS
            if fire_delay > 0:
                # 还未到提醒时间，重新调度
                self._start_timer(record, fire_delay)
                log_event(
                    logger,
                    "待办提醒已恢复调度",
                    task=record.get("task"),
                    target_datetime=target_str,
                    fire_delay_seconds=round(fire_delay, 1),
                )
            else:
                # 已到或已过提醒时间，立即补发
                log_event(
                    logger,
                    "待办提醒已到期，立即补发",
                    task=record.get("task"),
                    target_datetime=target_str,
                )
                self._execute_reminder(record)
            recovered += 1

        if recovered:
            log_event(logger, "待办提醒恢复完成", recovered_count=recovered)
        if any(r.get("status") == "cancelled_duplicate" for r in pending):
            with self._lock:
                self._save_persisted()
        return recovered

    # ------------------------------------------------------------------
    # 查询与管理
    # ------------------------------------------------------------------

    def get_pending(self) -> list[dict[str, Any]]:
        """获取所有待执行的提醒任务。"""
        with self._lock:
            return [r for r in self._history if r.get("status") == "pending"]

    def get_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """获取最近的提醒历史。"""
        with self._lock:
            return list(self._history[-limit:])

    def cancel_all(self) -> int:
        """取消所有待执行的提醒。"""
        with self._lock:
            count = 0
            for timer in self._timers:
                if timer.is_alive():
                    timer.cancel()
                    count += 1
            for record in self._history:
                if record.get("status") == "pending":
                    record["status"] = "cancelled"
            self._timers.clear()
            self._save_persisted()
            return count


# 全局单例
REMINDER_SCHEDULER = ReminderScheduler()
