# =============================================================================
# 【维护注释】文件：voice_agent/operations/schema.py
# 所属层次：原子操作执行层
# 主要职责：负责把计划步骤固化为可审计、可幂等、可重试的原子操作并记录执行结果。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：无；以导入副作用、常量、数据模型或包导出为主
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""SQLite schema shared by command and operation repositories."""

SCHEMA_SQL = r"""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS operation_migration_version (
    component TEXT NOT NULL,
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY(component, version)
);
CREATE TABLE IF NOT EXISTS command_record (
    command_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT,
    actor_id TEXT,
    raw_text TEXT,
    payload_redacted_json TEXT NOT NULL,
    status TEXT NOT NULL,
    trace_id TEXT,
    correlation_id TEXT,
    parent_command_id TEXT,
    cancel_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(source_type, source_ref)
);
CREATE INDEX IF NOT EXISTS idx_command_actor_status ON command_record(actor_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_command_source_ref ON command_record(source_type, source_ref);
CREATE TABLE IF NOT EXISTS operation_record (
    operation_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    origin_command_id TEXT NOT NULL,
    operation_type TEXT NOT NULL,
    step_id TEXT NOT NULL,
    actor_id TEXT,
    target_type TEXT,
    target_id TEXT,
    arguments_json TEXT NOT NULL,
    resolved_arguments_json TEXT NOT NULL,
    arguments_hash TEXT NOT NULL,
    executor_kind TEXT NOT NULL,
    tool_name TEXT,
    risk_level TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    depends_on_json TEXT NOT NULL,
    status TEXT NOT NULL,
    timeout_seconds REAL NOT NULL,
    max_retries INTEGER NOT NULL,
    side_effect INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    final_result_json TEXT,
    FOREIGN KEY(origin_command_id) REFERENCES command_record(command_id)
);
CREATE INDEX IF NOT EXISTS idx_operation_command ON operation_record(origin_command_id, created_at);
CREATE INDEX IF NOT EXISTS idx_operation_actor_status ON operation_record(actor_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_operation_target_status ON operation_record(target_id, status, created_at DESC);
CREATE TABLE IF NOT EXISTS operation_attempt (
    attempt_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    executor_mode TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    accepted INTEGER,
    dispatched INTEGER,
    device_confirmed INTEGER,
    response_json TEXT,
    error_code TEXT,
    error_message TEXT,
    FOREIGN KEY(operation_id) REFERENCES operation_record(operation_id),
    UNIQUE(operation_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS idx_attempt_operation ON operation_attempt(operation_id, attempt_no);
CREATE TABLE IF NOT EXISTS wait_condition (
    wait_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL CHECK(length(trim(subject_id)) > 0),
    predicate TEXT NOT NULL,
    status TEXT NOT NULL,
    timeout_at TEXT,
    created_at TEXT NOT NULL,
    satisfied_at TEXT,
    FOREIGN KEY(operation_id) REFERENCES operation_record(operation_id)
);
CREATE INDEX IF NOT EXISTS idx_wait_subject ON wait_condition(subject_type, subject_id, status);
CREATE TABLE IF NOT EXISTS operation_transition (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT,
    occurred_at TEXT NOT NULL,
    FOREIGN KEY(operation_id) REFERENCES operation_record(operation_id)
);
CREATE INDEX IF NOT EXISTS idx_transition_operation ON operation_transition(operation_id, transition_id);
"""
