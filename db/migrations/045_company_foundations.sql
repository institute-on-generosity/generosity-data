-- Company (corporate) foundations. The IRS doesn't flag them and NTEE T21 is unreliable (it codes the Ford
-- Foundation as corporate), so etl/load_company_foundations.py builds the flag from evidence on each
-- foundation's own 990-PF: Schedule B business contributors, corporate words in the name, and SEC EDGAR
-- company names. Additive: three new tables, nothing existing changes. Data dictionary: docs/research-layer.md.

-- Business (non-person) contributors listed on 990-PF Schedule B, every 990-PF return we have.
CREATE TABLE IF NOT EXISTS pf_business_contributors (
  ein       char(9) NOT NULL,   -- the foundation
  object_id text    NOT NULL,   -- its return
  tax_year  int,
  name      text    NOT NULL,   -- contributor business name as written
  address1  text,
  city      text,
  state     text,
  zip       text,
  amount    bigint              -- total contributions that year
);
CREATE INDEX IF NOT EXISTS pf_business_contributors_ein ON pf_business_contributors (ein);

-- The filer's street address on each 990-PF (compared with contributor addresses).
CREATE TABLE IF NOT EXISTS pf_filer_address (
  ein       char(9) NOT NULL,
  object_id text    PRIMARY KEY,
  tax_year  int,
  address1  text,
  city      text,
  state     text,
  zip       text
);

-- One row per 990-PF filer judged a company foundation.
--   evidence  contributor = a Schedule B business contributor carries the foundation's name stem
--             corp_name   = the name itself says company ("... Company Foundation", "... Corporate Fund")
--             sec         = the name stem is an SEC EDGAR company
--             hq          = that SEC company's address is the foundation's city and state
--             address     = that contributor shares the foundation's street address
CREATE TABLE IF NOT EXISTS company_foundations (
  ein       char(9) PRIMARY KEY,
  company   text    NOT NULL,   -- the company: contributor or SEC name, else the stem
  cik       bigint,             -- SEC central index key when matched
  ticker    text,
  evidence  text[]  NOT NULL,
  stem      text    NOT NULL    -- normalized foundation name stem the match used
);
