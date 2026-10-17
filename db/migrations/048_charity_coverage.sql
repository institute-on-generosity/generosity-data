-- Schedule I coverage per return: how much of a filer's own Part IX line 1 total (grants to domestic
-- organizations, charity_filers.grants_domestic) its Schedule I lists by recipient, and why not all of it.
-- Additive: two plain columns, two generated columns and a name rule. Data dictionary: docs/research-layer.md.
--   declared_orgs     Schedule I Part I lines 2 + 3 (Total501c3OrgCnt + TotalOtherOrgCnt), as filed;
--                     set by etl/load_charity_grants.py
--   placeholder_usd   Schedule I dollars on lines that name no recipient ("SEE ATTACHED", "VARIOUS",
--                     "SUMMARY OF GRANTS ... REFER TO PDF"): is_placeholder_recipient(); set by the loader
--   coverage          (sched_i_usd - placeholder_usd) / grants_domestic; NULL when line 1 is 0 or missing
--   coverage_note     first match wins:
--     exceeds_total   Schedule I sum is over 110% of line 1 (Schedule I counts grants line 1 puts elsewhere,
--                     or a filing error)
--     itemized        coverage 90% or more
--     see_attached    placeholder lines carry 10%+ of line 1: the list is in a PDF attachment, not the e-file
--     none_itemized   Schedule I lists no recipients
--     list_incomplete the e-file lists fewer recipients than the return's own count (declared_orgs)
--     not_itemized    rows match the count but sum short: the rest of line 1 isn't listed (grants of $5,000
--                     or less per recipient, accrued multi-year grants, or simply left off)
--     NULL            line 1 is 0 or missing

CREATE OR REPLACE FUNCTION is_placeholder_recipient(name text) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT coalesce(name ~* ('^\s*(see (attach|statement|stmt|schedule|sch\M|list|supplement|detail|part|pdf|below|exhibit|addendum)'
                         || '|refer to\M|various\M|misc(ellaneous)?\M|aggregate\M|summary of grants'
                         || '|multiple (recipients|organizations|charities|grantees|agencies)\M'
                         || '|totals?\s*$|total (grants|of|for)\M|n/?a\s*$|none\s*$)'
                         || '|\m(see attached|attached (list|schedule|statement)|refer to pdf)'), false)
$$;

ALTER TABLE charity_filers ADD COLUMN IF NOT EXISTS declared_orgs integer;
ALTER TABLE charity_filers ADD COLUMN IF NOT EXISTS placeholder_usd bigint;

UPDATE charity_filers SET placeholder_usd = 0 WHERE placeholder_usd IS NULL;
UPDATE charity_filers f SET placeholder_usd = p.usd
FROM (SELECT object_id, sum(amount) AS usd FROM charity_grants
      WHERE is_placeholder_recipient(recipient_name) GROUP BY object_id HAVING sum(amount) > 0) p
WHERE p.object_id = f.object_id;

ALTER TABLE charity_filers ADD COLUMN IF NOT EXISTS coverage real GENERATED ALWAYS AS (
  CASE WHEN grants_domestic > 0
       THEN ((coalesce(sched_i_usd, 0) - coalesce(placeholder_usd, 0))::float8 / grants_domestic)::real END) STORED;

ALTER TABLE charity_filers ADD COLUMN IF NOT EXISTS coverage_note text GENERATED ALWAYS AS (
  CASE WHEN coalesce(grants_domestic, 0) <= 0 THEN NULL
       WHEN coalesce(sched_i_usd, 0) > 1.1 * grants_domestic THEN 'exceeds_total'
       WHEN coalesce(sched_i_usd, 0) - coalesce(placeholder_usd, 0) >= 0.9 * grants_domestic THEN 'itemized'
       WHEN coalesce(placeholder_usd, 0) >= 0.1 * grants_domestic THEN 'see_attached'
       WHEN coalesce(sched_i_rows, 0) = 0 THEN 'none_itemized'
       WHEN declared_orgs > sched_i_rows THEN 'list_incomplete'
       ELSE 'not_itemized' END) STORED;

COMMENT ON COLUMN charity_filers.coverage IS 'Named Schedule I dollars / Part IX line 1 (migration 048)';
COMMENT ON COLUMN charity_filers.coverage_note IS 'exceeds_total | itemized | see_attached | none_itemized | list_incomplete | not_itemized (migration 048)';
