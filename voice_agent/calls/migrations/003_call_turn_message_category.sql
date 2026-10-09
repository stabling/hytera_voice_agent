-- 003: call_turn 新增 message_category 列，用于区分 user_voice / control_voice。
-- call_get_transcript 只返回 user_voice turns（需求 §22），control_voice turns
-- 不得通过通话内容工具泄露给用户。

ALTER TABLE call_turn ADD COLUMN message_category TEXT;
CREATE INDEX IF NOT EXISTS idx_call_turn_category
    ON call_turn(call_id, message_category);

INSERT OR IGNORE INTO migration_version(version, applied_at, description)
VALUES(3, strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'call_turn message_category column');
