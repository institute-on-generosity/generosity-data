#!/usr/bin/env python3
"""Load private foundations and their grants from IRS 990-PF e-file XML into `funders` + `grants`,
then link each grant's recipient to `orgs`.

  python3 etl/load_grants.py [data/xml/2025pf data/xml/2024pf data/xml/2023pf]
  python3 etl/load_grants.py --match-only        # relink recipients to orgs (e.g. after loading more states)
  python3 etl/load_grants.py --add data/xml/recovered/2025pf   # incremental: just these returns (see add())

Each directory holds one IRS processing year's 990-PF *_public.xml files, from every US foundation
(etl/fetch_990_xml.sh with TYPES=990PF). Several years load together:
  grants   one return per foundation per tax year (the newest filed, so amendments win); every grant
           keeps its tax_year and the object_id of the return it came from
  funders  one row per foundation, from its newest return
  skipped  pass-throughs: a return with 50,000+ grants (AmazonSmile paid 363,675 on one) relays shoppers'
           or donors' picks to nearly every charity; it isn't a funder anyone can approach
Tags are read namespace-agnostically:
  funder   Filer, FMVAssetsEOYAmt, OnlyContriToPreselectedInd, WebsiteAddressTxt,
           ApplicationSubmissionInfoGrp (Part XV 2a-d)
  grants   SupplementaryInformationGrp/GrantOrContributionPdDurYrGrp (Part XV 3a: paid this year)
Recipient matching (in SQL, same state only; orgs covers the loaded states):
  exact       normalized name matches one org in the state                    score 1
  exact-city  normalized name matches several; the one in the same city wins   score 1
  fuzzy       trigram similarity >= 0.7 in the same city, >= 0.9 elsewhere     score = similarity (+0.1 same city)
"""
import csv, functools, io, os, subprocess, sys, tempfile, time, xml.etree.ElementTree as ET
from multiprocessing import Pool

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
XML = os.path.join(os.path.dirname(__file__), "..", "data", "xml")
SRCS = [a for a in sys.argv[1:] if not a.startswith("--")] or sorted((os.path.join(XML, d) for d in os.listdir(XML) if d.endswith("pf")), reverse=True)
PASS_THROUGH = 50_000  # grants on one return: above this it's a donation pass-through (see above)
FUZZY = 0.6        # trigram candidates
CITY_FUZZY = 0.7   # accepted when the city matches

def local(tag):
    return tag.rsplit("}", 1)[-1]

def text(el):
    return " ".join((el.text or "").split()) if el is not None else ""

def find(root, path):
    nodes = [root]
    for part in path.split("/"):
        nodes = [c for n in nodes for c in n if local(c.tag) == part]
    return nodes

def first(root, *paths):
    for p in paths:
        hit = find(root, p)
        if hit and text(hit[0]):
            return text(hit[0])
    return ""

def num(s):
    try:
        return str(int(float(s)))
    except ValueError:
        return ""

def name_of(g):
    biz = find(g, "RecipientBusinessName")
    if biz:
        return " ".join(text(e) for e in biz[0] if local(e.tag).startswith("BusinessNameLine"))
    return first(g, "RecipientPersonNm")

def parse(path):
    root = ET.parse(path).getroot()
    hdr = find(root, "ReturnHeader")[0]
    if first(hdr, "ReturnTypeCd") != "990PF":
        return None
    pf = (find(root, "ReturnData/IRS990PF") or [None])[0]
    if pf is None:
        return None
    ein = first(hdr, "Filer/EIN").zfill(9)
    oid = os.path.basename(path).split("_")[0]
    year = first(hdr, "TaxYr")
    sup = (find(pf, "SupplementaryInformationGrp") or [pf])[0]
    app = (find(sup, "ApplicationSubmissionInfoGrp") or [None])[0]
    contact = ""
    if app is not None:
        who = first(app, "RecipientPersonNm", "RecipientBusinessName/BusinessNameLine1Txt")
        where = ", ".join(t for t in (first(app, "RecipientUSAddress/AddressLine1Txt"), first(app, "RecipientUSAddress/CityNm"),
                                     first(app, "RecipientUSAddress/StateAbbreviationCd")) if t)
        contact = " · ".join(t for t in (who, where, first(app, "RecipientPhoneNum"), first(app, "RecipientEmailAddressTxt")) if t)
    grants = []
    for g in find(sup, "GrantOrContributionPdDurYrGrp"):
        name = name_of(g)
        if not name:
            continue
        grants.append([ein, oid, year, name, first(g, "RecipientUSAddress/CityNm"), first(g, "RecipientUSAddress/StateAbbreviationCd")[:2],
                       first(g, "RecipientUSAddress/ZIPCd")[:5], num(first(g, "Amt")), first(g, "GrantOrContributionPurposeTxt")[:500],
                       first(g, "RecipientFoundationStatusTxt")[:40], first(g, "RecipientRelationshipTxt")[:80]])
    preselected = first(sup, "OnlyContriToPreselectedInd").upper() in ("X", "1", "TRUE")
    funder = [ein, oid, year, first(hdr, "Filer/BusinessName/BusinessNameLine1Txt"), first(hdr, "Filer/USAddress/CityNm"),
              first(hdr, "Filer/USAddress/StateAbbreviationCd")[:2], num(first(pf, "FMVAssetsEOYAmt")),
              num(first(sup, "TotalGrantOrContriPdDurYrAmt")), len(grants), "t" if preselected else "f",
              first(pf, "StatementsRegardingActyGrp/WebsiteAddressTxt", "WebsiteAddressTxt"), contact,
              first(app, "FormAndInfoAndMaterialsTxt") if app is not None else "",
              first(app, "SubmissionDeadlinesTxt") if app is not None else "",
              first(app, "RestrictionsOnAwardsTxt") if app is not None else ""]
    return funder, grants

def copy(table, cols, path):
    with open(path) as f:
        subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qc", f"\\copy {table} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')"],
                       stdin=f, text=True, check=True)

def safe_parse(path):
    try:
        return parse(path)
    except (ET.ParseError, IndexError):
        return "bad"

# Matching runs in three steps. The working tables are unlogged (not temp) so the fuzzy step can split
# across WORKERS psql sessions: nationally there are ~1.3M distinct recipients against ~2M orgs, which one
# session took days to match.
WORKERS = 8
PREP_SQL = f"""
\\timing on
UPDATE grants SET recipient_ein = NULL, match_score = NULL, match_method = NULL WHERE recipient_ein IS NOT NULL;
DROP TABLE IF EXISTS match_o, match_gs, match_r, match_f;
-- Normalize once: orgs, the grants to recipients in the loaded states (by id), and each distinct
-- recipient (name, city, state). Only grants into the loaded states can match.
CREATE UNLOGGED TABLE match_o AS SELECT ein, org_norm(name) AS n, state AS st, upper(city) AS city FROM orgs;
-- (state, trigram) in one index (btree_gin), so a fuzzy lookup only searches that state's orgs
CREATE INDEX ON match_o (st, n); CREATE INDEX ON match_o USING gin (st, n gin_trgm_ops); ANALYZE match_o;
CREATE UNLOGGED TABLE match_gs AS SELECT id, org_norm(recipient_name) AS n, upper(coalesce(recipient_city, '')) AS city, recipient_state AS st
  FROM grants WHERE recipient_state IN (SELECT DISTINCT st FROM match_o);
CREATE UNLOGGED TABLE match_r AS SELECT DISTINCT n, city, st FROM match_gs;
ALTER TABLE match_r ADD ein char(9), ADD score real, ADD method text; ANALYZE match_r;
-- exact normalized name, unique in the state
UPDATE match_r r SET ein = m.ein, score = 1, method = 'exact'
FROM (SELECT r.n, r.st, min(o.ein) AS ein FROM (SELECT DISTINCT n, st FROM match_r) r JOIN match_o o USING (st, n) GROUP BY 1, 2 HAVING count(*) = 1) m
WHERE r.n = m.n AND r.st = m.st;
-- ...but not in another city when the name repeats nationally (4+ orgs, or 2+ for churches/congregations):
-- graded 14% right Oct 15 ("Meals on Wheels", "Trinity Presbyterian Church" in another town)
UPDATE match_r r SET ein = NULL, score = NULL, method = NULL
FROM match_o o, (SELECT n, count(*) AS k FROM match_o GROUP BY n HAVING count(*) > 1) nat
WHERE r.method = 'exact' AND o.ein = r.ein AND o.city <> r.city AND nat.n = r.n
  AND (nat.k >= 4 OR r.n ~ '\\m(CHURCH|CONGREGATION|PARISH|MINISTRIES|MINISTRY|TEMPLE|CHAPEL|FELLOWSHIP|SYNAGOGUE|MOSQUE)\\M');
-- exact name, several orgs in the state: the one in the recipient's city
UPDATE match_r r SET ein = m.ein, score = 1, method = 'exact-city'
FROM (SELECT r.n, r.st, r.city, min(o.ein) AS ein FROM match_r r JOIN match_o o USING (st, n, city) WHERE r.ein IS NULL GROUP BY 1, 2, 3 HAVING count(*) = 1) m
WHERE r.n = m.n AND r.st = m.st AND r.city = m.city AND r.ein IS NULL;
CREATE UNLOGGED TABLE match_f (n text, city text, st text, ein char(9), score real);
"""
# fuzzy: best trigram match in the state, +0.1 when the city matches too; worker k takes its share of names.
# Candidates start at CITY_FUZZY, the lowest similarity ever accepted, so nothing is lost by the cutoff.
FUZZY_SQL = f"""
SET pg_trgm.similarity_threshold = {CITY_FUZZY};
INSERT INTO match_f
SELECT DISTINCT ON (r.n, r.st, r.city) r.n, r.city, r.st, o.ein,
       least(1, similarity(o.n, r.n) + CASE WHEN o.city = r.city THEN 0.1 ELSE 0 END)::real AS score
FROM match_r r JOIN match_o o ON o.n % r.n AND o.st = r.st
WHERE r.ein IS NULL AND length(r.n) >= 6 AND abs(hashtext(r.n)) % {WORKERS} = :k
  -- same city only: other-city links graded 12% right (Oct 15; generic names like "Life Church")
  AND o.city = r.city AND similarity(o.n, r.n) >= {CITY_FUZZY}
ORDER BY r.n, r.st, r.city, score DESC;
"""
WRITE_SQL = """
\\timing on
UPDATE match_r r SET ein = f.ein, score = f.score, method = 'fuzzy' FROM match_f f WHERE r.n = f.n AND r.st = f.st AND r.city = f.city;
-- write the links back by grant id
CREATE INDEX ON match_r (st, n, city); ANALYZE match_gs;
UPDATE grants g SET recipient_ein = r.ein, match_score = r.score, match_method = r.method
FROM match_gs gs JOIN match_r r USING (n, city, st) WHERE r.ein IS NOT NULL AND g.id = gs.id;
DROP TABLE match_o, match_gs, match_r, match_f;
-- never-linkable grants: foreign, individual, placeholder (migration 024)
UPDATE grants SET recipient_kind = recipient_kind_of(recipient_name, recipient_state)
WHERE recipient_ein IS NULL AND recipient_entity IS NULL AND recipient_kind IS NULL
  AND recipient_kind_of(recipient_name, recipient_state) IS NOT NULL;
"""

print = functools.partial(print, flush=True)  # progress shows up when piped to a log

def match():
    """Link grant recipients to orgs, timing each step (python3 etl/load_grants.py --match-only reruns just this)."""
    def run(sql, *args):
        return subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-q", *args], input=sql, text=True, capture_output=True)
    def check(step, out):
        print(f"{step}: " + " · ".join(l.replace("Time: ", "").split(" (")[0] for l in out.stdout.splitlines() if l.startswith("Time:")), flush=True)
        if out.returncode: sys.exit(f"psql failed: {out.stderr.strip()[:2000]}")
    check("prepare + exact", run(PREP_SQL))
    t0 = time.time()
    procs = [subprocess.Popen(["psql", DB, "-v", "ON_ERROR_STOP=1", "-q", "-v", f"k={k}"], stdin=subprocess.PIPE, text=True, stderr=subprocess.PIPE)
             for k in range(WORKERS)]
    for p in procs: p.stdin.write(FUZZY_SQL); p.stdin.close()
    errs = [p.stderr.read() for p in procs if p.wait()]
    if errs: sys.exit(f"psql failed: {errs[0].strip()[:2000]}")
    print(f"fuzzy ({WORKERS} workers): {time.time() - t0:.0f}s", flush=True)
    check("write back", run(WRITE_SQL))
    report()

def report():
    out = subprocess.run(["psql", DB, "-Atc", """SELECT count(*), count(recipient_ein),
        count(*) FILTER (WHERE recipient_state IN (SELECT DISTINCT state FROM orgs)),
        count(recipient_ein) FILTER (WHERE match_method = 'fuzzy') FROM grants"""], capture_output=True, text=True, check=True).stdout.strip()
    total, linked, in_states, fuzzy = map(int, out.split("|"))
    print(f"linked: {linked}/{total} grants ({linked / max(total, 1):.0%}); "
          f"{linked}/{in_states} to recipients in loaded states ({linked / max(in_states, 1):.0%}); fuzzy: {fuzzy}")

def psql_out(sql):
    r = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qAt"], input=sql, text=True, capture_output=True)
    if r.returncode: sys.exit(f"psql failed: {r.stderr.strip()[:2000]}")
    return r.stdout.strip()

# Incremental links for rows an --add load wrote (id > :after). A recipient already in grants (same normalized
# name, city, state, ZIP) takes the link most of its earlier grants carry, whatever rule or review made it;
# new recipients get the overrides, then the exact / exact-city / fuzzy rules (etl/org_matcher.py), public
# entities (etl/load_public_entities.py) and recipient_kind_of(). The later rules (alias, zip, dba, splink,
# Claude review: etl/rematch_grants.py, etl/claude_match) need their own rerun.
ADD_REUSE_SQL = """
SET work_mem = '1GB';
DROP TABLE IF EXISTS add_new, add_seen;
CREATE UNLOGGED TABLE add_new AS
SELECT id, org_norm(recipient_name) AS n, upper(coalesce(recipient_city, '')) AS city, coalesce(recipient_state, '') AS st,
       left(coalesce(recipient_zip, ''), 5) AS zip FROM grants WHERE id > :after;
CREATE INDEX ON add_new (st, n); ANALYZE add_new;
CREATE UNLOGGED TABLE add_seen AS
SELECT DISTINCT ON (k.n, k.city, k.st, k.zip) k.n, k.city, k.st, k.zip, g.recipient_ein, g.recipient_entity, g.match_method,
       g.match_score, g.recipient_kind
FROM (SELECT DISTINCT n, city, st, zip FROM add_new) k
JOIN grants g ON g.id <= :after AND coalesce(g.recipient_state, '') = k.st AND upper(coalesce(g.recipient_city, '')) = k.city
  AND left(coalesce(g.recipient_zip, ''), 5) = k.zip AND org_norm(g.recipient_name) = k.n
GROUP BY k.n, k.city, k.st, k.zip, g.recipient_ein, g.recipient_entity, g.match_method, g.match_score, g.recipient_kind
ORDER BY k.n, k.city, k.st, k.zip, count(*) DESC, g.recipient_ein NULLS LAST;
UPDATE grants g SET recipient_ein = s.recipient_ein, recipient_entity = s.recipient_entity, match_method = s.match_method,
       match_score = s.match_score, recipient_kind = s.recipient_kind
FROM add_new a JOIN add_seen s USING (n, city, st, zip) WHERE g.id = a.id;
DELETE FROM add_new a USING add_seen s WHERE a.n = s.n AND a.city = s.city AND a.st = s.st AND a.zip = s.zip;
SELECT count(*) FROM add_new;
"""
ADD_RULES_SQL = """
UPDATE grants g SET recipient_ein = ov.ein, match_method = 'override', match_score = 1
FROM add_new a JOIN LATERAL (SELECT ein FROM grant_match_overrides o WHERE o.recipient_norm = a.n AND o.recipient_state IN (a.st, '')
                             ORDER BY o.recipient_state DESC LIMIT 1) ov ON true
WHERE g.id = a.id AND g.recipient_ein IS NULL;
"""

def add(dirs):
    """--add DIR...: load only the 990-PFs in DIR (e.g. data/xml/recovered/2025pf from etl/recover_missing_xml.py)
    with the full load's rules: per (foundation, tax year) the newest return wins, so a new return replaces an older
    one already loaded and is skipped when a newer one is; the funder row follows the foundation's newest return."""
    meta = {}
    for idx in os.listdir(XML):
        if idx.startswith("index_") and idx.endswith(".csv"):
            for r in csv.DictReader(open(os.path.join(XML, idx), encoding="latin-1")):
                if r["RETURN_TYPE"] == "990PF":
                    meta[r["OBJECT_ID"]] = (r["EIN"].zfill(9), r["TAX_PERIOD"][:4])
    disk = {}  # newest return per (foundation, period) among everything the full load reads
    for d in os.listdir(XML):
        if d.endswith("pf") and os.path.isdir(os.path.join(XML, d)):
            for f in os.listdir(os.path.join(XML, d)):
                oid = f.split("_")[0]
                if oid in meta and (meta[oid] not in disk or oid > disk[meta[oid]]):
                    disk[meta[oid]] = oid
    files = {}
    for src in dirs:
        for f in os.listdir(src):
            oid = f.split("_")[0]
            if f.endswith(".xml") and oid in meta and disk.get(meta[oid]) == oid:
                files[meta[oid]] = (oid, os.path.join(src, f))
    funder_now = {}
    for line in psql_out("SELECT ein || '|' || object_id FROM funders").split():
        ein, oid = line.split("|")
        funder_now[ein] = (meta.get(oid, (ein, ""))[1], oid)
    take = files
    replaced = [oid for oid in psql_out("SELECT DISTINCT object_id FROM grants").split()
                if oid in meta and meta[oid] in take and oid != take[meta[oid]][0]]
    print(f"{len(take)} returns in {', '.join(dirs)} are the newest for their foundation and year "
          f"({len(replaced)} replace a loaded return)")
    tmp = tempfile.mkdtemp(prefix="grants-add-", dir=XML)
    fpath, gpath, rpath = (os.path.join(tmp, f) for f in ("funders.csv", "grants.csv", "replaced.txt"))
    n_f = n_g = bad = skipped = 0
    newest = {}  # ein -> funder row from its newest added return, when newer than the loaded one
    with open(fpath, "w") as ff, open(gpath, "w") as gf, Pool() as pool:
        fw, gw = csv.writer(ff, lineterminator="\n"), csv.writer(gf, lineterminator="\n")
        for r in pool.imap_unordered(safe_parse, [p for _, p in take.values()], chunksize=64):
            if r == "bad":
                bad += 1; continue
            if not r:
                continue
            funder, grants = r
            if len(grants) > PASS_THROUGH:
                skipped += 1; continue
            ein, oid = funder[0], funder[1]
            if ein not in funder_now or (meta[oid][1], oid) > funder_now[ein]:
                funder_now[ein] = (meta[oid][1], oid)
                newest[ein] = funder
            gw.writerows(grants); n_g += len(grants)
        fw.writerows(newest.values()); n_f = len(newest)
    with open(rpath, "w") as f:
        f.write("".join(o + "\n" for o in replaced))
    after = psql_out("SELECT coalesce(max(id), 0) FROM grants")
    cols = "funder_ein, object_id, tax_year, recipient_name, recipient_city, recipient_state, recipient_zip, amount, purpose, recipient_status, relationship"
    fcols = ("ein, object_id, tax_year, name, city, state, assets, grants_paid, grant_count, invite_only, website, "
             "apply_contact, apply_form, apply_deadlines, apply_restrictions")
    out = psql_out(f"""BEGIN;
CREATE TEMP TABLE f_raw (LIKE funders INCLUDING DEFAULTS);
\\copy f_raw ({fcols}) FROM '{fpath}' WITH (FORMAT csv, NULL '')
INSERT INTO funders ({fcols}) SELECT {fcols} FROM f_raw
ON CONFLICT (ein) DO UPDATE SET {", ".join(f"{c} = EXCLUDED.{c}" for c in fcols.split(", ")[1:])};
CREATE TEMP TABLE replaced (object_id text);
\\copy replaced FROM '{rpath}'
DELETE FROM grants WHERE object_id IN (SELECT object_id FROM replaced);
CREATE TEMP TABLE g_raw (LIKE grants INCLUDING DEFAULTS);
\\copy g_raw ({cols}) FROM '{gpath}' WITH (FORMAT csv, NULL '')
INSERT INTO grants ({cols}) SELECT {cols} FROM g_raw WHERE funder_ein IN (SELECT ein FROM funders) ORDER BY funder_ein, object_id;
SELECT count(*) FROM grants WHERE id > {after};
COMMIT;""")
    for p in (fpath, gpath, rpath): os.remove(p)
    os.rmdir(tmp)
    print(f"funders written: {n_f} · grants parsed: {n_g} · inserted: {out.splitlines()[-1]} · returns replaced: {len(replaced)} "
          f"· unparseable: {bad} · pass-throughs skipped: {skipped} · new grant ids > {after}")
    link_added(int(after))

def link_added(after):
    """Link grants with id > after (see ADD_REUSE_SQL)."""
    t0 = time.time()
    left = psql_out(ADD_REUSE_SQL.replace(":after", str(after))).splitlines()[-1]
    print(f"reused earlier links; {left} added rows have a recipient not seen before ({time.time() - t0:.0f}s)")
    psql_out(ADD_RULES_SQL)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from org_matcher import link
    import load_public_entities
    link("grants", "gadd", where="id IN (SELECT id FROM add_new)")
    print(psql_out(load_public_entities.MATCH.replace("WHERE g.recipient_ein IS NULL",
                                                      "WHERE g.id IN (SELECT id FROM add_new) AND g.recipient_ein IS NULL")))
    psql_out("""UPDATE grants g SET recipient_kind = recipient_kind_of(g.recipient_name, g.recipient_state) FROM add_new a
WHERE g.id = a.id AND g.recipient_ein IS NULL AND g.recipient_entity IS NULL AND g.recipient_kind IS NULL
  AND recipient_kind_of(g.recipient_name, g.recipient_state) IS NOT NULL;
DROP TABLE add_new, add_seen;""")
    print(psql_out(f"""SELECT 'added rows linked: ' || count(recipient_ein) || ' of ' || count(*) || ' (' || round(100.0 * count(recipient_ein) / count(*), 1)
        || '%); by method: ' || (SELECT string_agg(coalesce(match_method, 'none') || ' ' || k, ', ' ORDER BY k DESC)
        FROM (SELECT match_method, count(*) AS k FROM grants WHERE id > {after} GROUP BY 1) m) FROM grants WHERE id > {after}"""))

def main():
    if "--match-only" in sys.argv:
        return match()
    if "--add" in sys.argv:
        return add([a for a in sys.argv[1:] if not a.startswith("--")])
    if any(a.startswith("--link-added=") for a in sys.argv):
        # rerun the incremental links: --link-added=<the max grant id before the --add load>
        return link_added(int(next(a for a in sys.argv if a.startswith("--link-added=")).split("=")[1]))
    # Which return to read for each (foundation, tax year): the newest filed. Object ids rise over time,
    # and each year's index gives the EIN and tax period without opening the file.
    files = {}
    for src in SRCS:
        year = os.path.basename(os.path.normpath(src))[:4]
        meta = {r["OBJECT_ID"]: (r["EIN"].zfill(9), r["TAX_PERIOD"][:4]) for r in csv.DictReader(open(os.path.join(XML, f"index_{year}.csv"), encoding="latin-1"))
                if r["RETURN_TYPE"] == "990PF"}
        for f in os.listdir(src):
            oid = f.split("_")[0]
            if f.endswith(".xml") and oid in meta:
                key = meta[oid]
                if key not in files or oid > files[key][0]:
                    files[key] = (oid, os.path.join(src, f))
    newest = {}  # ein -> (tax period, oid): the return the funder row comes from
    for (ein, period), (oid, _) in files.items():
        if ein not in newest or (period, oid) > newest[ein]:
            newest[ein] = (period, oid)
    print(f"{len(files)} returns from {len(newest)} foundations in {', '.join(SRCS)}")

    tmp = tempfile.mkdtemp(prefix="grants-", dir=XML)
    fpath, gpath = os.path.join(tmp, "funders.csv"), os.path.join(tmp, "grants.csv")
    n_f = n_g = bad = 0
    skipped = set()
    with open(fpath, "w") as ff, open(gpath, "w") as gf, Pool() as pool:
        fw, gw = csv.writer(ff, lineterminator="\n"), csv.writer(gf, lineterminator="\n")
        for r in pool.imap_unordered(safe_parse, [p for _, p in files.values()], chunksize=64):
            if r == "bad":
                bad += 1
                continue
            if not r:
                continue
            funder, grants = r
            if len(grants) > PASS_THROUGH:
                skipped.add(funder[0])
                continue
            if newest.get(funder[0], (None, None))[1] == funder[1]:
                fw.writerow(funder); n_f += 1
            gw.writerows(grants); n_g += len(grants)
    if skipped:  # a pass-through in any year: drop it from every year
        for path in (fpath, gpath):
            rows = [r for r in csv.reader(open(path)) if r[0] not in skipped]
            csv.writer(open(path, "w"), lineterminator="\n").writerows(rows)
            n_f, n_g = (len(rows), n_g) if path == fpath else (n_f, len(rows))
    subprocess.run(["psql", DB, "-qc", "TRUNCATE grants, funders RESTART IDENTITY"], check=True)
    copy("funders", "ein, object_id, tax_year, name, city, state, assets, grants_paid, grant_count, invite_only, website, "
         "apply_contact, apply_form, apply_deadlines, apply_restrictions", fpath)
    # A foundation whose newest return didn't parse has no funder row: drop its older grants too.
    subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-q"], text=True, check=True, input=f"""CREATE TEMP TABLE g_raw (LIKE grants INCLUDING DEFAULTS);
\\copy g_raw (funder_ein, object_id, tax_year, recipient_name, recipient_city, recipient_state, recipient_zip, amount, purpose, recipient_status, relationship) FROM '{gpath}' WITH (FORMAT csv, NULL '')
INSERT INTO grants (funder_ein, object_id, tax_year, recipient_name, recipient_city, recipient_state, recipient_zip, amount, purpose, recipient_status, relationship)
SELECT funder_ein, object_id, tax_year, recipient_name, recipient_city, recipient_state, recipient_zip, amount, purpose, recipient_status, relationship
FROM g_raw WHERE funder_ein IN (SELECT ein FROM funders);
""")
    for p in (fpath, gpath): os.remove(p)
    os.rmdir(tmp)
    print(f"funders: {n_f} · grants: {n_g} · unparseable files: {bad} · pass-throughs skipped: {len(skipped)}")
    match()

if __name__ == "__main__":
    main()
