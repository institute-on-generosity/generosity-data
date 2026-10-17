#!/usr/bin/env python3
"""Second-round links for federal_awards (migration 044) after etl/load_federal_awards.py's name-and-place
match (etl/org_matcher.py): by UEI, through the Federal Audit Clearinghouse UEI -> EIN crosswalk
(fac_uei_ein, etl/load_fac_crosswalk.py), and by name after abbreviation expansion. Incremental: only rows
the rules pick are updated, then fed_award_size_hist (046) is refreshed. Run again after any --match-only reload (that one resets every link).

  python3 etl/relink_federal.py --candidates     # build rf_cand (unlogged): proposed links per rule
  python3 etl/relink_federal.py --sample=60      # print a sample per rule for grading
  python3 etl/relink_federal.py --apply=uei,uei-stub,abbrev

Rules (graded Oct 17, 2026 by hand from name, place and revenue):
  uei       unlinked award, its UEI has one EIN in the FAC (not a multi-EIN auditee), that org is in the
            recipient's state and the names are similar (>= 0.3, USAspending or FAC name). 58 of 60 right
            (2 county "First Steps" partnerships went to the state board's group EIN)
  uei-stub  the award is linked by name to an org with no revenue on the IRS master file (a duplicate or
            stub EIN, e.g. "JOHN HOPKINS UNIVERSITY"), the FAC EIN for its UEI is an org with revenue in the
            same state, names similar (>= 0.5), "Foundation" in both or neither. 36 of 36 right
            (Johns Hopkins, Emory, Mayo Clinic, Columbia, Princeton, Dartmouth, Seattle Children's ...)
  abbrev    unlinked, same name after expanding abbreviations (FDN, ASSN, UNIV, CTR, SVCS, INTL, ST ...), one
            org in the state, same city or unique in the country (the 990-PF round-3 rule: 60 of 60 right)
Agreement check: where both exist, the FAC EIN equals the name-and-place link for 9,764 of 9,906 UEIs (98.6%).
"""
import os, subprocess, sys

sys.path.insert(0, os.path.dirname(__file__))
from rematch_grants import ABBR  # noqa: E402

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")

def psql(sql, *args):
    r = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qAt", *args], input=sql, text=True, capture_output=True)
    if r.returncode:
        sys.exit(f"psql failed: {r.stderr.strip()[:1500]}")
    return r.stdout.strip()

CANDIDATES = f"""
SET work_mem = '512MB';
{ABBR}
DROP TABLE IF EXISTS rf_cand;
CREATE UNLOGGED TABLE rf_cand (uei text, n text, city text, st text, cur char(9), ein char(9), method text, score real);
-- uei and uei-stub: one row per (UEI, current link)
WITH x AS (SELECT uei, min(ein) AS ein, min(name) AS fname, bool_or(multiple_eins) AS multi FROM fac_uei_ein GROUP BY uei HAVING count(*) = 1),
fa AS (SELECT recipient_uei AS uei, recipient_ein AS cur, min(recipient_name) AS nm, min(recipient_state) AS st
       FROM federal_awards WHERE recipient_uei IS NOT NULL GROUP BY 1, 2),
c AS (SELECT fa.*, x.ein, x.multi, o.state AS ost, o.revenue_amt AS orev, o.name AS oname, cu.name AS cname, cu.revenue_amt AS crev,
             greatest(similarity(org_norm(fa.nm), org_norm(o.name)), similarity(org_norm(x.fname), org_norm(o.name))) AS sim,
             similarity(org_norm(cu.name), org_norm(o.name)) AS csim
      FROM fa JOIN x USING (uei) JOIN orgs o ON o.ein = x.ein LEFT JOIN orgs cu ON cu.ein = fa.cur
      WHERE fa.cur IS NULL OR fa.cur <> x.ein)
INSERT INTO rf_cand
SELECT uei, NULL, NULL, st, cur, ein, CASE WHEN cur IS NULL THEN 'uei' ELSE 'uei-stub' END, CASE WHEN cur IS NULL THEN sim ELSE csim END
FROM c
WHERE (cur IS NULL AND NOT multi AND ost = st AND sim >= 0.3)
   OR (cur IS NOT NULL AND coalesce(crev, 0) = 0 AND coalesce(orev, 0) > 0 AND csim >= 0.5 AND ost = st
       AND (cname ~* 'FOUNDATION') = (oname ~* 'FOUNDATION'));
-- abbrev: unlinked rows by normalized name + city + state
CREATE TEMP TABLE ab_o AS SELECT ein, pg_temp.abbr_norm(org_norm(name)) AS a, org_norm(name) AS n, state AS st, upper(city) AS city FROM orgs;
CREATE INDEX ON ab_o (st, a); ANALYZE ab_o;
CREATE TEMP TABLE u AS SELECT DISTINCT org_norm(recipient_name) AS n, upper(coalesce(recipient_city, '')) AS city, recipient_state AS st
  FROM federal_awards WHERE recipient_ein IS NULL AND recipient_state IS NOT NULL;
INSERT INTO rf_cand
SELECT NULL, u.n, u.city, u.st, NULL, m.ein, 'abbrev', 1
FROM u JOIN LATERAL (SELECT min(o.ein) AS ein, count(*) AS k, bool_or(o.city = u.city) AS samecity, min(o.n) AS on_
                     FROM ab_o o WHERE o.st = u.st AND o.a = pg_temp.abbr_norm(u.n)) m ON m.k = 1
JOIN (SELECT a, count(*) AS nat FROM ab_o GROUP BY a) na ON na.a = pg_temp.abbr_norm(u.n)
WHERE m.on_ <> u.n AND (m.samecity OR na.nat = 1);
"""

def candidates():
    psql(CANDIDATES)
    print(psql("""SELECT method || ': ' || count(DISTINCT coalesce(uei, n || city || st)) || ' keys' FROM rf_cand GROUP BY method ORDER BY 1"""))

def sample(k):
    print(psql(f"""SELECT method, nm, st, oname, cname FROM (
        SELECT c.method, coalesce(f.recipient_name, c.n) AS nm, c.st, o.name AS oname, cu.name AS cname,
               row_number() OVER (PARTITION BY c.method ORDER BY md5(coalesce(c.uei, c.n || c.city))) AS r
        FROM rf_cand c JOIN orgs o ON o.ein = c.ein LEFT JOIN orgs cu ON cu.ein = c.cur
        LEFT JOIN LATERAL (SELECT recipient_name FROM federal_awards WHERE recipient_uei = c.uei LIMIT 1) f ON true) x
        WHERE r <= {k} ORDER BY method, r""", "-F", " | "))

def apply(methods):
    for m in ("uei", "uei-stub"):
        if m in methods:
            psql(f"""UPDATE federal_awards f SET recipient_ein = c.ein, match_method = c.method, match_score = c.score
                     FROM rf_cand c WHERE c.method = '{m}' AND f.recipient_uei = c.uei
                       AND f.recipient_ein IS NOT DISTINCT FROM c.cur;""")
    if "abbrev" in methods:
        psql("""UPDATE federal_awards f SET recipient_ein = c.ein, match_method = 'abbrev', match_score = 1
                FROM rf_cand c WHERE c.method = 'abbrev' AND f.recipient_ein IS NULL AND f.recipient_state = c.st
                  AND org_norm(f.recipient_name) = c.n AND upper(coalesce(f.recipient_city, '')) = c.city;
                ANALYZE federal_awards;""")
    # federal grant-size histograms (migration 046) carry recipient causes and sizes from the links
    psql("SET work_mem = '512MB'; REFRESH MATERIALIZED VIEW CONCURRENTLY fed_award_size_hist;")
    print(psql("""SELECT 'linked: ' || round(100.0 * count(recipient_ein) / count(*), 1) || '% of award-years, '
                  || round(100.0 * sum(amount) FILTER (WHERE recipient_ein IS NOT NULL) / sum(amount), 1) || '% of dollars, '
                  || count(DISTINCT recipient_ein) || ' EINs' FROM federal_awards"""))

if __name__ == "__main__":
    args = dict(a.lstrip("-").split("=", 1) if "=" in a else (a.lstrip("-"), "") for a in sys.argv[1:])
    if "candidates" in args: candidates()
    if "sample" in args: sample(int(args["sample"] or 60))
    if "apply" in args: apply(args["apply"].split(","))
