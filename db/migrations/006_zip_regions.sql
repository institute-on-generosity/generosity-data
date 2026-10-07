-- ZIP code -> county, and whether that county is in Appalachia (ARC region), so searches
-- like "food banks in rural Appalachia" filter on the actual region, not 13 whole states.
-- Built by etl/load_regions.py from Census 2020 ZCTA-county relationships and the ARC county list.
CREATE TABLE zip_regions (
  zip5        char(5) PRIMARY KEY,
  county_fips char(5) NOT NULL,   -- county with the largest share of the ZIP's land area
  county_name text NOT NULL,
  state       char(2) NOT NULL,
  appalachia  boolean NOT NULL
);
CREATE INDEX zip_regions_appalachia_idx ON zip_regions (zip5) WHERE appalachia;
