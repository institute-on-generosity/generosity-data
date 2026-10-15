-- Insights: precomputed, verified patterns for NomBot's Insights tab (philanthropic researchers and advisors).
-- Written by etl/mine_insights.py: each row is one SQL query + a Vega-Lite chart + text; re-runs update numbers.
CREATE TABLE IF NOT EXISTS insights (
  id            serial PRIMARY KEY,
  slug          text UNIQUE NOT NULL,
  section       text NOT NULL CHECK (section IN ('funding-gaps', 'rising-fading', 'money-flows', 'track-record', 'affiliations', 'financial-stress')),
  headline      text NOT NULL,          -- specific, with a number
  summary       text NOT NULL,          -- 1-2 sentences: what, and why it matters to an advisor
  chart         jsonb NOT NULL,         -- complete Vega-Lite v5 spec, data inlined in data.values, width "container", no title
  method        text NOT NULL,          -- data, years, how computed
  caveats       text,                   -- coverage limits
  sources       jsonb NOT NULL,         -- [{label, url}]
  query         text NOT NULL,          -- SQL that reproduces the numbers
  data          jsonb NOT NULL,         -- rows for CSV download (<= 500 flat objects)
  org_eins      text[],                 -- orgs to open in NomBot (<= 60)
  explore_query text,                   -- NomBot search string that reproduces the set
  cause         text,                   -- NTEE major letter (A-Z), optional
  state         char(2),                -- optional
  score         real NOT NULL,          -- surprise/importance 0-1
  verified      boolean NOT NULL DEFAULT false,
  created_at    timestamptz DEFAULT now(),
  refreshed_at  timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS insights_section ON insights (section, score DESC);
