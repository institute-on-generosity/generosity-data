-- Research questions asked in NomBot (research answers, /research/ask): one row per distinct question,
-- upserted each time it's answered. No personal data: the question text, its parsed spec and headline only.
CREATE TABLE IF NOT EXISTS research_questions (
  id                  bigserial PRIMARY KEY,
  question            text NOT NULL,                  -- as first asked (display)
  normalized_question text NOT NULL UNIQUE,           -- lower case, single spaces, no trailing punctuation
  spec                jsonb NOT NULL,                 -- the router's spec (lib/answerSpec)
  headline            text NOT NULL,                  -- the answer's headline when last asked
  status              text NOT NULL DEFAULT 'ok',     -- ok / partial / unanswerable
  asked_count         integer NOT NULL DEFAULT 1,
  first_asked_at      timestamptz NOT NULL DEFAULT now(),
  last_asked_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS research_questions_recent ON research_questions (last_asked_at DESC);
CREATE INDEX IF NOT EXISTS research_questions_top ON research_questions (asked_count DESC, last_asked_at DESC);
