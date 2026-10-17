-- Federal assistance to nonprofits from USAspending.gov: grants and cooperative agreements (assistance
-- types 02-05) whose recipient is a nonprofit or a private college, by award and federal fiscal year,
-- with awarding agency, assistance listing (CFDA) and place of performance. Loaded by
-- etl/load_federal_awards.py from the USAspending award data archive. Recipients are linked to orgs by
-- name and place (etl/org_matcher.py): USAspending has no EIN.
-- Additive: one new table. Data dictionary: docs/research-layer.md.
CREATE TABLE IF NOT EXISTS federal_awards (
  id               bigserial PRIMARY KEY,
  award_key        text NOT NULL,      -- assistance_award_unique_key
  fiscal_year      smallint NOT NULL,  -- federal fiscal year of the actions (Oct-Sep)
  fain             text,
  agency_code      text,               -- awarding agency (toptier code)
  agency_name      text,
  sub_agency_name  text,
  cfda_number      text,               -- assistance listing
  cfda_title       text,
  assistance_type  char(2),            -- 02 block grant · 03 formula grant · 04 project grant · 05 cooperative agreement
  recipient_uei    text,
  recipient_name   text NOT NULL,
  recipient_city   text,
  recipient_state  char(2),
  recipient_zip    text,
  business_types   text,               -- USAspending business type codes (M, N, O ...)
  recipient_kind   text,               -- nonprofit_501c3 (M) · nonprofit_other (N) · private_college (O)
  pop_scope        text,               -- primary_place_of_performance_scope
  pop_state        char(2),
  pop_county_fips  char(5),
  amount           bigint,             -- sum of federal_action_obligation in the fiscal year (net of deobligations)
  actions          integer,
  description      text,               -- award base description
  recipient_ein    char(9),
  match_method     text,
  match_score      real,
  UNIQUE (award_key, fiscal_year)
);
CREATE INDEX IF NOT EXISTS federal_awards_pop_idx ON federal_awards (pop_state, fiscal_year);
CREATE INDEX IF NOT EXISTS federal_awards_county_idx ON federal_awards (pop_county_fips);
CREATE INDEX IF NOT EXISTS federal_awards_recipient_idx ON federal_awards (recipient_ein) WHERE recipient_ein IS NOT NULL;
CREATE INDEX IF NOT EXISTS federal_awards_agency_idx ON federal_awards (agency_code, fiscal_year);
