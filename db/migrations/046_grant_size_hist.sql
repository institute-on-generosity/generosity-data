-- Research layer, part 6: grant-size histograms, so medians and percentiles (typical grant, one in ten
-- grants is $X or more) answer from summary rows instead of sorting millions of grants.
-- One row per research-cube cell (same dimensions as grant_cube / charity_grant_cube) and amount bucket.
-- Bucket = the amount cut to 3 significant digits (amount_bucket(); amounts under $1,000 are their own
-- bucket). exact = every amount in the row equals the bucket (a round amount like $25,000 or $12,300),
-- which is 92% of foundation grants. A non-exact row holds amounts in [bucket, next bucket) and keeps
-- their min and max, so a percentile read from these rows is exact when it lands in exact rows and
-- otherwise bounded by that row's min and max (under 1% apart). Sum n over any set of rows.
-- Additive: one function, three new materialized views; refresh_research_layer() refreshes them too.
SET LOCAL work_mem = '512MB';
SET LOCAL maintenance_work_mem = '1GB';

CREATE OR REPLACE FUNCTION amount_bucket(a bigint) RETURNS bigint LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE WHEN a IS NULL OR a < 1000 THEN a
              ELSE a - a % (10::bigint ^ (length(a::text) - 3))::bigint END
$$;

-- 990-PF grants (grant_clean), dimensions exactly as grant_cube.
DROP MATERIALIZED VIEW IF EXISTS grant_size_hist;
CREATE MATERIALIZED VIEW grant_size_hist AS
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
       amount_bucket(g.amount) AS bucket,
       g.amount = amount_bucket(g.amount) AS exact,
       count(*)::int AS n,
       min(g.amount) AS lo,
       max(g.amount) AS hi
FROM grant_clean g
JOIN grants gr ON gr.id = g.id
LEFT JOIN public_entities pe ON pe.id = gr.recipient_entity
LEFT JOIN county_need cn ON cn.county_fips = g.county_fips
LEFT JOIN recipient_profile rp ON rp.ein = g.recipient_ein
LEFT JOIN funder_profile fp ON fp.ein = g.funder_ein
LEFT JOIN lab ON lab.ein = g.recipient_ein
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12;
CREATE UNIQUE INDEX grant_size_hist_pk ON grant_size_hist
  (tax_year, recipient_state, rural, ntee3, recipient_kind, linked, recipient_band, funder_band, funder_type, labels, bucket, exact) NULLS NOT DISTINCT;
CREATE INDEX grant_size_hist_ntee_idx ON grant_size_hist (ntee3 text_pattern_ops, tax_year);
CREATE INDEX grant_size_hist_state_idx ON grant_size_hist (recipient_state, tax_year);
CREATE INDEX grant_size_hist_labels_idx ON grant_size_hist USING gin (labels) WHERE labels <> '{}';

-- Schedule I grants (charity_grant_clean), dimensions exactly as charity_grant_cube.
DROP MATERIALIZED VIEW IF EXISTS charity_grant_size_hist;
CREATE MATERIALIZED VIEW charity_grant_size_hist AS
WITH lab AS (SELECT ein, array_agg(label || ':' || tier ORDER BY label) AS labels FROM org_labels GROUP BY ein)
SELECT g.tax_year::smallint AS tax_year,
       g.recipient_state,
       CASE WHEN cn.population < 50000 THEN true WHEN cn.population IS NOT NULL THEN false END AS rural,
       left(g.recipient_ntee, 3) AS ntee3,
       g.recipient_ein IS NOT NULL AS linked,
       g.funder_class,
       g.to_grantmaker,
       coalesce(lab.labels, '{}') AS labels,
       amount_bucket(g.amount) AS bucket,
       g.amount = amount_bucket(g.amount) AS exact,
       count(*)::int AS n,
       min(g.amount) AS lo,
       max(g.amount) AS hi
FROM charity_grant_clean g
LEFT JOIN county_need cn ON cn.county_fips = g.county_fips
LEFT JOIN lab ON lab.ein = g.recipient_ein
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10;
CREATE UNIQUE INDEX charity_grant_size_hist_pk ON charity_grant_size_hist
  (tax_year, recipient_state, rural, ntee3, linked, funder_class, to_grantmaker, labels, bucket, exact) NULLS NOT DISTINCT;
CREATE INDEX charity_grant_size_hist_ntee_idx ON charity_grant_size_hist (ntee3 text_pattern_ops, tax_year);
CREATE INDEX charity_grant_size_hist_state_idx ON charity_grant_size_hist (recipient_state, tax_year);
CREATE INDEX charity_grant_size_hist_labels_idx ON charity_grant_size_hist USING gin (labels) WHERE labels <> '{}';

-- Federal award-years above $0 (federal_awards), by fiscal year, place of performance, recipient cause
-- (newest IRS master file NTEE, labels), recipient size. rural as grant_cube, from pop_county_fips.
-- usd: there is no federal cube, so the average comes from here too.
DROP MATERIALIZED VIEW IF EXISTS fed_award_size_hist;
CREATE MATERIALIZED VIEW fed_award_size_hist AS
WITH lab AS (SELECT ein, array_agg(label || ':' || tier ORDER BY label) AS labels FROM org_labels GROUP BY ein)
SELECT fa.fiscal_year,
       fa.pop_state,
       CASE WHEN cn.population < 50000 THEN true WHEN cn.population IS NOT NULL THEN false END AS rural,
       left(o.ntee_cd, 3) AS ntee3,
       fa.recipient_ein IS NOT NULL AS linked,
       rp.band AS recipient_band,
       coalesce(lab.labels, '{}') AS labels,
       amount_bucket(fa.amount) AS bucket,
       fa.amount = amount_bucket(fa.amount) AS exact,
       count(*)::int AS n,
       sum(fa.amount)::bigint AS usd,
       min(fa.amount) AS lo,
       max(fa.amount) AS hi
FROM federal_awards fa
LEFT JOIN orgs o ON o.ein = fa.recipient_ein
LEFT JOIN county_need cn ON cn.county_fips = fa.pop_county_fips AND cn.county_fips <> '00000'
LEFT JOIN recipient_profile rp ON rp.ein = fa.recipient_ein
LEFT JOIN lab ON lab.ein = fa.recipient_ein
WHERE fa.amount > 0
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9;
CREATE UNIQUE INDEX fed_award_size_hist_pk ON fed_award_size_hist
  (fiscal_year, pop_state, rural, ntee3, linked, recipient_band, labels, bucket, exact) NULLS NOT DISTINCT;
CREATE INDEX fed_award_size_hist_labels_idx ON fed_award_size_hist USING gin (labels) WHERE labels <> '{}';

CREATE OR REPLACE FUNCTION refresh_research_layer() RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  REFRESH MATERIALIZED VIEW CONCURRENTLY recipient_profile;
  REFRESH MATERIALIZED VIEW CONCURRENTLY funder_profile;
  REFRESH MATERIALIZED VIEW CONCURRENTLY grant_cube;
  REFRESH MATERIALIZED VIEW CONCURRENTLY grant_recipient_year;
  REFRESH MATERIALIZED VIEW CONCURRENTLY grant_county_year;
  -- Schedule I (042)
  REFRESH MATERIALIZED VIEW CONCURRENTLY charity_funder_profile;
  REFRESH MATERIALIZED VIEW CONCURRENTLY charity_grant_clean;
  REFRESH MATERIALIZED VIEW CONCURRENTLY charity_grant_cube;
  -- Grant-size histograms (046), after the tables they read
  REFRESH MATERIALIZED VIEW CONCURRENTLY grant_size_hist;
  REFRESH MATERIALIZED VIEW CONCURRENTLY charity_grant_size_hist;
  REFRESH MATERIALIZED VIEW CONCURRENTLY fed_award_size_hist;
END $$;
