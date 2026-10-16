-- Board interlocks v2 (graded Oct 15: low-confidence links were ~53% the same person).
-- person_key(): apostrophes no longer split names (D'Albert became "ALBERT"), and credentials, honorifics
-- and status words are dropped ("KIM LCSW", "MARK DECEASED"). Middle initial and Jr/Sr stay out of the key
-- (person_middle(), person_suffix()); a shared person counts only when they don't conflict
-- ("John A Smith" vs "John R Smith", "Pat Becker Jr" vs "Pat Becker Sr").
-- Low-confidence pairs (one shared name, no grant between them, name on 4+ boards nationally) are kept
-- only when both orgs are in the same county. Obvious affiliates ("X" and "X Foundation", a chamber and
-- its foundation, a fire company and its relief association) are 'sister', not 'link'.
-- Rebuild (etl/load_board.py): UPDATE board_members SET person_key = person_key(name); REFRESH MATERIALIZED VIEW board_interlocks.

-- The name's words, upper case, with honorifics, suffixes, credentials and status words removed (regex, not
-- unnest: this runs on ~5M board rows). Apostrophes are dropped first: "O'NEIL" -> "ONEIL".
-- Functions call each other schema-qualified: REFRESH MATERIALIZED VIEW runs with a restricted search_path.
CREATE OR REPLACE FUNCTION person_words(n text) RETURNS text[] LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT string_to_array(btrim(regexp_replace(regexp_replace(
    ' ' || regexp_replace(regexp_replace(upper(coalesce(n, '')), '[''`’]', '', 'g'), '[^A-Z]+', ' ', 'g') || ' ',
    ' (?:MR|MRS|MS|MISS|DR|REV|HON|PROF|FR|FATHER|SISTER|BROTHER|PASTOR|BISHOP|RABBI|JUDGE|SEN|GEN|COL|CAPT|LT|MAJ|SGT|JR|SR|II|III|IV|PHD|MD|ESQ|ESQUIRE|CPA|JD|MBA|RN|BSN|MSN|DNP|NP|FNP|APRN|DDS|DMD|DO|OD|DVM|PSYD|EDD|DMIN|MPH|MPA|MSW|LCSW|LMSW|LPC|LCPC|LMFT|PE|AIA|CFRE|CAE|CFP|CFA|CLU|CHFC|FACHE|FACP|FACS|RET|USA|USN|USAF|USMC|THE|TRUSTEE|TTEE|DECEASED|DECD|EMERITUS|RESIGNED|FORMER|INTERIM)(?= )', '', 'g'),
    '\s+', ' ', 'g')), ' ')
$$;

-- First and last name ("Dr. John A. O'Neil, Jr., CPA" -> "JOHN ONEIL"). NULL for organizations and placeholders.
CREATE OR REPLACE FUNCTION person_key(n text) RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE
    WHEN upper(n) ~ '\m(BANK|TRUST|N A|NA|LLC|LLP|INC|CORP|CORPORATION|COMPANY|FOUNDATION|ASSOCIATION|STATEMENT|ATTACHED|SCHEDULE|COMMITTEE|BOARD|FUND|PARTNERS|GROUP|CHURCH|SOCIETY|COUNCIL|NONE|VARIOUS|SEE)\M' THEN NULL
    WHEN cardinality(w) >= 2 THEN w[1] || ' ' || w[cardinality(w)]
  END
  FROM (SELECT string_to_array(btrim(regexp_replace(' ' || array_to_string(public.person_words(n), '  ') || ' ', ' [A-Z](?= )', '', 'g')), ' ') AS w0) x,
       LATERAL (SELECT array_remove(w0, '') AS w) y
$$;

-- Middle initial ("JOHN A SMITH" -> "A"); NULL when none or when the name starts with an initial ("J ROBERT SMITH").
-- The last name is the last full word ("KIM WIREMAN LCSW-C": the trailing "C" is not a surname).
CREATE OR REPLACE FUNCTION person_middle(n text) RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE WHEN length(w[1]) > 1 AND cardinality(w) >= 3 AND length(w[2]) >= 1
                   AND cardinality(array_remove(w[3:], NULL)) > 0 AND array_to_string(w[3:], ' ') ~ '[A-Z]{2}'
              THEN left(w[2], 1) END
  FROM (SELECT public.person_words(n) AS w) x
$$;

-- Generational suffix: JR, SR, II, III, IV (NULL when none).
CREATE OR REPLACE FUNCTION person_suffix(n text) RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT substring(upper(coalesce(n, '')) FROM '\m(JR|SR|II|III|IV)\M')
$$;

-- Core of an org name, for spotting affiliates ("URBAN LAND INSTITUTE FOUNDATION" -> "URBAN LAND").
CREATE OR REPLACE FUNCTION org_core(n text) RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT btrim(regexp_replace(regexp_replace(public.org_norm(n), '\m(FOUNDATION|FUND|FDN|TRUST|ENDOWMENT|CHARITABLE|CHARITIES|EDUCATIONAL|EDUCATION|ACTION|INSTITUTE|ASSOCIATION|ASSN|HOLDINGS?|PROPERTIES|HOUSING|DEVELOPMENT|SUPPORT|SUPPORTING|ORGANIZATION|ORG|FRIENDS|NFP|USA|AMERICA|AMERICAN)\M', '', 'g'), '\s+', ' ', 'g'))
$$;

DROP MATERIALIZED VIEW IF EXISTS board_interlocks;
-- Org pairs in the same state that list the same person (kind, relation, confidence as in migration 018).
CREATE MATERIALIZED VIEW board_interlocks AS
WITH p AS (
  SELECT b.ein, b.person_key, bool_or(b.form = '990PF') AS funder,
         array_remove(array_agg(DISTINCT public.person_middle(b.name)), NULL) AS mids,
         array_remove(array_agg(DISTINCT public.person_suffix(b.name)), NULL) AS sfx
  FROM board_members b WHERE b.person_key IS NOT NULL GROUP BY 1, 2),
m AS (
  SELECT p.*, coalesce(o.state, f.state) AS st, z.county_fips AS cty
  FROM p LEFT JOIN orgs o USING (ein) LEFT JOIN funders f USING (ein) LEFT JOIN zip_regions z ON z.zip5 = left(o.zip, 5)),
size AS (SELECT ein, count(*) AS board, bool_or(funder) AS funder FROM m GROUP BY 1),
local AS (SELECT person_key, st FROM m WHERE st IS NOT NULL GROUP BY 1, 2 HAVING count(*) BETWEEN 2 AND 8),
nat AS (SELECT person_key, count(*) AS boards FROM m GROUP BY 1),
agg AS (
  SELECT a.ein AS ein_a, b.ein AS ein_b, min(a.st) AS state, count(*)::int AS shared,
         array_agg(a.person_key ORDER BY a.person_key) AS people, min(n.boards) AS rarest,
         bool_and(a.cty IS NOT NULL AND a.cty = b.cty) AS same_county
  FROM local l
  JOIN m a ON a.person_key = l.person_key AND a.st = l.st
  JOIN m b ON b.person_key = l.person_key AND b.st = l.st AND a.ein < b.ein
  JOIN nat n ON n.person_key = l.person_key
  -- the same person only if middle initials and Jr/Sr don't conflict
  WHERE (a.mids = '{}' OR b.mids = '{}' OR a.mids && b.mids) AND (a.sfx = '{}' OR b.sfx = '{}' OR a.sfx && b.sfx)
  GROUP BY 1, 2),
paid AS (  -- grants between the two, either direction, keyed like the pairs (lower EIN first)
  SELECT least(funder_ein, recipient_ein) AS a, greatest(funder_ein, recipient_ein) AS b,
         count(*)::int AS grants, sum(amount) AS amount, array_agg(DISTINCT tax_year ORDER BY tax_year) AS years
  FROM grants WHERE recipient_ein IS NOT NULL AND recipient_ein <> funder_ein GROUP BY 1, 2),
named AS (
  SELECT x.*, sa.funder AS a_funder, sb.funder AS b_funder,
         round(x.shared::numeric / least(sa.board, sb.board), 2) AS overlap,
         coalesce(fa.name, oa.name, '') AS name_a, coalesce(fb.name, ob.name, '') AS name_b
  FROM agg x JOIN size sa ON sa.ein = x.ein_a JOIN size sb ON sb.ein = x.ein_b
  LEFT JOIN orgs oa ON oa.ein = x.ein_a LEFT JOIN funders fa ON fa.ein = x.ein_a
  LEFT JOIN orgs ob ON ob.ein = x.ein_b LEFT JOIN funders fb ON fb.ein = x.ein_b)
SELECT x.ein_a, x.ein_b, x.state, x.shared, x.overlap, x.people,
       CASE WHEN g.grants IS NOT NULL THEN 'funder-grantee'
            WHEN x.a_funder AND x.b_funder THEN 'funder-funder'
            WHEN x.a_funder OR x.b_funder THEN 'funder-nonprofit'
            ELSE 'nonprofit-nonprofit' END AS kind,
       CASE WHEN x.name_a ~ '\m(TA|TR|UA|U/A|TUW|U/W|TRUST)\M' AND x.name_b ~ '\m(TA|TR|UA|U/A|TUW|U/W|TRUST)\M' THEN 'bank-trust'
            WHEN x.overlap >= 0.5 OR similarity(x.name_a, x.name_b) >= 0.4 THEN 'sister'
            -- affiliates: one name's core inside the other's, a chamber and its foundation, a fire company and its relief fund
            WHEN length(public.org_core(x.name_a)) >= 6 AND length(public.org_core(x.name_b)) >= 6
                 AND (position(public.org_core(x.name_a) IN public.org_core(x.name_b)) > 0 OR position(public.org_core(x.name_b) IN public.org_core(x.name_a)) > 0) THEN 'sister'
            WHEN x.name_a ~ '\mCHAMBER\M' AND x.name_b ~ '\mCHAMBER\M' THEN 'sister'
            WHEN x.name_a ~ '\m(FIRE|FIREMENS|FIREMEN|RESCUE|AMBULANCE)\M' AND x.name_b ~ '\m(FIRE|FIREMENS|FIREMEN|RESCUE|AMBULANCE)\M' THEN 'sister'
            ELSE 'link' END AS relation,
       CASE WHEN g.grants IS NOT NULL OR x.shared >= 2 THEN 'high' WHEN x.rarest <= 3 THEN 'medium' ELSE 'low' END AS confidence,
       g.grants, g.amount, g.years
FROM named x LEFT JOIN paid g ON g.a = x.ein_a AND g.b = x.ein_b
-- one common name and no grant: only when both orgs are in the same county
WHERE g.grants IS NOT NULL OR x.shared >= 2 OR x.rarest <= 3 OR x.same_county
WITH NO DATA;
CREATE UNIQUE INDEX IF NOT EXISTS board_interlocks_pair ON board_interlocks (ein_a, ein_b);
CREATE INDEX IF NOT EXISTS board_interlocks_b ON board_interlocks (ein_b);
