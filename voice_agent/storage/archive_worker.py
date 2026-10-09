# =============================================================================
# 【维护注释】文件：voice_agent/storage/archive_worker.py
# 所属层次：持久化抽象层
# 主要职责：定义存储接口、SQLite/PostgreSQL 切换、归档、对象存储、保留期和读路径灰度。
# 项目内直接依赖：.object_store, .runtime
# 主要公开符号：ObjectArchiveWorker, build_archive_worker
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Asynchronous post-spool object archival. Local audio is deleted only after verified upload."""
from __future__ import annotations
import logging, queue, threading, time
from datetime import datetime,timezone
from typing import Any
from .object_store import FilesystemObjectStore,S3ObjectStore
from .runtime import mirror_record

logger=logging.getLogger(__name__)

class ObjectArchiveWorker:
    def __init__(self, store: Any|None, *, enabled: bool, queue_size:int=1024) -> None:
        self.store=store; self.enabled=bool(enabled and store is not None); self.queue=queue.Queue(maxsize=max(1,int(queue_size))); self.stop_event=threading.Event(); self.thread=None; self.archived=0; self.failed=0; self.last_error=None
        if self.enabled:
            self.thread=threading.Thread(target=self._run,name='object-archive-worker',daemon=True); self.thread.start()
    def submit(self, record: Any) -> bool:
        if not self.enabled: return False
        try: self.queue.put_nowait(record); return True
        except queue.Full:
            self.failed+=1; self.last_error='archive queue full'; logger.error('object_archive_queue_full event_id=%s',getattr(record,'event_id',None)); return False
    def _run(self):
        while not self.stop_event.is_set():
            try: record=self.queue.get(timeout=.5)
            except queue.Empty: continue
            try: self.archive(record)
            except Exception as exc:
                self.failed+=1; self.last_error=f'{type(exc).__name__}: {exc}'; logger.exception('object_archive_failed event_id=%s',getattr(record,'event_id',None))
            finally: self.queue.task_done()
    def archive(self,record:Any):
        if hasattr(self.store, 'put_file'):
            ref=self.store.put_file(record.spool_path,prefix='audio',checkpoint_path=str(record.spool_path)+'.multipart.json')
        else:
            data=record.spool_path.read_bytes(); ref=self.store.put_bytes(data,prefix='audio')
        metadata=dict(getattr(record,'metadata',{}) or {}); terminal=str(metadata.get('terminal_id') or metadata.get('source_id') or '').strip() or None
        manifest={'schema_version':1,'object_id':f'audio:{record.event_id}','object_key':ref.key,'sha256':ref.sha256,'byte_size':ref.size,'encryption':ref.encryption,'category':'audio','source_event_id':record.event_id,'terminal_id':terminal,'created_at':datetime.now(timezone.utc).isoformat()}
        mirror_record('object_manifest',manifest['object_id'],manifest,terminal_id=terminal)
        # Upload + metadata/hash verification completed inside the store before local deletion.
        record.spool_path.unlink(missing_ok=True); self.archived+=1; return manifest
    def metrics(self): return {'enabled':self.enabled,'queue_depth':self.queue.qsize(),'queue_max':self.queue.maxsize,'archived':self.archived,'failed':self.failed,'last_error':self.last_error}
    def close(self):
        self.stop_event.set()
        if self.thread is not None: self.thread.join(timeout=2); self.thread=None

def build_archive_worker(*, enabled:bool,bucket:str,prefix:str='audio',endpoint_url:str='',kms_key_id:str='',queue_size:int=1024,filesystem_root:str='') -> ObjectArchiveWorker:
    if not enabled: return ObjectArchiveWorker(None,enabled=False)
    if filesystem_root:
        store=FilesystemObjectStore(filesystem_root)
    else:
        store=S3ObjectStore(bucket=bucket,prefix=prefix,endpoint_url=endpoint_url or None,kms_key_id=kms_key_id or None)
    return ObjectArchiveWorker(store,enabled=True,queue_size=queue_size)
