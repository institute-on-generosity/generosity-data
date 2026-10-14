-- Board members of nonprofits (990, 990-EZ Part VII) and foundations (990-PF officers, directors, trustees),
-- and the org pairs that share them ("board interlocks"). Loaded by etl/load_board.py, refreshed by db/refresh.sql.

-- A person's matching key: first and last name, upper case, no punctuation, titles, suffixes or middle
-- initials ("Dr. John A. Smith, Jr." -> "JOHN SMITH"). NULL for organizations listed as trustees
-- (banks, trust companies) and placeholders ("see statement").
CREATE OR REPLACE FUNCTION person_key(n text) RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE
    WHEN upper(n) ~ '\m(BANK|TRUST|N A|NA|LLC|LLP|INC|CORP|CORPORATION|COMPANY|FOUNDATION|ASSOCIATION|STATEMENT|ATTACHED|SCHEDULE|COMMITTEE|BOARD|FUND|PARTNERS|GROUP|CHURCH|SOCIETY|COUNCIL|NONE|VARIOUS|SEE)\M' THEN NULL
    WHEN cardinality(w) >= 2 THEN w[1] || ' ' || w[cardinality(w)]
  END
  FROM (SELECT array(
    SELECT t FROM unnest(regexp_split_to_array(trim(regexp_replace(upper(coalesce(n, '')), '[^A-Z ]', ' ', 'g')), '\s+')) WITH ORDINALITY u(t, i)
    WHERE length(t) > 1 AND t NOT IN ('MR', 'MRS', 'MS', 'MISS', 'DR', 'REV', 'HON', 'JR', 'SR', 'II', 'III', 'IV', 'PHD', 'MD', 'ESQ', 'CPA', 'JD', 'MBA', 'RN', 'DDS', 'DO', 'THE', 'CFP', 'CFA', 'TRUSTEE', 'TTEE')
    ORDER BY i) AS w) x
$$;

CREATE TABLE IF NOT EXISTS board_members (
  ein        char(9) NOT NULL,
  object_id  text NOT NULL,          -- the return it came from (each org's newest)
  tax_year   int,
  form       text NOT NULL,          -- 990, 990EZ, 990PF
  name       text NOT NULL,
  person_key text,
  title      text,
  role       text,                   -- 990: officer / director / '' ; 990-EZ and 990-PF don't say
  hours      real,
  pay        numeric,
  state      text                    -- the person's state where the form gives it (990-PF), else NULL
);
CREATE INDEX IF NOT EXISTS board_members_key ON board_members (person_key);
CREATE INDEX IF NOT EXISTS board_members_ein ON board_members (ein);

-- Org pairs in the same state that list the same person. Same-state only, and only names that appear on
-- at most 8 boards in that state, so common names ("JOHN SMITH" in Texas) don't create false links.
--   kind        funder-grantee (a foundation paid the other org), funder-funder, funder-nonprofit, nonprofit-nonprofit
--   relation    sister: half or more of the smaller board is shared, or the names are alike ("X Foundation" and
--               "X Association"): one family of orgs, expected. bank-trust: both are bank-run trusts ("... TA",
--               "TR UA", "TUW"), whose returns list the same bank staff. link: everything else, the hidden connections
--   confidence  high: funder-grantee, or 2+ shared people. medium: the name is rare nationally (<= 3 boards). low: otherwise
CREATE MATERIALIZED VIEW IF NOT EXISTS board_interlocks AS
WITH m AS (
  SELECT b.ein, b.person_key, bool_or(b.form = '990PF') AS funder, min(coalesce(o.state, f.state)) AS st
  FROM board_members b LEFT JOIN orgs o USING (ein) LEFT JOIN funders f USING (ein)
  WHERE b.person_key IS NOT NULL GROUP BY 1, 2),
size AS (SELECT ein, count(*) AS board, bool_or(funder) AS funder FROM m GROUP BY 1),
local AS (SELECT person_key, st FROM m WHERE st IS NOT NULL GROUP BY 1, 2 HAVING count(*) BETWEEN 2 AND 8),
nat AS (SELECT person_key, count(*) AS boards FROM m GROUP BY 1),
agg AS (
  SELECT a.ein AS ein_a, b.ein AS ein_b, min(a.st) AS state, count(*)::int AS shared,
         array_agg(a.person_key ORDER BY a.person_key) AS people, min(n.boards) AS rarest
  FROM local l
  JOIN m a ON a.person_key = l.person_key AND a.st = l.st
  JOIN m b ON b.person_key = l.person_key AND b.st = l.st AND a.ein < b.ein
  JOIN nat n ON n.person_key = l.person_key
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
            ELSE 'link' END AS relation,
       CASE WHEN g.grants IS NOT NULL OR x.shared >= 2 THEN 'high' WHEN x.rarest <= 3 THEN 'medium' ELSE 'low' END AS confidence,
       g.grants, g.amount, g.years
FROM named x LEFT JOIN paid g ON g.a = x.ein_a AND g.b = x.ein_b
WITH NO DATA;
CREATE UNIQUE INDEX IF NOT EXISTS board_interlocks_pair ON board_interlocks (ein_a, ein_b);
CREATE INDEX IF NOT EXISTS board_interlocks_b ON board_interlocks (ein_b);
