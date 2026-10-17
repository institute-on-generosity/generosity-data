-- Schedule I grants: grants that public charities (Form 990 filers) report paying to domestic
-- organizations and governments (Schedule I Part II, RecipientTable). Covers donor-advised fund
-- sponsors, community foundations, United Ways and every other charity that reports grants over $5,000.
-- Loaded by etl/load_charity_grants.py from the same IRS e-file batches as the 990-PF grants.
-- Additive: new tables only. Kept apart from `grants` (990-PF) so foundation answers don't change.
-- Data dictionary: docs/research-layer.md.

-- One row per Form 990 return that has a Schedule I (newest return per filer and tax period).
CREATE TABLE IF NOT EXISTS charity_filers (
  object_id         text PRIMARY KEY,
  ein               char(9) NOT NULL,
  tax_year          smallint NOT NULL,
  name              text,
  city              text,
  state             char(2),
  holds_dafs        boolean,   -- Part IV line 6 (DonorAdvisedFundInd): maintains donor-advised funds
  daf_count         integer,   -- Schedule D Part I line 1(a): donor-advised funds held at year end
  daf_contributions bigint,    -- Schedule D Part I line 2(a)
  daf_grants        bigint,    -- Schedule D Part I line 3(a): grants from DAFs during the year
  daf_value         bigint,    -- Schedule D Part I line 4(a): DAF assets at year end
  grants_domestic   bigint,    -- Part IX line 1(A): grants to domestic organizations and governments
  sched_i_rows      integer,   -- RecipientTable rows on Schedule I
  sched_i_usd       bigint     -- their cash + non-cash total
);
CREATE INDEX IF NOT EXISTS charity_filers_ein_idx ON charity_filers (ein, tax_year);

-- One row per Schedule I Part II line. amount = cash + non-cash. reported_ein is the recipient EIN as
-- the filer wrote it; recipient_ein is the link to orgs (reported EIN when it's in orgs, else the name
-- matcher). Grants to individuals (Part III) are not loaded.
CREATE TABLE IF NOT EXISTS charity_grants (
  id                bigserial PRIMARY KEY,
  funder_ein        char(9) NOT NULL,
  object_id         text NOT NULL,
  tax_year          smallint,
  recipient_name    text NOT NULL,
  recipient_city    text,
  recipient_state   char(2),
  recipient_zip     text,
  recipient_country text,      -- set only for a foreign address
  reported_ein      char(9),
  irc_section       text,
  cash              bigint,
  noncash           bigint,
  amount            bigint,
  purpose           text,
  noncash_desc      text,
  recipient_ein     char(9),
  match_method      text,      -- ein · exact · exact-city · fuzzy
  match_score       real
);
CREATE INDEX IF NOT EXISTS charity_grants_funder_idx ON charity_grants (funder_ein, tax_year);
CREATE INDEX IF NOT EXISTS charity_grants_recipient_idx ON charity_grants (recipient_ein) WHERE recipient_ein IS NOT NULL;
CREATE INDEX IF NOT EXISTS charity_grants_state_idx ON charity_grants (recipient_state, tax_year);
