# =============================================================================
# 【维护注释】文件：voice_agent/storage/read_canary.py
# 所属层次：持久化抽象层
# 主要职责：定义存储接口、SQLite/PostgreSQL 切换、归档、对象存储、保留期和读路径灰度。
# 项目内直接依赖：.cutover
# 主要公开符号：CanaryReadProxy
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Fail-safe deterministic read canary. Writes always stay on the primary repository."""
from __future__ import annotations
import logging
from typing import Any
from .cutover import choose_postgres_read
logger=logging.getLogger(__name__)

class CanaryReadProxy:
    READ_METHODS={"recent_calls","active_call","recent_operations","similar_outcomes","open_tasks","entity_timeline","verified_memories","get_snapshot","replay","get","query","search","list_sessions","get_call"}
    def __init__(self,primary:Any,secondary:Any,*,percent:int): self.primary=primary; self.secondary=secondary; self.percent=max(0,min(int(percent),100))
    def __getattr__(self,name:str):
        p=getattr(self.primary,name)
        if name not in self.READ_METHODS or not callable(p): return p
        s=getattr(self.secondary,name,None)
        if not callable(s): return p
        def call(*args,**kwargs):
            stable=kwargs.get('snapshot_id') or kwargs.get('terminal_id') or kwargs.get('actor_id') or kwargs.get('memory_id') or kwargs.get('operation_id') or (args[0] if args else '')
            if not choose_postgres_read(f"{name}:{stable}",self.percent): return p(*args,**kwargs)
            try: return s(*args,**kwargs)
            except Exception as exc:
                logger.warning('postgres_canary_read_failed method=%s error=%s fallback=sqlite',name,f'{type(exc).__name__}: {exc}')
                return p(*args,**kwargs)
        return call
