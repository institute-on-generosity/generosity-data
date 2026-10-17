-- Research layer, part 1: size bands and per-organization profiles that research answers
-- (NomBot lib/answer.ts) filter and group by. Additive: new functions, a lookup table and two
-- materialized views. Nothing existing changes. Data dictionary: docs/research-layer.md.

-- Size bands. Edges are fixed so a band always means the same dollars; answers snap a requested
-- cutoff ("under $1M") to these edges and say so.
CREATE OR REPLACE FUNCTION revenue_band(v bigint) RETURNS smallint
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE WHEN v IS NULL THEN NULL
              ELSE width_bucket(v, ARRAY[100000, 500000, 1000000, 5000000, 10000000, 50000000, 100000000]::bigint[]) + 1 END::smallint
$$;
CREATE OR REPLACE FUNCTION asset_band(v bigint) RETURNS smallint
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE WHEN v IS NULL THEN NULL
              ELSE width_bucket(v, ARRAY[1000000, 10000000, 50000000, 100000000, 1000000000]::bigint[]) + 1 END::smallint
$$;

CREATE TABLE IF NOT EXISTS size_bands (
  kind  text     NOT NULL CHECK (kind IN ('recipient_revenue', 'funder_assets')),
  band  smallint NOT NULL,
  lo    bigint,              -- inclusive; NULL = no lower edge
  hi    bigint,              -- exclusive; NULL = no upper edge
  name  text     NOT NULL,
  PRIMARY KEY (kind, band)
);
INSERT INTO size_bands (kind, band, lo, hi, name) VALUES
  ('recipient_revenue', 1, NULL, 100000, 'Under $100K'),
  ('recipient_revenue', 2, 100000, 500000, '$100K to $500K'),
  ('recipient_revenue', 3, 500000, 1000000, '$500K to $1M'),
  ('recipient_revenue', 4, 1000000, 5000000, '$1M to $5M'),
  ('recipient_revenue', 5, 5000000, 10000000, '$5M to $10M'),
  ('recipient_revenue', 6, 10000000, 50000000, '$10M to $50M'),
  ('recipient_revenue', 7, 50000000, 100000000, '$50M to $100M'),
  ('recipient_revenue', 8, 100000000, NULL, '$100M and up'),
  ('funder_assets', 1, NULL, 1000000, 'Under $1M'),
  ('funder_assets', 2, 1000000, 10000000, '$1M to $10M'),
  ('funder_assets', 3, 10000000, 50000000, '$10M to $50M'),
  ('funder_assets', 4, 50000000, 100000000, '$50M to $100M'),
  ('funder_assets', 5, 100000000, 1000000000, '$100M to $1B'),
  ('funder_assets', 6, 1000000000, NULL, '$1B and up')
ON CONFLICT (kind, band) DO UPDATE SET lo = EXCLUDED.lo, hi = EXCLUDED.hi, name = EXCLUDED.name;

-- recipient_profile: one row per organization with a known annual revenue. Revenue is the newest
-- e-filed 990 / 990-EZ (flag_latest) when there is one, else the IRS master file's revenue amount.
-- Orgs with neither (mostly 990-N filers, usually under $50K) are absent, so their band is unknown.
DROP MATERIALIZED VIEW IF EXISTS recipient_profile;
CREATE MATERIALIZED VIEW recipient_profile AS
SELECT o.ein,
       coalesce(fl.cy_revenue, o.revenue_amt) AS revenue,
       CASE WHEN fl.cy_revenue IS NOT NULL THEN '990' ELSE 'bmf' END AS revenue_source,
       CASE WHEN fl.cy_revenue IS NOT NULL THEN fl.tax_year END AS revenue_year,
       revenue_band(coalesce(fl.cy_revenue, o.revenue_amt)) AS band
FROM orgs o
LEFT JOIN flag_latest fl ON fl.ein = o.ein
WHERE coalesce(fl.cy_revenue, o.revenue_amt) IS NOT NULL;
CREATE UNIQUE INDEX recipient_profile_pk ON recipient_profile (ein);
CREATE INDEX recipient_profile_band_idx ON recipient_profile (band);

-- funder_profile: one row per 990-PF filer. Assets = fair market value at year end on its newest
-- 990-PF (not the grant year's). funder_type: the IRS does not flag family or company foundations.
--   family_named  name contains the word "Family" (a floor: most family foundations carry a person's name)
--   operating     private operating foundation (master file foundation code 02 or 03)
--   trust         nonexempt charitable trust filing a 990-PF (section 4947(a)(1), subsection 92)
--   other         everything else, almost all private non-operating foundations
-- First match wins, in that order.
DROP MATERIALIZED VIEW IF EXISTS funder_profile;
CREATE MATERIALIZED VIEW funder_profile AS
SELECT f.ein, f.assets, asset_band(f.assets) AS asset_band,
       CASE WHEN f.name ~* '\mfamily\M' THEN 'family_named'
            WHEN o.foundation IN ('02', '03') THEN 'operating'
            WHEN o.subsection = '92' THEN 'trust'
            ELSE 'other' END AS funder_type
FROM funders f
LEFT JOIN orgs o ON o.ein = f.ein;
CREATE UNIQUE INDEX funder_profile_pk ON funder_profile (ein);
CREATE INDEX funder_profile_band_idx ON funder_profile (asset_band, funder_type);
