-- Poverty estimate detail for explanations: people in poverty and the SAIPE 90% confidence range.
-- The loader also stores the national figures as county_fips '00000' (state 'US') for comparison.
ALTER TABLE county_need ADD COLUMN people_in_poverty int, ADD COLUMN poverty_low numeric(4,1), ADD COLUMN poverty_high numeric(4,1);
