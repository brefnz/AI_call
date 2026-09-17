-- Skema database AI Outbound Calling & Voice Blasting
-- Default SQLite, dapat diport ke MySQL/Postgres sesuai kebutuhan (PRD tidak mengunci engine tertentu).

CREATE TABLE IF NOT EXISTS campaign (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    topic_id            TEXT NOT NULL,
    start_date          TEXT NOT NULL,
    end_date            TEXT NOT NULL,
    call_start_time     TEXT NOT NULL,   -- 'HH:MM'
    call_end_time       TEXT NOT NULL,   -- 'HH:MM'
    concurrent_limit    INTEGER NOT NULL DEFAULT 5,
    max_retry           INTEGER NOT NULL DEFAULT 2,
    status              TEXT NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE, PAUSED, COMPLETED
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (topic_id) REFERENCES topic(id)
);

CREATE TABLE IF NOT EXISTS campaign_target (
    id                  TEXT PRIMARY KEY,
    campaign_id         TEXT NOT NULL,
    phone_number        TEXT NOT NULL,
    customer_data       TEXT,            -- JSON string: data tambahan dari Datakelola
    status              TEXT NOT NULL DEFAULT 'PENDING',
    -- PENDING, CALLING, CONNECTED, NO_ANSWER, BUSY, REJECTED, FAILED, COMPLETED, INCOMPLETE
    retry_count         INTEGER NOT NULL DEFAULT 0,
    scheduled_at        TEXT,
    called_at           TEXT,
    connected_at        TEXT,
    ended_at            TEXT,
    duration            INTEGER,         -- detik
    outcome             TEXT,            -- SUCCESS, PARTIAL_SUCCESS, NO_ANSWER, BUSY, REJECTED,
                                          -- FAILED, INCOMPLETE, CALLER_REFUSED, REQUEST_HUMAN_AGENT
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (campaign_id) REFERENCES campaign(id)
);

CREATE TABLE IF NOT EXISTS topic (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    description         TEXT,
    objective           TEXT,
    system_instruction  TEXT,
    status              TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS topic_question (
    id                  TEXT PRIMARY KEY,
    topic_id            TEXT NOT NULL,
    question_text       TEXT NOT NULL,
    question_order      INTEGER NOT NULL,
    is_mandatory        INTEGER NOT NULL DEFAULT 1,   -- 0/1
    answer_type         TEXT NOT NULL DEFAULT 'text', -- text, yes_no, scale, choice
    validation_rule     TEXT,            -- JSON string, opsional
    next_question       TEXT,            -- id pertanyaan berikutnya (opsional, untuk branching)
    status              TEXT NOT NULL DEFAULT 'ACTIVE',
    FOREIGN KEY (topic_id) REFERENCES topic(id)
);

CREATE TABLE IF NOT EXISTS call_session (
    id                  TEXT PRIMARY KEY,
    campaign_target_id  TEXT NOT NULL,
    asterisk_call_id    TEXT,
    gemini_session_id   TEXT,
    started_at          TEXT,
    connected_at        TEXT,
    ended_at            TEXT,
    duration            INTEGER,
    status              TEXT NOT NULL DEFAULT 'INIT',
    -- INIT, CALLING, CONNECTED, IN_PROGRESS, COMPLETED, FAILED, INCOMPLETE
    outcome             TEXT,
    conversation_result TEXT,            -- GOOD, PARTIAL, POOR
    FOREIGN KEY (campaign_target_id) REFERENCES campaign_target(id)
);

CREATE TABLE IF NOT EXISTS conversation_answer (
    id                  TEXT PRIMARY KEY,
    call_session_id     TEXT NOT NULL,
    question_id         TEXT NOT NULL,
    answer_text         TEXT,
    answer_status       TEXT NOT NULL,   -- VALID, INVALID, UNCLEAR, SKIPPED, REFUSED
    confidence          REAL,
    answered_at         TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (call_session_id) REFERENCES call_session(id),
    FOREIGN KEY (question_id) REFERENCES topic_question(id)
);

CREATE TABLE IF NOT EXISTS transcript (
    id                  TEXT PRIMARY KEY,
    call_session_id     TEXT NOT NULL,
    speaker             TEXT NOT NULL,   -- AI, CALLER
    text                TEXT NOT NULL,
    timestamp           TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (call_session_id) REFERENCES call_session(id)
);

CREATE TABLE IF NOT EXISTS call_event_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id         TEXT,
    target_id           TEXT,
    call_session_id     TEXT,
    ari_call_id         TEXT,
    gemini_session_id   TEXT,
    event               TEXT NOT NULL,   -- CALL_CONNECTED, GEMINI_SESSION_STARTED, QUESTION_STARTED, ...
    status              TEXT,
    error                TEXT,
    timestamp           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_target_campaign_status ON campaign_target (campaign_id, status);
CREATE INDEX IF NOT EXISTS idx_target_scheduled ON campaign_target (scheduled_at);
CREATE INDEX IF NOT EXISTS idx_answer_session ON conversation_answer (call_session_id);
CREATE INDEX IF NOT EXISTS idx_transcript_session ON transcript (call_session_id);
