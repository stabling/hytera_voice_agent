# =============================================================================
# 【维护注释】文件：voice_agent/storage/cutover.py
# 所属层次：持久化抽象层
# 主要职责：定义存储接口、SQLite/PostgreSQL 切换、归档、对象存储、保留期和读路径灰度。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：choose_postgres_read, CutoverGate
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Deterministic read-canary and cutover gate helpers."""
from __future__ import annotations
import hashlib
from dataclasses import dataclass


def choose_postgres_read(key:str,percent:int)->bool:
    p=max(0,min(int(percent),100))
    if p==0:return False
    if p==100:return True
    bucket=int.from_bytes(hashlib.sha256(str(key).encode()).digest()[:4],'big')%100
    return bucket<p

@dataclass(frozen=True)
class CutoverGate:
    reconciliation_ok: bool
    rls_ok: bool
    restore_drill_ok: bool
    outage_buffer_ok: bool
    trace_ok: bool
    def approved(self)->bool: return all((self.reconciliation_ok,self.rls_ok,self.restore_drill_ok,self.outage_buffer_ok,self.trace_ok))
