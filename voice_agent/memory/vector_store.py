# =============================================================================
# 【维护注释】文件：voice_agent/memory/vector_store.py
# 所属层次：长期记忆层
# 主要职责：负责记忆候选、检索、验证、向量/全文索引与生命周期；记忆不能直接触发设备命令。
# 项目内直接依赖：voice_agent.storage.interfaces
# 主要公开符号：PgVectorMemoryStore
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Optional pgvector semantic search with mandatory structured scope filtering."""
from __future__ import annotations
from typing import Any, Sequence
from voice_agent.storage.interfaces import RepositoryScope

class PgVectorMemoryStore:
    def __init__(self,pool:Any,*,dimensions:int=1536,use_approximate:bool=False) -> None:
        self.pool=pool; self.dimensions=int(dimensions); self.use_approximate=bool(use_approximate)
    @staticmethod
    def _literal(vector: Sequence[float]) -> str:
        return "["+",".join(f"{float(x):.9g}" for x in vector)+"]"
    def search(self, embedding: Sequence[float], *, scope: RepositoryScope, limit:int=10, namespace:str|None=None) -> list[dict[str,Any]]:
        if len(embedding)!=self.dimensions: raise ValueError("embedding dimension mismatch")
        s=scope.normalized()
        if not s.tenant_id: raise PermissionError("vector search requires tenant scope")
        clauses=["m.tenant_id=%s","m.verification_status='VERIFIED'","(m.expires_at IS NULL OR m.expires_at>clock_timestamp())"]
        params:[Any]=[s.tenant_id]
        if s.actor_id: clauses.append("(m.actor_id IS NULL OR m.actor_id=%s)"); params.append(s.actor_id)
        if s.terminal_id: clauses.append("(m.terminal_id IS NULL OR m.terminal_id=%s)"); params.append(s.terminal_id)
        if namespace: clauses.append("m.namespace=%s"); params.append(str(namespace))
        # Authorization predicates are explicit SQL predicates and RLS also applies.  ANN candidates are never accepted without this recheck.
        sql="SELECT m.memory_id,m.summary,m.confidence,(e.embedding <=> %s::vector) AS distance FROM memory_embedding e JOIN memory_item m USING(memory_id) WHERE "+" AND ".join(clauses)+" ORDER BY e.embedding <=> %s::vector LIMIT %s"
        literal=self._literal(embedding); values=[literal,*params,literal,max(1,min(int(limit),100))]
        with self.pool.connection(s) as conn, conn.cursor() as cur:
            if self.use_approximate: cur.execute("SET LOCAL hnsw.iterative_scan = strict_order")
            cur.execute(sql,values); rows=cur.fetchall()
        return [{"memory_id":r[0],"summary":r[1],"confidence":float(r[2]),"distance":float(r[3])} for r in rows]
