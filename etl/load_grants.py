#!/usr/bin/env python3
"""Load private foundations and their grants from IRS 990-PF e-file XML into `funders` + `grants`,
then link each grant's recipient to `orgs`.

  python3 etl/load_grants.py [data/xml/2025pf data/xml/2024pf data/xml/2023pf]
  python3 etl/load_grants.py --match-only        # relink recipients to orgs (e.g. after loading more states)

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
import csv, functools, io, os, subprocess, sys, tempfile, xml.etree.ElementTree as ET
from multiprocessing import Pool

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
XML = os.path.join(os.path.dirname(__file__), "..", "data", "xml")
SRCS = [a for a in sys.argv[1:] if not a.startswith("--")] or sorted((os.path.join(XML, d) for d in os.listdir(XML) if d.endswith("pf")), reverse=True)
PASS_THROUGH = 50_000  # grants on one return: above this it's a donation pass-through (see above)
FUZZY = 0.6        # trigram candidates
CITY_FUZZY = 0.7   # accepted when the city matches
FAR_FUZZY = 0.9    # accepted from another city in the state

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

MATCH_SQL = f"""
\\timing on
UPDATE grants SET recipient_ein = NULL, match_score = NULL, match_method = NULL WHERE recipient_ein IS NOT NULL;
-- Normalize once: orgs in the loaded states, the grants to recipients there (by id), and each distinct
-- recipient (name, city, state). Only grants into the loaded states can match, so the rest are never read twice.
CREATE TEMP TABLE o AS SELECT ein, org_norm(name) AS n, state AS st, upper(city) AS city FROM orgs;
CREATE INDEX ON o (st, n); CREATE INDEX ON o USING gin (n gin_trgm_ops); ANALYZE o;
CREATE TEMP TABLE gs AS SELECT id, org_norm(recipient_name) AS n, upper(coalesce(recipient_city, '')) AS city, recipient_state AS st
  FROM grants WHERE recipient_state IN (SELECT DISTINCT st FROM o);
CREATE TEMP TABLE r AS SELECT DISTINCT n, city, st FROM gs;
ALTER TABLE r ADD ein char(9), ADD score real, ADD method text; ANALYZE r;
-- exact normalized name, unique in the state
UPDATE r SET ein = m.ein, score = 1, method = 'exact'
FROM (SELECT r.n, r.st, min(o.ein) AS ein FROM (SELECT DISTINCT n, st FROM r) r JOIN o USING (st, n) GROUP BY 1, 2 HAVING count(*) = 1) m
WHERE r.n = m.n AND r.st = m.st;
-- exact name, several orgs in the state: the one in the recipient's city
UPDATE r SET ein = m.ein, score = 1, method = 'exact-city'
FROM (SELECT r.n, r.st, r.city, min(o.ein) AS ein FROM r JOIN o USING (st, n, city) WHERE r.ein IS NULL GROUP BY 1, 2, 3 HAVING count(*) = 1) m
WHERE r.n = m.n AND r.st = m.st AND r.city = m.city AND r.ein IS NULL;
-- fuzzy: best trigram match in the state, +0.1 when the city matches too
SET pg_trgm.similarity_threshold = {FUZZY};
UPDATE r SET ein = m.ein, score = m.score, method = 'fuzzy'
FROM (SELECT DISTINCT ON (r.n, r.st, r.city) r.n, r.st, r.city, o.ein,
        least(1, similarity(o.n, r.n) + CASE WHEN o.city = r.city THEN 0.1 ELSE 0 END)::real AS score
      FROM r JOIN o ON o.n % r.n AND o.st = r.st
      WHERE r.ein IS NULL AND length(r.n) >= 6
        AND similarity(o.n, r.n) >= CASE WHEN o.city = r.city THEN {CITY_FUZZY} ELSE {FAR_FUZZY} END
      ORDER BY r.n, r.st, r.city, score DESC) m
WHERE r.n = m.n AND r.st = m.st AND r.city = m.city;
-- write the links back by grant id
CREATE INDEX ON r (st, n, city); ANALYZE gs;
UPDATE grants g SET recipient_ein = r.ein, match_score = r.score, match_method = r.method
FROM gs JOIN r USING (n, city, st) WHERE r.ein IS NOT NULL AND g.id = gs.id;
"""

print = functools.partial(print, flush=True)  # progress shows up when piped to a log

def match():
    """Link grant recipients to orgs, timing each step (python3 etl/load_grants.py --match-only reruns just this)."""
    out = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-q"], input=MATCH_SQL, text=True, capture_output=True)
    print(" · ".join(l.replace("Time: ", "").split(" (")[0] for l in out.stdout.splitlines() if l.startswith("Time:")))
    if out.returncode: sys.exit(f"psql failed: {out.stderr.strip()[:2000]}")
    report()

def report():
    out = subprocess.run(["psql", DB, "-Atc", """SELECT count(*), count(recipient_ein),
        count(*) FILTER (WHERE recipient_state IN (SELECT DISTINCT state FROM orgs)),
        count(recipient_ein) FILTER (WHERE match_method = 'fuzzy') FROM grants"""], capture_output=True, text=True, check=True).stdout.strip()
    total, linked, in_states, fuzzy = map(int, out.split("|"))
    print(f"linked: {linked}/{total} grants ({linked / max(total, 1):.0%}); "
          f"{linked}/{in_states} to recipients in loaded states ({linked / max(in_states, 1):.0%}); fuzzy: {fuzzy}")

def main():
    if "--match-only" in sys.argv:
        return match()
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
