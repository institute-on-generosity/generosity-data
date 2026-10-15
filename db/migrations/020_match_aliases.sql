-- Grant recipient matching, round 2 (etl/rematch_grants.py):
--   orgs.sort_name: the IRS master file's secondary name (chapter or local name, e.g. "BINGHAMTON UNIVERSITY HILLEL"
--                   for Hillel), used as an alias when matching grant recipients
--   grant_match_overrides: hand corrections that beat every automatic rule
ALTER TABLE orgs ADD COLUMN IF NOT EXISTS sort_name text;

CREATE TABLE IF NOT EXISTS grant_match_overrides (
  recipient_norm text NOT NULL,          -- org_norm(recipient_name)
  recipient_state text NOT NULL DEFAULT '',  -- '' = any state
  ein char(9),                           -- NULL = never link this name (e.g. a placeholder that looks like an org)
  note text,
  added_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (recipient_norm, recipient_state)
);
