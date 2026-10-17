-- Cause labels: reusable topic tags that no IRS/NTEE code captures (first: climate, women and girls).
-- Definitions live in cause_labels (methodology: docs/labels/<label>.md); per-org results in org_labels,
-- written by etl/label_causes.py. Rerunnable: a run replaces every row of its label and stamps `version`.
CREATE TABLE IF NOT EXISTS cause_labels (
  label       text PRIMARY KEY,                  -- slug, e.g. climate, women_girls
  name        text NOT NULL,                     -- display name, e.g. "Women and girls"
  definition  text NOT NULL,                     -- one paragraph: what the label means
  includes    text NOT NULL,                     -- what counts (examples)
  excludes    text NOT NULL,                     -- what does not count (examples)
  mixed_rule  text NOT NULL,                     -- when an org is 'partial' instead of 'primary'
  version     text NOT NULL,                     -- methodology version, e.g. 2026-10-16.1
  grading     jsonb,                             -- latest fresh-sample grade: precision per tier, recall, sample sizes
  created_at  timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE cause_labels ADD COLUMN IF NOT EXISTS grading jsonb;

CREATE TABLE IF NOT EXISTS org_labels (
  ein        char(9) NOT NULL,
  label      text NOT NULL REFERENCES cause_labels(label) ON DELETE CASCADE,
  tier       text NOT NULL CHECK (tier IN ('primary', 'partial')),
  score      real,                               -- classifier confidence 0-1 (rules: 1)
  method     text NOT NULL,                      -- how it was decided, e.g. llm:claude-haiku-5-5, rule:name
  evidence   text,                               -- <= 25 words: why (quote or paraphrase of name/mission)
  version    text NOT NULL,
  labeled_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (ein, label)
);
CREATE INDEX IF NOT EXISTS org_labels_label_tier ON org_labels (label, tier);
