PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS migration_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS call_session (
    call_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL DEFAULT 1,
    direction TEXT NOT NULL,
    call_type TEXT NOT NULL,
    status TEXT NOT NULL,
    observed_source_id TEXT,
    observed_target_id TEXT,
    resolved_source_id TEXT,
    resolved_target_id TEXT,
    group_id TEXT,
    initial_channel_id TEXT,
    request_id TEXT,
    requested_at TEXT,
    ringing_at TEXT,
    answered_at TEXT,
    first_audio_at TEXT,
    last_audio_at TEXT,
    ended_at TEXT,
    duration_ms INTEGER,
    end_reason TEXT,
    correlation_method TEXT,
    correlation_confidence REAL NOT NULL DEFAULT 1.0,
    provenance TEXT NOT NULL DEFAULT 'observed',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(direction IN ('inbound','outbound','unknown')),
    CHECK(call_type IN ('private','group','standby','broadcast','unknown')),
    CHECK(status IN ('observed','requested','ringing','answered','active','ended','failed','rejected','timeout')),
    CHECK(correlation_confidence >= 0.0 AND correlation_confidence <= 1.0),
    CHECK(duration_ms IS NULL OR duration_ms >= 0)
);

CREATE INDEX IF NOT EXISTS idx_call_session_time
    ON call_session(COALESCE(requested_at, first_audio_at, created_at) DESC, call_id);
CREATE INDEX IF NOT EXISTS idx_call_session_source_time
    ON call_session(observed_source_id, COALESCE(requested_at, first_audio_at, created_at) DESC);
CREATE INDEX IF NOT EXISTS idx_call_session_target_time
    ON call_session(observed_target_id, COALESCE(requested_at, first_audio_at, created_at) DESC);
CREATE INDEX IF NOT EXISTS idx_call_session_group_time
    ON call_session(group_id, COALESCE(requested_at, first_audio_at, created_at) DESC);
CREATE INDEX IF NOT EXISTS idx_call_session_status_time
    ON call_session(status, COALESCE(requested_at, first_audio_at, created_at) DESC);
CREATE INDEX IF NOT EXISTS idx_call_session_request
    ON call_session(request_id);
CREATE INDEX IF NOT EXISTS idx_call_session_channel_time
    ON call_session(initial_channel_id, COALESCE(requested_at, first_audio_at, created_at) DESC);

CREATE TABLE IF NOT EXISTS call_participant (
    participant_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL DEFAULT 1,
    call_id TEXT NOT NULL,
    observed_id TEXT,
    resolved_id TEXT,
    display_name TEXT,
    role TEXT NOT NULL,
    participant_type TEXT NOT NULL,
    resolution_method TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    evidence_event_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(call_id) REFERENCES call_session(call_id) ON DELETE CASCADE,
    CHECK(role IN ('caller','callee','group','speaker','unknown')),
    CHECK(participant_type IN ('radio','group','unknown')),
    CHECK(confidence >= 0.0 AND confidence <= 1.0),
    UNIQUE(call_id, role, observed_id)
);
CREATE INDEX IF NOT EXISTS idx_call_participant_call ON call_participant(call_id, role);
CREATE INDEX IF NOT EXISTS idx_call_participant_observed ON call_participant(observed_id, call_id);
CREATE INDEX IF NOT EXISTS idx_call_participant_resolved ON call_participant(resolved_id, call_id);

CREATE TABLE IF NOT EXISTS call_turn (
    turn_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL DEFAULT 1,
    call_id TEXT NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    request_id TEXT,
    speaker_radio_id TEXT,
    started_at TEXT,
    ended_at TEXT,
    transcript TEXT,
    normalized_text TEXT,
    asr_ok INTEGER NOT NULL,
    asr_model TEXT,
    asr_confidence REAL,
    audio_path TEXT,
    audio_sha256 TEXT,
    sequence_no INTEGER,
    late_event INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(call_id) REFERENCES call_session(call_id) ON DELETE CASCADE,
    CHECK(asr_ok IN (0,1)),
    CHECK(late_event IN (0,1)),
    CHECK(asr_confidence IS NULL OR (asr_confidence >= 0.0 AND asr_confidence <= 1.0))
);
CREATE INDEX IF NOT EXISTS idx_call_turn_call_sequence
    ON call_turn(call_id, sequence_no, started_at, event_id);
CREATE INDEX IF NOT EXISTS idx_call_turn_time ON call_turn(started_at, event_id);

CREATE TABLE IF NOT EXISTS call_event_link (
    call_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    relation TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    late_event INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY(call_id, event_id),
    FOREIGN KEY(call_id) REFERENCES call_session(call_id) ON DELETE CASCADE,
    CHECK(confidence >= 0.0 AND confidence <= 1.0),
    CHECK(late_event IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_call_event_event ON call_event_link(event_id);
CREATE INDEX IF NOT EXISTS idx_call_event_type_time ON call_event_link(event_type, occurred_at);

CREATE TABLE IF NOT EXISTS call_capture_link (
    call_id TEXT NOT NULL,
    capture_session_id TEXT NOT NULL,
    audio_path TEXT,
    audio_sha256 TEXT,
    started_at TEXT,
    ended_at TEXT,
    confidence REAL NOT NULL DEFAULT 1.0,
    evidence_event_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(call_id, capture_session_id),
    FOREIGN KEY(call_id) REFERENCES call_session(call_id) ON DELETE CASCADE,
    CHECK(confidence >= 0.0 AND confidence <= 1.0)
);
CREATE INDEX IF NOT EXISTS idx_call_capture_session ON call_capture_link(capture_session_id, call_id);

CREATE TABLE IF NOT EXISTS projector_offset (
    projector_name TEXT PRIMARY KEY,
    last_journal_offset INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

INSERT OR IGNORE INTO migration_version(version, applied_at, description)
VALUES(1, strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'call store schema');
