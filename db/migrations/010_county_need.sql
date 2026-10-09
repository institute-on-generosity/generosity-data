-- County need, for "underserved areas": poverty and income (Census SAIPE 2023) and population (Census
-- population estimates, 2024). Keyed like zip_regions.county_fips. Filled by etl/load_county_need.py.
CREATE TABLE county_need (
  county_fips char(5) PRIMARY KEY,
  name text NOT NULL,
  state char(2) NOT NULL,
  population int,
  poverty_rate numeric(4,1),        -- % of people of all ages in poverty
  child_poverty_rate numeric(4,1),  -- % of people age 0-17 in poverty
  median_income int,                -- median household income, $
  poverty_year int,
  population_year int
);
CREATE INDEX county_need_state_idx ON county_need (state);
