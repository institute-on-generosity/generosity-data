-- 👍/👎 from NomBot users on individual search results. Human relevance labels
-- for the search eval (NomBot web/eval). No accounts: client_id is a random id
-- kept in the browser, so one person's vote on a result can be changed or cleared.
CREATE TABLE feedback (
  id            bigserial PRIMARY KEY,
  client_id     text NOT NULL,
  question      text NOT NULL,
  question_key  text NOT NULL,           -- normalized question (lowercase, single spaces)
  ein           char(9) NOT NULL,
  vote          smallint NOT NULL CHECK (vote IN (-1, 1)),
  rank          int,                     -- 1-based position the result was shown at
  filters       jsonb,                   -- filters in effect (states, max revenue, cause, requirements, parser)
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (client_id, question_key, ein)
);
CREATE INDEX feedback_question_idx ON feedback (question_key, ein);
