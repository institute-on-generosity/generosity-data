-- Research layer for Schedule I grants (charity_grants, migration 041): who the grantmaking charities
-- are, a clean grant view with the same exclusions as grant_clean, and a cube for fast sums.
-- Additive: three new materialized views; refresh_research_layer() now refreshes them too.
-- Data dictionary: docs/research-layer.md.
SET LOCAL work_mem = '512MB';

-- charity_funder_profile: one row per Schedule I filer.
--   holds_dafs      any loaded return says it maintains donor-advised funds (Part IV line 6)
--   daf_share       Schedule D DAF grants / Schedule I grants, over the years where both are reported
--   funder_class    first match wins:
--     united_way            "United Way" in the name
--     community_foundation  "Community Foundation / Fund / Trust" in the name
--     daf_sponsor           holds DAFs and DAF grants are at least half of its Schedule I grants
--                           (national sponsors such as Fidelity, Schwab, Vanguard, NPT, American Endowment
--                           Foundation; also Jewish and faith-based funds that run mostly on DAFs)
--     community_foundation  otherwise NTEE T31 (community foundations without the words in their name)
--     other                 every other charity reporting grants (universities, hospitals, federations,
--                           intermediaries passing on federal or foundation money)
DROP MATERIALIZED VIEW IF EXISTS charity_grant_cube;
DROP MATERIALIZED VIEW IF EXISTS charity_grant_clean;
DROP MATERIALIZED VIEW IF EXISTS charity_funder_profile;
CREATE MATERIALIZED VIEW charity_funder_profile AS
WITH y AS (
  SELECT ein, bool_or(holds_dafs) AS holds_dafs,
         sum(daf_grants) FILTER (WHERE daf_grants IS NOT NULL AND sched_i_usd > 0) AS daf_g,
         sum(sched_i_usd) FILTER (WHERE daf_grants IS NOT NULL AND sched_i_usd > 0) AS si_g,
         sum(sched_i_usd) AS sched_i_usd
  FROM charity_filers GROUP BY ein),
n AS (SELECT DISTINCT ON (ein) ein, name, state, daf_count, daf_value FROM charity_filers ORDER BY ein, tax_year DESC, object_id DESC)
SELECT n.ein, n.name, coalesce(o.state, n.state) AS state, o.ntee_cd AS ntee,
       coalesce(y.holds_dafs, false) AS holds_dafs, n.daf_count, n.daf_value,
       CASE WHEN y.si_g > 0 THEN (y.daf_g::float8 / y.si_g)::real END AS daf_share,
       y.sched_i_usd,
       CASE WHEN n.name ~* '\munited way\M' OR o.name ~* '\munited way\M' THEN 'united_way'
            WHEN n.name ~* '\mcommunity (foundation|fund|trust)\M' OR o.name ~* '\mcommunity (foundation|fund|trust)\M' THEN 'community_foundation'
            WHEN y.holds_dafs AND y.si_g > 0 AND y.daf_g >= 0.5 * y.si_g THEN 'daf_sponsor'
            WHEN o.ntee_cd LIKE 'T31%' THEN 'community_foundation'
            ELSE 'other' END AS funder_class
FROM n JOIN y USING (ein)
LEFT JOIN orgs o ON o.ein = n.ein;
CREATE UNIQUE INDEX charity_funder_profile_pk ON charity_funder_profile (ein);
CREATE INDEX charity_funder_profile_class_idx ON charity_funder_profile (funder_class);

-- charity_grant_clean: Schedule I grants for tax years 2022-2025 to US organizations (a US state on the
-- address, no foreign address), amount above 0, no transfers to private foundations (as grant_clean).
-- to_grantmaker: the recipient is itself a DAF sponsor, community foundation or United Way, so the money
-- will be granted again and would count twice; answers leave these out by default.
CREATE MATERIALIZED VIEW charity_grant_clean AS
SELECT c.id, c.funder_ein, fp.funder_class, fp.state AS funder_state, c.recipient_ein, c.recipient_state,
       z.county_fips, c.tax_year, c.amount, o.ntee_cd AS recipient_ntee,
       coalesce(c.purpose ~* '\m(general|unrestricted)\M|operating support|\m(operations|operating)\M', false) AS gos,
       coalesce(rp.funder_class IN ('daf_sponsor', 'community_foundation', 'united_way'), false) AS to_grantmaker
FROM charity_grants c
JOIN charity_funder_profile fp ON fp.ein = c.funder_ein
LEFT JOIN orgs o ON o.ein = c.recipient_ein
LEFT JOIN charity_funder_profile rp ON rp.ein = c.recipient_ein
LEFT JOIN zip_regions z ON z.zip5 = left(c.recipient_zip, 5)::char(5)
WHERE c.tax_year BETWEEN 2022 AND 2025 AND c.amount > 0 AND c.recipient_country IS NULL
  AND c.recipient_state ~ '^(A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA|PR|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])$'
  AND coalesce(o.foundation, '') NOT IN ('02', '03', '04');
CREATE UNIQUE INDEX charity_grant_clean_pk ON charity_grant_clean (id);
CREATE INDEX charity_grant_clean_recipient_idx ON charity_grant_clean (recipient_ein, tax_year) WHERE recipient_ein IS NOT NULL;
CREATE INDEX charity_grant_clean_state_idx ON charity_grant_clean (recipient_state, tax_year);
CREATE INDEX charity_grant_clean_ntee_idx ON charity_grant_clean (recipient_ntee text_pattern_ops) WHERE recipient_ntee IS NOT NULL;
CREATE INDEX charity_grant_clean_funder_idx ON charity_grant_clean (funder_ein);
CREATE INDEX charity_grant_clean_county_idx ON charity_grant_clean (county_fips);

-- charity_grant_cube: sums over charity_grant_clean by the dimensions research answers filter on.
-- usd, n, gos_usd, gos_n are additive. Same conventions as grant_cube (rural, ntee3, labels).
CREATE MATERIALIZED VIEW charity_grant_cube AS
WITH lab AS (SELECT ein, array_agg(label || ':' || tier ORDER BY label) AS labels FROM org_labels GROUP BY ein)
SELECT g.tax_year::smallint AS tax_year,
       g.recipient_state,
       CASE WHEN cn.population < 50000 THEN true WHEN cn.population IS NOT NULL THEN false END AS rural,
       left(g.recipient_ntee, 3) AS ntee3,
       g.recipient_ein IS NOT NULL AS linked,
       g.funder_class,
       g.to_grantmaker,
       coalesce(lab.labels, '{}') AS labels,
       sum(g.amount)::bigint AS usd,
       count(*)::int AS n,
       coalesce(sum(g.amount) FILTER (WHERE g.gos), 0)::bigint AS gos_usd,
       (count(*) FILTER (WHERE g.gos))::int AS gos_n
FROM charity_grant_clean g
LEFT JOIN county_need cn ON cn.county_fips = g.county_fips
LEFT JOIN lab ON lab.ein = g.recipient_ein
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8;
CREATE UNIQUE INDEX charity_grant_cube_pk ON charity_grant_cube
  (tax_year, recipient_state, rural, ntee3, linked, funder_class, to_grantmaker, labels) NULLS NOT DISTINCT;
CREATE INDEX charity_grant_cube_ntee_idx ON charity_grant_cube (ntee3 text_pattern_ops, tax_year);
CREATE INDEX charity_grant_cube_labels_idx ON charity_grant_cube USING gin (labels) WHERE labels <> '{}';

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
END $$;
