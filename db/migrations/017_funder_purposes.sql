-- Each funder's grants summed by purpose text, for GrantRadar's grant-type mix ("Mostly unrestricted").
-- Summing the raw grants for the ~5,000 funders a national search matches took ~15s per search;
-- this has ~1/5 the rows of grants (903K) and is indexed by funder. Rebuilt by db/refresh.sql after a grants load.
CREATE MATERIALIZED VIEW IF NOT EXISTS funder_purposes AS
SELECT funder_ein, purpose, count(*)::int AS n, coalesce(sum(amount) FILTER (WHERE amount > 0), 0)::float8 AS amount
FROM grants GROUP BY 1, 2;
CREATE INDEX IF NOT EXISTS funder_purposes_funder ON funder_purposes (funder_ein);
