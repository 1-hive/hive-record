-- SPDX-License-Identifier: GPL-3.0-or-later
-- The hive record (SPEC §18). Run as the admin role, which owns everything.
-- Role names are substituted by hiverecord.store: {admin}, {gateway}, {reader}.

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO {gateway}, {reader};

-- Values stored outside the log and writable only by the admin: the hive id and
-- the generation (SPEC §17.3).
CREATE TABLE hive_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE events (
    position        BIGINT PRIMARY KEY CHECK (position >= 1),
    event_id        UUID NOT NULL UNIQUE,
    recorded_at     TIMESTAMPTZ NOT NULL,
    type            TEXT NOT NULL,
    actor_id        TEXT NOT NULL,
    goal_id         TEXT,
    task_id         TEXT,
    idempotency_key TEXT NOT NULL,
    request_digest  TEXT NOT NULL,
    canonical       TEXT NOT NULL,
    digest          TEXT NOT NULL,
    envelope        JSONB NOT NULL
);

CREATE UNIQUE INDEX events_idempotency ON events (actor_id, idempotency_key)
    WHERE type <> 'gateway.rejected';
CREATE INDEX events_task ON events (task_id, position) WHERE task_id IS NOT NULL;
CREATE INDEX events_goal ON events (goal_id, position) WHERE goal_id IS NOT NULL;
CREATE INDEX events_type ON events (type, position);

-- Append-only for every role, the owner included.
CREATE FUNCTION events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'the hive record is append-only (% on events)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END $$;

CREATE TRIGGER events_no_update_delete BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION events_append_only();
CREATE TRIGGER events_no_truncate BEFORE TRUNCATE ON events
    FOR EACH STATEMENT EXECUTE FUNCTION events_append_only();

GRANT SELECT ON hive_meta, events TO {gateway}, {reader};
GRANT INSERT ON events TO {gateway};

-- New tables: readable by default, never writable by default.
ALTER DEFAULT PRIVILEGES FOR ROLE {admin} IN SCHEMA public GRANT SELECT ON TABLES TO {gateway}, {reader};
