-- The organization's own website as it reports it on the 990 / 990-EZ header (WebsiteAddressTxt), cleaned
-- to a bare host + path (no scheme). NULL when blank or a placeholder ("N/A", "NONE"). Filled by etl/load_990_text.py.
ALTER TABLE filing_text ADD COLUMN website text;
