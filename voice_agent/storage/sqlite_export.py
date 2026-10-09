# =============================================================================
# 【维护注释】文件：voice_agent/storage/sqlite_export.py
# 所属层次：持久化抽象层
# 主要职责：定义存储接口、SQLite/PostgreSQL 切换、归档、对象存储、保留期和读路径灰度。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：normalize_sqlite_record
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Normalize legacy SQLite rows into the same canonical records used by dual-write."""
from __future__ import annotations
import json
from typing import Any

def _loads(value: Any, default: Any):
    if value in (None,""): return default
    if isinstance(value,(dict,list)): return value
    try: return json.loads(str(value))
    except (ValueError,TypeError): return default

def normalize_sqlite_record(kind: str, row: dict[str,Any]) -> dict[str,Any]:
    d=dict(row); k=str(kind)
    if k=="event":
        return dict(_loads(d.get("payload_json"),{}))
    if k=="operation":
        d["arguments"]=_loads(d.pop("arguments_json",None),{})
        d["resolved_arguments"]=_loads(d.pop("resolved_arguments_json",None),{})
        d["depends_on"]=_loads(d.pop("depends_on_json",None),[])
        d["side_effect"]=bool(d.get("side_effect"))
        d.pop("arguments_hash",None); d.pop("final_result_json",None)
        return d
    if k=="command":
        d["payload_redacted"]=_loads(d.pop("payload_redacted_json",None),{})
        return d
    if k=="memory":
        d["object"]=_loads(d.pop("object_json",None),None)
        d["evidence_event_ids"]=_loads(d.pop("evidence_event_ids_json",None),[])
        d["provenance"]=_loads(d.pop("provenance_json",None),{})
        return d
    if k=="context_snapshot":
        d["item_ids"]=_loads(d.pop("item_ids_json",None),[])
        d["event_ids"]=_loads(d.pop("event_ids_json",None),[])
        d["memory_ids"]=_loads(d.pop("memory_ids_json",None),[])
        d["context"]=_loads(d.pop("context_json",None),{})
        return d
    return d
