-- Make the newest-record views deterministic: 5,611 orgs filed 2+ returns for the same tax year, and
-- DISTINCT ON without a tie-break picked one at random on each refresh (an insight's recount failed between
-- runs). Same rule as filing_latest: newest object_id (the latest filing) wins.
DROP MATERIALIZED VIEW IF EXISTS flag_latest;
CREATE MATERIALIZED VIEW flag_latest AS
  SELECT DISTINCT ON (ein) ein, tax_year, cy_revenue, py_revenue, cy_expenses, cash, cy_contributions
  FROM filing_flag ORDER BY ein, tax_year DESC NULLS LAST, object_id DESC;
CREATE UNIQUE INDEX flag_latest_ein_idx ON flag_latest (ein);

-- financials has 20 duplicate (ein, tax_year) rows; prefer the full 990 over 990-EZ, then the larger revenue
DROP MATERIALIZED VIEW IF EXISTS financials_latest;
CREATE MATERIALIZED VIEW financials_latest AS
  SELECT DISTINCT ON (ein) ein, tax_year, form, revenue, expenses, assets,
         coalesce(raw->>'totnetassetend', raw->>'totnetassetsend')::numeric AS net_assets
  FROM financials ORDER BY ein, tax_year DESC, form, revenue DESC NULLS LAST;
CREATE UNIQUE INDEX financials_latest_ein_idx ON financials_latest (ein);
