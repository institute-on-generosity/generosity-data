-- Links removed after grading (Oct 15): which grant, what it pointed to, and why, so the change can be undone or audited.
CREATE TABLE IF NOT EXISTS grant_unlinked (
  grant_id bigint PRIMARY KEY,
  ein      char(9),
  method   text,
  score    real,
  reason   text,
  at       timestamptz DEFAULT now()
);
