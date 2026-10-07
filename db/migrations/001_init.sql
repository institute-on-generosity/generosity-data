-- Core schema shared by NomBot and GrantRadar.
CREATE EXTENSION IF NOT EXISTS vector;

-- One row per exempt organization, from the IRS EO Business Master File.
CREATE TABLE orgs (
  ein          char(9) PRIMARY KEY,
  name         text NOT NULL,
  care_of      text,
  street       text,
  city         text,
  state        char(2),
  zip          text,
  subsection   text,          -- 03 = 501(c)(3)
  foundation   text,          -- foundation code; 02-04 = private foundation
  ruling       text,          -- YYYYMM of exemption ruling
  status       text,          -- 01 unconditional, 12 trust; 25 (terminating) is not loaded
  ntee_cd      text,          -- blank for ~1/3 of orgs
  asset_amt    bigint,
  income_amt   bigint,
  revenue_amt  bigint,
  tax_period   text,          -- YYYYMM of latest return on file
  search       tsvector GENERATED ALWAYS AS (
                 to_tsvector('english', coalesce(name, '') || ' ' || coalesce(city, ''))
               ) STORED,
  loaded_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX orgs_search_idx ON orgs USING gin (search);
CREATE INDEX orgs_state_idx  ON orgs (state);
CREATE INDEX orgs_ntee_idx   ON orgs (ntee_cd);

-- Financials from the IRS SOI annual extract (numbers only).
CREATE TABLE financials (
  ein       char(9) NOT NULL REFERENCES orgs (ein) ON DELETE CASCADE,
  tax_year  int NOT NULL,
  form      text NOT NULL,      -- 990, 990EZ, 990PF
  revenue   bigint,
  expenses  bigint,
  assets    bigint,
  PRIMARY KEY (ein, tax_year, form)
);

-- Mission and program text from 990 e-file XML: the only source for embeddings.
CREATE TABLE filing_text (
  object_id  text PRIMARY KEY,  -- IRS e-file object id
  ein        char(9) NOT NULL,
  tax_year   int,
  form       text,
  mission    text,
  programs   text,
  embedding  vector(512),
  search     tsvector GENERATED ALWAYS AS (
               to_tsvector('english', coalesce(mission, '') || ' ' || coalesce(programs, ''))
             ) STORED
);
CREATE INDEX filing_text_ein_idx    ON filing_text (ein);
CREATE INDEX filing_text_search_idx ON filing_text USING gin (search);
