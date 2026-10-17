"""Link recipient names on any table to `orgs`, with the 990-PF matcher's rules (etl/load_grants.py; see
its comments for the grading behind each guard). Used by etl/load_charity_grants.py and
etl/load_federal_awards.py. The table needs id, recipient_name, recipient_city, recipient_state,
recipient_ein, match_score, match_method; only rows with recipient_ein still NULL are matched.

  exact       normalized name (org_norm) matches one org in the state; not another city when the name
              repeats nationally (4+ orgs, or 2+ for congregations)                  score 1
  exact-city  the name matches several orgs in the state; the one in the same city   score 1
  fuzzy       best trigram match in the same city, similarity >= 0.7                 score = similarity + 0.1
Work tables are unlogged and prefixed, so a run can't collide with a 990-PF relink.
"""
import functools, os, subprocess, sys, time

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
WORKERS = 8
print = functools.partial(print, flush=True)

def _psql(sql, *args):
    r = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-q", *args], input=sql, text=True, capture_output=True)
    if r.returncode:
        sys.exit(f"psql failed: {r.stderr.strip()[:2000]}")
    return r.stdout

def _times(step, out):
    print(f"{step}: " + " · ".join(l.replace("Time: ", "").split(" (")[0] for l in out.splitlines() if l.startswith("Time:")))

def link(table, prefix, pre="", where="true"):
    """pre: SQL run first (reset links, link by a reported EIN). Then exact, exact-city and fuzzy.
    where: limits which rows are matched (e.g. "id > 123" for rows an incremental load just added)."""
    t, x = table, prefix
    _times("prepare + exact", _psql(rf"""
\timing on
SET work_mem = '1GB';
{pre}
DROP TABLE IF EXISTS {x}_o, {x}_gs, {x}_r, {x}_f;
CREATE UNLOGGED TABLE {x}_o AS SELECT ein, org_norm(name) AS n, state AS st, upper(city) AS city FROM orgs;
CREATE INDEX ON {x}_o (st, n); CREATE INDEX ON {x}_o USING gin (st, n gin_trgm_ops); ANALYZE {x}_o;
CREATE UNLOGGED TABLE {x}_gs AS SELECT id, org_norm(recipient_name) AS n, upper(coalesce(recipient_city, '')) AS city, recipient_state AS st
  FROM {t} WHERE recipient_ein IS NULL AND ({where}) AND recipient_state IN (SELECT DISTINCT st FROM {x}_o);
CREATE UNLOGGED TABLE {x}_r AS SELECT DISTINCT n, city, st FROM {x}_gs;
ALTER TABLE {x}_r ADD ein char(9), ADD score real, ADD method text; ANALYZE {x}_r;
UPDATE {x}_r r SET ein = m.ein, score = 1, method = 'exact'
FROM (SELECT r.n, r.st, min(o.ein) AS ein FROM (SELECT DISTINCT n, st FROM {x}_r) r JOIN {x}_o o USING (st, n) GROUP BY 1, 2 HAVING count(*) = 1) m
WHERE r.n = m.n AND r.st = m.st;
UPDATE {x}_r r SET ein = NULL, score = NULL, method = NULL
FROM {x}_o o, (SELECT n, count(*) AS k FROM {x}_o GROUP BY n HAVING count(*) > 1) nat
WHERE r.method = 'exact' AND o.ein = r.ein AND o.city <> r.city AND nat.n = r.n
  AND (nat.k >= 4 OR r.n ~ '\m(CHURCH|CONGREGATION|PARISH|MINISTRIES|MINISTRY|TEMPLE|CHAPEL|FELLOWSHIP|SYNAGOGUE|MOSQUE)\M');
UPDATE {x}_r r SET ein = m.ein, score = 1, method = 'exact-city'
FROM (SELECT r.n, r.st, r.city, min(o.ein) AS ein FROM {x}_r r JOIN {x}_o o USING (st, n, city) WHERE r.ein IS NULL GROUP BY 1, 2, 3 HAVING count(*) = 1) m
WHERE r.n = m.n AND r.st = m.st AND r.city = m.city AND r.ein IS NULL;
CREATE UNLOGGED TABLE {x}_f (n text, city text, st text, ein char(9), score real);
"""))
    t0 = time.time()
    procs = [subprocess.Popen(["psql", DB, "-v", "ON_ERROR_STOP=1", "-q"], stdin=subprocess.PIPE, text=True, stderr=subprocess.PIPE)
             for _ in range(WORKERS)]
    for k, p in enumerate(procs):
        p.stdin.write(f"""
SET pg_trgm.similarity_threshold = 0.7;
INSERT INTO {x}_f
SELECT DISTINCT ON (r.n, r.st, r.city) r.n, r.city, r.st, o.ein,
       least(1, similarity(o.n, r.n) + CASE WHEN o.city = r.city THEN 0.1 ELSE 0 END)::real AS score
FROM {x}_r r JOIN {x}_o o ON o.n % r.n AND o.st = r.st
WHERE r.ein IS NULL AND length(r.n) >= 6 AND abs(hashtext(r.n)) % {WORKERS} = {k}
  AND o.city = r.city AND similarity(o.n, r.n) >= 0.7
ORDER BY r.n, r.st, r.city, score DESC;
""")
        p.stdin.close()
    errs = [p.stderr.read() for p in procs if p.wait()]
    if errs:
        sys.exit(f"psql failed: {errs[0].strip()[:2000]}")
    print(f"fuzzy ({WORKERS} workers): {time.time() - t0:.0f}s")
    _times("write back", _psql(f"""
\\timing on
UPDATE {x}_r r SET ein = f.ein, score = f.score, method = 'fuzzy' FROM {x}_f f WHERE r.n = f.n AND r.st = f.st AND r.city = f.city;
CREATE INDEX ON {x}_r (st, n, city); ANALYZE {x}_gs;
UPDATE {t} g SET recipient_ein = r.ein, match_score = r.score, match_method = r.method
FROM {x}_gs gs JOIN {x}_r r USING (n, city, st) WHERE r.ein IS NOT NULL AND g.id = gs.id;
DROP TABLE {x}_o, {x}_gs, {x}_r, {x}_f;
ANALYZE {t};
"""))
    print(_psql(f"""SELECT 'linked: ' || count(recipient_ein) || ' of ' || count(*) || ' rows (' || round(100.0 * count(recipient_ein) / count(*), 1)
        || '%), dollars ' || round(100.0 * sum(amount) FILTER (WHERE recipient_ein IS NOT NULL) / nullif(sum(amount), 0), 1) || '%; by method: '
        || (SELECT string_agg(coalesce(match_method, 'none') || ' ' || k, ', ' ORDER BY k DESC) FROM (SELECT match_method, count(*) AS k FROM {t} GROUP BY 1) m)
        FROM {t}""", "-At").strip())
