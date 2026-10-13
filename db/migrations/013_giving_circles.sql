-- Giving circles and other collective-giving groups (people pooling gifts and voting on grants), found in
-- the IRS master file by name: Impact 100 chapters, "100 Women/Men Who Care", women's funds, and groups
-- named "giving circle". Only circles that are their own nonprofit show up; ones hosted by a community
-- foundation, or whose members give directly, don't file with the IRS. Patterns from the network
-- directories (Impact100 Global, 100 Who Care Alliance, Philanos); NTEE codes vary too much to use alone.
-- grants_paid: Form 990 Part IX line 1 (grants to US organizations), newest SOI year, when filed.
CREATE OR REPLACE VIEW giving_circles AS
WITH c AS (
  SELECT o.*,
    CASE
      WHEN o.name ~* 'impact\s?100' THEN 'Impact 100'
      WHEN o.name ~* '\m100\+?\s?(women|men|people|kids|teens|plus women)\s+who\s+care|power of 100|\m100 women strong' THEN '100 Who Care'
      WHEN o.name ~* 'giving circle|circle of giving|collective giving|giving (collective|together)' AND coalesce(o.ntee_cd, 'T') ~ '^[TSP]' THEN 'Giving circle'
      WHEN o.name ~* 'women.?s (fund|foundation|giving)|women give' AND coalesce(o.ntee_cd, 'T') !~ '^Q' THEN 'Women''s fund'
    END AS kind
  FROM orgs o
)
SELECT * FROM (
  SELECT c.ein, c.name, c.city, c.state, c.ntee_cd, c.kind, c.revenue_amt AS revenue,
         (SELECT nullif((f.raw->>'grntstogovt')::numeric, 0) FROM financials f WHERE f.ein = c.ein AND f.form = '990' ORDER BY f.tax_year DESC LIMIT 1) AS grants_paid,
         (SELECT t.website FROM filing_text t WHERE t.ein = c.ein AND t.website IS NOT NULL ORDER BY t.tax_year DESC NULLS LAST LIMIT 1) AS website
  FROM c WHERE c.kind IS NOT NULL) g
-- "Women's foundation" also names advocacy and service groups: keep only ones that pay real grants.
WHERE g.kind <> 'Women''s fund' OR g.grants_paid >= 10000;
