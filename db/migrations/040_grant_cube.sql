-- Research layer, part 3: pre-aggregated foundation-grant facts, so open-ended research questions
-- (totals, shares, rankings and breakdowns by place, cause, recipient size, funder size or type,
-- recipient kind) answer from ~1M summary rows instead of ~5M grants. Built on grant_clean (033),
-- recipient_profile and funder_profile (038). Same grants, same exclusions: sums match grant_clean.
-- Additive: three new materialized views and a refresh function. Data dictionary: docs/research-layer.md.
SET LOCAL work_mem = '512MB';
SET LOCAL maintenance_work_mem = '1GB';

-- grant_cube: one row per combination of the dimensions below; measures are sums over grant_clean.
--   usd, n, gos_usd, gos_n  additive: sum them over any set of rows
--   recipients, funders     distinct counts WITHIN the row only; NOT additive across rows
-- Place = the recipient's address on the grant (often a headquarters). rural = the recipient ZIP's
-- county has under 50,000 people (county_need population); NULL = ZIP not mapped to a county.
DROP MATERIALIZED VIEW IF EXISTS grant_cube;
CREATE MATERIALIZED VIEW grant_cube AS
WITH lab AS (SELECT ein, array_agg(label || ':' || tier ORDER BY label) AS labels FROM org_labels GROUP BY ein)
SELECT g.tax_year::smallint AS tax_year,
       g.recipient_state,
       CASE WHEN cn.population < 50000 THEN true WHEN cn.population IS NOT NULL THEN false END AS rural,
       left(g.recipient_ntee, 3) AS ntee3,
       CASE WHEN pe.kind IS NOT NULL THEN pe.kind WHEN g.recipient_ein IS NOT NULL THEN 'nonprofit' ELSE 'unidentified' END AS recipient_kind,
       g.recipient_ein IS NOT NULL AS linked,
       rp.band AS recipient_band,
       fp.asset_band AS funder_band,
       coalesce(fp.funder_type, 'other') AS funder_type,
       coalesce(lab.labels, '{}') AS labels,
       sum(g.amount)::bigint AS usd,
       count(*)::int AS n,
       coalesce(sum(g.amount) FILTER (WHERE g.gos), 0)::bigint AS gos_usd,
       (count(*) FILTER (WHERE g.gos))::int AS gos_n,
       count(DISTINCT g.recipient_ein)::int AS recipients,
       count(DISTINCT g.funder_ein)::int AS funders
FROM grant_clean g
JOIN grants gr ON gr.id = g.id
LEFT JOIN public_entities pe ON pe.id = gr.recipient_entity
LEFT JOIN county_need cn ON cn.county_fips = g.county_fips
LEFT JOIN recipient_profile rp ON rp.ein = g.recipient_ein
LEFT JOIN funder_profile fp ON fp.ein = g.funder_ein
LEFT JOIN lab ON lab.ein = g.recipient_ein
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10;
CREATE UNIQUE INDEX grant_cube_pk ON grant_cube
  (tax_year, recipient_state, rural, ntee3, recipient_kind, linked, recipient_band, funder_band, funder_type, labels) NULLS NOT DISTINCT;
CREATE INDEX grant_cube_ntee_idx ON grant_cube (ntee3 text_pattern_ops, tax_year);
CREATE INDEX grant_cube_state_idx ON grant_cube (recipient_state, tax_year);
CREATE INDEX grant_cube_labels_idx ON grant_cube USING gin (labels) WHERE labels <> '{}';

-- grant_recipient_year: identified recipients only, one row per recipient, tax year and grant address
-- (a recipient's grants can carry different addresses). For exact distinct-recipient counts, top
-- recipients and concentration without touching every grant. No funder columns: questions that
-- filter by funder use grant_clean.
DROP MATERIALIZED VIEW IF EXISTS grant_recipient_year;
CREATE MATERIALIZED VIEW grant_recipient_year AS
WITH lab AS (SELECT ein, array_agg(label || ':' || tier ORDER BY label) AS labels FROM org_labels GROUP BY ein)
SELECT g.recipient_ein,
       g.tax_year::smallint AS tax_year,
       g.recipient_state,
       g.county_fips,
       CASE WHEN cn.population < 50000 THEN true WHEN cn.population IS NOT NULL THEN false END AS rural,
       g.recipient_ntee,
       rp.band AS recipient_band,
       coalesce(lab.labels, '{}') AS labels,
       sum(g.amount)::bigint AS usd,
       count(*)::int AS n,
       coalesce(sum(g.amount) FILTER (WHERE g.gos), 0)::bigint AS gos_usd,
       count(DISTINCT g.funder_ein)::int AS funders
FROM grant_clean g
LEFT JOIN county_need cn ON cn.county_fips = g.county_fips
LEFT JOIN recipient_profile rp ON rp.ein = g.recipient_ein
LEFT JOIN lab ON lab.ein = g.recipient_ein
WHERE g.recipient_ein IS NOT NULL
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8;
CREATE UNIQUE INDEX grant_recipient_year_pk ON grant_recipient_year (recipient_ein, tax_year, recipient_state, county_fips) NULLS NOT DISTINCT;
CREATE INDEX grant_recipient_year_ntee_idx ON grant_recipient_year (recipient_ntee text_pattern_ops, tax_year);
CREATE INDEX grant_recipient_year_state_idx ON grant_recipient_year (recipient_state, tax_year);
CREATE INDEX grant_recipient_year_labels_idx ON grant_recipient_year USING gin (labels) WHERE labels <> '{}';

-- grant_county_year: all clean grants by tax year, recipient county and NTEE major group, for
-- county rankings and county per-resident figures. county_fips NULL = ZIP not mapped to a county.
DROP MATERIALIZED VIEW IF EXISTS grant_county_year;
CREATE MATERIALIZED VIEW grant_county_year AS
SELECT g.tax_year::smallint AS tax_year,
       g.county_fips,
       g.recipient_state,
       left(g.recipient_ntee, 1) AS ntee_major,
       g.recipient_ein IS NOT NULL AS linked,
       sum(g.amount)::bigint AS usd,
       count(*)::int AS n
FROM grant_clean g
GROUP BY 1, 2, 3, 4, 5;
CREATE UNIQUE INDEX grant_county_year_pk ON grant_county_year (tax_year, county_fips, recipient_state, ntee_major, linked) NULLS NOT DISTINCT;
CREATE INDEX grant_county_year_state_idx ON grant_county_year (recipient_state, tax_year);

-- Refresh the research layer after grant_clean / flag_latest / org_labels / county_need change.
-- CONCURRENTLY: readers keep the old rows while each view rebuilds (each has a unique index).
-- Called from db/refresh.sql after grant_clean and grant_totals.
CREATE OR REPLACE FUNCTION refresh_research_layer() RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  REFRESH MATERIALIZED VIEW CONCURRENTLY recipient_profile;
  REFRESH MATERIALIZED VIEW CONCURRENTLY funder_profile;
  REFRESH MATERIALIZED VIEW CONCURRENTLY grant_cube;
  REFRESH MATERIALIZED VIEW CONCURRENTLY grant_recipient_year;
  REFRESH MATERIALIZED VIEW CONCURRENTLY grant_county_year;
END $$;
