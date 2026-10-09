# =============================================================================
# 【维护注释】文件：voice_agent/storage/retention.py
# 所属层次：持久化抽象层
# 主要职责：定义存储接口、SQLite/PostgreSQL 切换、归档、对象存储、保留期和读路径灰度。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：RetentionPolicy, due_for_deletion
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Retention/deletion policy primitives. Legal hold always wins over age policies."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

@dataclass(frozen=True)
class RetentionPolicy:
    category: str
    days: int
    delete_object: bool=True
    delete_text: bool=True


def due_for_deletion(record: dict[str,Any], policy: RetentionPolicy, *, now: datetime|None=None) -> bool:
    if bool(record.get("legal_hold")): return False
    if int(policy.days)<=0: return False
    raw=record.get("created_at") or record.get("occurred_at") or record.get("updated_at")
    if not raw: return False
    try: created=datetime.fromisoformat(str(raw).replace("Z","+00:00"))
    except ValueError: return False
    reference=now or datetime.now(timezone.utc)
    if created.tzinfo is None: created=created.replace(tzinfo=timezone.utc)
    return created <= reference-timedelta(days=int(policy.days))
