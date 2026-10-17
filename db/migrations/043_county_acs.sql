-- Census American Community Survey 5-year estimates (2020-2024 release) by county, state and nation:
-- population, poverty, median household income, age and race/ethnicity. Loaded by etl/load_acs.py from
-- the Census table-based summary files (no API key). Joins to county_need on county_fips.
-- Additive: one new table. county_need (SAIPE poverty, population estimates) is unchanged.
-- Data dictionary: docs/research-layer.md.
CREATE TABLE IF NOT EXISTS county_acs (
  county_fips        char(5) PRIMARY KEY,   -- county FIPS; state rows = state FIPS || '000'; nation = '00000'
  level              text NOT NULL CHECK (level IN ('county', 'state', 'us')),
  name               text NOT NULL,
  state              char(2),               -- postal code ('US' for the nation)
  acs_year           smallint NOT NULL,     -- last year of the 5-year period (2024 = 2020-2024)
  population         integer,               -- B01003_001
  poverty_universe   integer,               -- B17001_001: people whose poverty status is known
  people_in_poverty  integer,               -- B17001_002: income in the past 12 months below poverty
  poverty_moe        integer,               -- B17001_002 margin of error (90%)
  poverty_rate       real,                  -- people_in_poverty / poverty_universe, percent
  median_income      integer,               -- B19013_001: median household income, 2024 dollars
  under18            integer,               -- B01001: under 18
  age65_plus         integer,               -- B01001: 65 and over
  white_nh           integer,               -- B03002_003: White alone, not Hispanic or Latino
  black_nh           integer,               -- B03002_004
  aian_nh            integer,               -- B03002_005: American Indian and Alaska Native alone
  asian_nh           integer,               -- B03002_006
  nhpi_nh            integer,               -- B03002_007: Native Hawaiian and Other Pacific Islander alone
  other_nh           integer,               -- B03002_008: some other race alone
  multi_nh           integer,               -- B03002_009: two or more races
  hispanic           integer,               -- B03002_012: Hispanic or Latino, any race
  poc_share          real                   -- 1 - white_nh / B03002_001: people of color, percent
);
CREATE INDEX IF NOT EXISTS county_acs_state_idx ON county_acs (state, level);
