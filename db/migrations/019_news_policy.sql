-- News and policy sources (asked for at the Oct 14 AI Fellows session): GDELT news, Congress.gov bills,
-- Federal Register documents. Plus source_updates, the freshness log every loader writes to.

-- One row per loader run: what was checked, how many rows it added, the newest item it saw.
CREATE TABLE IF NOT EXISTS source_updates (
  id         bigserial PRIMARY KEY,
  source     text NOT NULL,            -- gdelt, congress, federal_register, ...
  run_at     timestamptz NOT NULL DEFAULT now(),
  rows_added int NOT NULL,
  newest     timestamptz,              -- newest item in the source as of this run
  note       text
);
CREATE INDEX IF NOT EXISTS source_updates_source ON source_updates (source, run_at DESC);

-- GDELT DOC 2.0 article lists for philanthropy and nonprofit-sector queries (etl/load_gdelt.py).
CREATE TABLE IF NOT EXISTS news_articles (
  url            text PRIMARY KEY,
  title          text NOT NULL,
  seen_at        timestamptz NOT NULL,   -- when GDELT first saw it
  domain         text,
  source_country text,
  language       text,
  queries        text[] NOT NULL DEFAULT '{}',  -- which of our topic queries found it
  search         tsvector GENERATED ALWAYS AS (to_tsvector('english', title)) STORED
);
CREATE INDEX IF NOT EXISTS news_articles_seen ON news_articles (seen_at DESC);
CREATE INDEX IF NOT EXISTS news_articles_search ON news_articles USING gin (search);

-- Congress.gov bills, current congress (etl/load_congress.py).
CREATE TABLE IF NOT EXISTS congress_bills (
  congress           int NOT NULL,
  bill_type          text NOT NULL,      -- hr, s, hres, sjres, ...
  number             int NOT NULL,
  title              text NOT NULL,
  origin_chamber     text,
  introduced         date,
  latest_action_date date,
  latest_action      text,
  updated_at         timestamptz,
  url                text,               -- congress.gov page
  search             tsvector GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || coalesce(latest_action, ''))) STORED,
  PRIMARY KEY (congress, bill_type, number)
);
CREATE INDEX IF NOT EXISTS congress_bills_updated ON congress_bills (updated_at DESC);
CREATE INDEX IF NOT EXISTS congress_bills_search ON congress_bills USING gin (search);

-- Federal Register documents: rules, proposed rules, notices (incl. funding notices), presidential documents
-- (etl/load_federal_register.py).
CREATE TABLE IF NOT EXISTS federal_register_docs (
  document_number  text PRIMARY KEY,
  title            text NOT NULL,
  type             text,                 -- Rule, Proposed Rule, Notice, Presidential Document
  abstract         text,
  agencies         text[],
  action           text,
  publication_date date NOT NULL,
  significant      boolean,
  html_url         text,
  -- Notices of funding opportunities / grant programs: what grant seekers most want from here (paperwork
  -- notices about a grant program's forms are left out)
  funding_notice   boolean GENERATED ALWAYS AS (
                     type = 'Notice' AND title !~* '(information collection|comment request|paperwork)'
                     AND (title || ' ' || coalesce(abstract, '')) ~* '(funding opportunit|notice of funding|applications for new awards|inviting applications|grant program)') STORED,
  search           tsvector GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || coalesce(abstract, ''))) STORED
);
CREATE INDEX IF NOT EXISTS federal_register_docs_pub ON federal_register_docs (publication_date DESC);
CREATE INDEX IF NOT EXISTS federal_register_docs_search ON federal_register_docs USING gin (search);
CREATE INDEX IF NOT EXISTS federal_register_docs_funding ON federal_register_docs (publication_date DESC) WHERE funding_notice;
