-- Part 1: Human-in-the-loop feedback table for AI-suggested graph edges.
--
-- Naming note: the assignment text says student_saig217.ai_suggestion_feedback,
-- but this repo (and the Aug 20 announcement about Lakebase's 10 replication-slot
-- limit) moved every student table into the SHARED bootcamp_students schema with a
-- _saig217 suffix. app.py hard-codes that convention (SHARED_SCHEMA + _table_for),
-- so the feedback table follows it too. Sharing one schema also means this table
-- rides the existing bootcamp_students replication slot instead of asking for a new one.
--
-- Replace saig217 everywhere below with your login suffix (same value you type
-- at /login and pass to the notebook's `username` widget).

CREATE TABLE IF NOT EXISTS bootcamp_students.ai_suggestion_feedback_saig217 (
    id             SERIAL PRIMARY KEY,
    source_repo    TEXT NOT NULL,
    package_name   TEXT NOT NULL,
    suggested_repo TEXT NOT NULL,
    feedback       TEXT NOT NULL,
    reason         TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Prevents duplicate feedback for the same (repo, package, suggestion) triple.
    -- This is also the ON CONFLICT target used by the /graph/edges/feedback upsert
    -- in app.py, so the constraint name/columns must not change.
    CONSTRAINT ai_suggestion_feedback_saig217_uniq
        UNIQUE (source_repo, package_name, suggested_repo),

    -- Endpoint validates this too, but a DB-level check stops bad rows written by
    -- psql/curl from poisoning the pipeline's blocked-set query.
    CONSTRAINT ai_suggestion_feedback_saig217_feedback_chk
        CHECK (feedback IN ('bad', 'good'))
);

-- Required for logical replication (CDC) to emit full before/after images.
-- Owner-only DDL: run this as the same identity that created the table.
ALTER TABLE bootcamp_students.ai_suggestion_feedback_saig217 REPLICA IDENTITY FULL;

-- ---------------------------------------------------------------------------
-- Verification / CDC readiness checks (run these, do not skip)
-- ---------------------------------------------------------------------------

-- 1. Replica identity should report 'f' (full), not 'd' (default).
SELECT relname, relreplident
FROM pg_class
WHERE oid = 'bootcamp_students.ai_suggestion_feedback_saig217'::regclass;

-- 2. Feed state, per table. Lakebase CDF is powered by the wal2delta extension,
--    NOT by ordinary Postgres publications -- pg_publication_tables is the wrong
--    place to look. Status should read SNAPSHOTTING then STREAMING.
SELECT * FROM wal2delta.tables;

-- 3. Slot budget sanity check (Lakebase caps this around 10, which is why the whole
--    cohort shares the bootcamp_students schema).
SHOW max_replication_slots;

-- 4. Smoke-test row. NOT optional: CDF skips a table with zero rows entirely, so
--    the destination Delta table will not exist until at least one row is inserted.
INSERT INTO bootcamp_students.ai_suggestion_feedback_saig217
    (source_repo, package_name, suggested_repo, feedback, reason)
VALUES ('facebook/react', 'loose-envify', 'zertosh/loose-envify', 'bad', 'CDC smoke test')
ON CONFLICT (source_repo, package_name, suggested_repo) DO UPDATE
    SET feedback = EXCLUDED.feedback,
        reason     = EXCLUDED.reason,
        created_at = now()
RETURNING *;
