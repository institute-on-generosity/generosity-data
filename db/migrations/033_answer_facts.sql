-- Research answers (NomBot lib/answer.ts): slim, pre-filtered facts so aggregate questions
-- ("how much foundation money goes to X", "how many X run deficits") answer in well under 3 s.
-- Refreshed by db/refresh.sql after grants or 990 filings change.

-- grant_clean: every 990-PF grant for tax years 2022-2025 to a US organization, with the same
-- exclusions as the insights miner (CLEAN in etl/mine_insights.py): amount > 0, no individuals,
-- placeholders or foreign recipients (recipient_kind), no transfers to another private foundation.
-- Unlinked grants stay in (recipient_ein NULL): address fields are on every grant.
-- gos: the purpose text reads as general operating / unrestricted support (text rule, not a coded field).
DROP MATERIALIZED VIEW IF EXISTS grant_totals;
DROP MATERIALIZED VIEW IF EXISTS grant_clean;
CREATE MATERIALIZED VIEW grant_clean AS
SELECT g.id, g.funder_ein, f.state AS funder_state, g.recipient_ein, g.recipient_state,
       z.county_fips, g.tax_year, g.amount, o.ntee_cd AS recipient_ntee,
       coalesce(g.purpose ~* '\m(general|unrestricted)\M|operating support|\m(operations|operating)\M', false) AS gos
FROM grants g
LEFT JOIN orgs o ON o.ein = g.recipient_ein
LEFT JOIN funders f ON f.ein = g.funder_ein
LEFT JOIN zip_regions z ON z.zip5 = left(g.recipient_zip, 5)::char(5)
WHERE g.tax_year BETWEEN 2022 AND 2025 AND g.amount > 0 AND g.recipient_kind IS NULL
  AND coalesce(o.foundation, '') NOT IN ('02', '03', '04');
CREATE INDEX grant_clean_recipient_idx ON grant_clean (recipient_ein, tax_year) WHERE recipient_ein IS NOT NULL;
CREATE INDEX grant_clean_state_idx ON grant_clean (recipient_state, tax_year);
CREATE INDEX grant_clean_ntee_idx ON grant_clean (recipient_ntee text_pattern_ops) WHERE recipient_ntee IS NOT NULL;
CREATE INDEX grant_clean_funder_idx ON grant_clean (funder_ein);
CREATE INDEX grant_clean_county_idx ON grant_clean (county_fips);

-- grant_totals: denominators per tax year and recipient state (all clean grants, linked ones,
-- general-support ones), so shares and link rates need no 5M-row scan.
CREATE MATERIALIZED VIEW grant_totals AS
SELECT tax_year, recipient_state AS state,
       sum(amount)::bigint AS usd, count(*)::int AS n,
       (sum(amount) FILTER (WHERE recipient_ein IS NOT NULL))::bigint AS linked_usd,
       (count(*) FILTER (WHERE recipient_ein IS NOT NULL))::int AS linked_n,
       (sum(amount) FILTER (WHERE gos))::bigint AS gos_usd,
       (count(*) FILTER (WHERE gos))::int AS gos_n
FROM grant_clean GROUP BY 1, 2;
CREATE INDEX grant_totals_idx ON grant_totals (tax_year, state);

-- flag_year: one 990 or 990-EZ per org per tax year (newest filing), with the revenue lines answers
-- use and the org's state, NTEE code and county. government_grants is Part VIII line 1e (full 990
-- only; NULL on 990-EZ). Money answers treat a NULL on a full 990 as $0, like the miner.
DROP MATERIALIZED VIEW IF EXISTS flag_year;
CREATE MATERIALIZED VIEW flag_year AS
SELECT DISTINCT ON (f.ein, f.tax_year)
       f.ein, f.tax_year, f.form, f.cy_revenue AS revenue, f.cy_expenses AS expenses,
       f.cy_contributions AS contributions, f.government_grants, f.cash,
       o.state, o.ntee_cd, o.subsection, o.foundation, z.county_fips
FROM filing_flag f
JOIN orgs o ON o.ein = f.ein
LEFT JOIN zip_regions z ON z.zip5 = left(o.zip, 5)::char(5)
ORDER BY f.ein, f.tax_year, f.object_id DESC;
CREATE UNIQUE INDEX flag_year_pk ON flag_year (ein, tax_year);
CREATE INDEX flag_year_state_idx ON flag_year (tax_year, state);
CREATE INDEX flag_year_ntee_idx ON flag_year (ntee_cd text_pattern_ops);
