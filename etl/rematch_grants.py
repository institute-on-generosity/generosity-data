#!/usr/bin/env python3
"""Second-round matching for grants still missing a recipient (grants.recipient_ein IS NULL), after
etl/load_grants.py's exact and fuzzy passes. Each rule proposes candidates; rules are only applied after a
sample is graded (NomBot web/eval/judge_matches.mts) and clears the precision bar.

  python3 etl/rematch_grants.py --candidates          # build rm_cand (unlogged): proposed links per rule
  python3 etl/rematch_grants.py --sample=60           # write a graded-sample file per rule for the judge
  python3 etl/rematch_grants.py --apply=override,alias,zip   # write the chosen rules' links into grants

Rules (US recipients only; placeholders, individuals and foreign addresses are skipped):
  override  grant_match_overrides: hand corrections, beat everything
  alias     recipient name = the org's IRS secondary name (orgs.sort_name), same state, one org, and the
            main names share words (chapters often list their host, e.g. Psi Chi -> "Bloomfield College"),
            in the same city or ZIP (generic names like "Mosaic" repeat across a state)
  zip       same ZIP code, name similarity >= 0.7 (name or secondary name), clearly the best in that ZIP,
            "Foundation" in both names or neither, no PTA/PTO/honor-society/booster chapters unless named
  national  (proposed, not used: 63% right) exact name unique in the country, 3+ words
Candidates are built loosely (zip from 0.5, alias without a name check); ACCEPT holds the graded rules.
Links carry match_method = rule name and match_score = similarity (1 for override / alias / national).
"""
import json, os, subprocess, sys

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "rematch")
US = "'^(A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA|PR|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])$'"
SKIP = r"'^\s*(see|refer to)\b|attach|statement|schedule|various|^\s*n/?a\b|^grants?$|hip+a|confidential|anonymous|not listed|list availab|individual|patient|scholarship recipient|eligible|needy'"

def psql(sql):
    r = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qAt"], input=sql, text=True, capture_output=True)
    if r.returncode:
        sys.exit(f"psql failed: {r.stderr.strip()[:1500]}")
    return r.stdout.strip()

CANDIDATES = f"""
\\timing on
SET work_mem = '1GB';
DROP TABLE IF EXISTS rm_u, rm_o, rm_cand;
-- Unmatched US recipients, one row per (name, city, state, ZIP)
CREATE UNLOGGED TABLE rm_u AS
SELECT org_norm(recipient_name) AS n, min(recipient_name) AS raw, upper(coalesce(recipient_city, '')) AS city,
       recipient_state AS st, left(coalesce(recipient_zip, ''), 5) AS zip, count(*)::int AS grants, coalesce(sum(amount), 0) AS amt
FROM grants
WHERE recipient_ein IS NULL AND recipient_state ~ {US} AND recipient_name !~* {SKIP}
GROUP BY 1, 3, 4, 5;
DELETE FROM rm_u WHERE length(n) < 4;
CREATE INDEX ON rm_u (zip); CREATE INDEX ON rm_u (st, n); ANALYZE rm_u;
CREATE UNLOGGED TABLE rm_o AS
SELECT ein, name, sort_name, org_norm(name) AS n, nullif(org_norm(coalesce(sort_name, '')), '') AS sn,
       state AS st, upper(city) AS city, left(coalesce(zip, ''), 5) AS zip, ntee_cd
FROM orgs;
CREATE INDEX ON rm_o (zip); CREATE INDEX ON rm_o (st, n); CREATE INDEX ON rm_o (st, sn); CREATE INDEX ON rm_o (n); ANALYZE rm_o;
CREATE UNLOGGED TABLE rm_cand (n text, city text, st text, zip text, ein char(9), method text, score real);

-- override: hand corrections (state-specific first, then any state)
INSERT INTO rm_cand
SELECT u.n, u.city, u.st, u.zip, ov.ein, 'override', 1
FROM rm_u u JOIN LATERAL (SELECT ein FROM grant_match_overrides g WHERE g.recipient_norm = u.n AND g.recipient_state IN (u.st, '')
                          ORDER BY g.recipient_state DESC LIMIT 1) ov ON true;

-- alias: the IRS secondary name, one org in the state
INSERT INTO rm_cand
SELECT u.n, u.city, u.st, u.zip, min(o.ein), 'alias', 1
FROM rm_u u JOIN rm_o o ON o.st = u.st AND o.sn = u.n
GROUP BY 1, 2, 3, 4 HAVING count(DISTINCT o.ein) = 1;

-- zip: best name match in the same ZIP, if clearly better than the runner-up
INSERT INTO rm_cand
SELECT n, city, st, zip, ein, 'zip', s FROM (
  SELECT u.n, u.city, u.st, u.zip, o.ein,
         greatest(similarity(o.n, u.n), coalesce(similarity(o.sn, u.n), 0)) AS s,
         row_number() OVER w AS rk, lead(greatest(similarity(o.n, u.n), coalesce(similarity(o.sn, u.n), 0))) OVER w AS next_s
  FROM rm_u u JOIN rm_o o ON o.zip = u.zip
  WHERE u.zip ~ '^[0-9]{{5}}$' AND greatest(similarity(o.n, u.n), coalesce(similarity(o.sn, u.n), 0)) >= 0.5
  WINDOW w AS (PARTITION BY u.n, u.city, u.st, u.zip ORDER BY greatest(similarity(o.n, u.n), coalesce(similarity(o.sn, u.n), 0)) DESC)
) x WHERE rk = 1 AND (next_s IS NULL OR s - next_s >= 0.1);

-- national: exact name held by exactly one org in the country, 3+ words
INSERT INTO rm_cand
SELECT u.n, u.city, u.st, u.zip, o.ein, 'national', 1
FROM rm_u u JOIN (SELECT n, min(ein) AS ein FROM rm_o GROUP BY n HAVING count(*) = 1) o ON o.n = u.n
WHERE array_length(regexp_split_to_array(u.n, '\\s+'), 1) >= 3;

CREATE INDEX ON rm_cand (method); ANALYZE rm_cand;
"""

def candidates():
    print(psql(CANDIDATES).replace("Time:", "\n  time:"))
    print(psql("""SELECT m.method || ': ' || count(*) || ' names, ' || sum(u.grants) || ' grants, $' || round(sum(u.amt) / 1e6) || 'M'
                  FROM (SELECT DISTINCT ON (n, city, st, zip) * FROM rm_cand ORDER BY n, city, st, zip,
                        CASE method WHEN 'override' THEN 0 WHEN 'alias' THEN 1 WHEN 'zip' THEN 2 ELSE 3 END) m
                  JOIN rm_u u USING (n, city, st, zip) GROUP BY m.method ORDER BY 1"""))

# What each rule must also satisfy to be used (from grading: zip at 0.5-0.7 was ~65% right, Foundation vs Inc
# pairs are usually separate entities, chapters list their host as the secondary name, and nationally unique
# names still collide across states: national is not used).
ACCEPT = """(
  (c.method = 'override')
  OR (c.method = 'alias' AND similarity(o.n, u.n) >= 0.3 AND (o.city = u.city OR o.zip = u.zip))
  OR (c.method = 'zip' AND c.score >= 0.7
      AND (u.n ~* 'FOUNDATION') = (o.n ~* 'FOUNDATION')
      AND NOT (o.n ~* '\\m(PTA|PTO|PSI CHI|HONOR SOCIETY|BOOSTER|ALUMNI|PARENT)' AND u.n !~* '\\m(PTA|PTO|PSI CHI|HONOR SOCIETY|BOOSTER|ALUMNI|PARENT)'))
)"""

def sample(k):
    os.makedirs(OUT, exist_ok=True)
    rows = psql(f"""SELECT json_agg(x) FROM (
      SELECT * FROM (
        SELECT c.method, c.score, u.raw AS grant_name, u.city AS grant_city, u.st AS grant_state, u.zip AS grant_zip,
               o.ein, o.name AS org_name, o.sort_name AS org_other_name, o.city AS org_city, o.st AS org_state, o.zip AS org_zip, o.ntee_cd,
               row_number() OVER (PARTITION BY c.method ORDER BY md5(c.n || c.zip)) AS r
        FROM rm_cand c JOIN rm_u u USING (n, city, st, zip) JOIN rm_o o ON o.ein = c.ein
        WHERE {ACCEPT}) y
      WHERE r <= {k}) x""")
    path = os.path.join(OUT, "sample.json")
    with open(path, "w") as f:
        json.dump(json.loads(rows or "[]"), f, indent=1)
    print(f"wrote {path}")

def apply(methods):
    order = ["override", "alias", "zip", "national"]
    chosen = [m for m in order if m in methods]
    psql(f"""
SET work_mem = '1GB';
CREATE TEMP TABLE pick AS
SELECT DISTINCT ON (c.n, c.city, c.st, c.zip) c.n, c.city, c.st, c.zip, c.ein, c.method, c.score
FROM rm_cand c JOIN rm_u u USING (n, city, st, zip) JOIN rm_o o ON o.ein = c.ein
WHERE c.method = ANY(ARRAY{chosen}::text[]) AND {ACCEPT}
ORDER BY c.n, c.city, c.st, c.zip, array_position(ARRAY{order}::text[], c.method);
CREATE INDEX ON pick (st, n);
UPDATE grants g SET recipient_ein = p.ein, match_method = p.method, match_score = p.score
FROM pick p
WHERE g.recipient_ein IS NULL AND g.recipient_state = p.st AND org_norm(g.recipient_name) = p.n
  AND upper(coalesce(g.recipient_city, '')) = p.city AND left(coalesce(g.recipient_zip, ''), 5) = p.zip;
""")
    print(psql("SELECT 'linked: ' || count(recipient_ein) || ' of ' || count(*) || ' grants (' || round(100.0 * count(recipient_ein) / count(*)) || '%)' FROM grants"))

if __name__ == "__main__":
    args = dict(a.lstrip("-").split("=", 1) if "=" in a else (a.lstrip("-"), "") for a in sys.argv[1:])
    if "candidates" in args: candidates()
    if "sample" in args: sample(int(args["sample"] or 60))
    if "apply" in args: apply(args["apply"].split(","))
