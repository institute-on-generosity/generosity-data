#!/usr/bin/env python3
"""Load private foundations and their grants from IRS 990-PF e-file XML into `funders` + `grants`,
then link each grant's recipient to `orgs`.

  python3 etl/load_grants.py [data/xml/2025pf]

The directory holds 990-PF *_public.xml files for EINs in `orgs` (extract them from the yearly
TEOS zips with RETURN_TYPE = 990PF; see README). Tags are read namespace-agnostically:
  funder   Filer, FMVAssetsEOYAmt, OnlyContriToPreselectedInd, WebsiteAddressTxt,
           ApplicationSubmissionInfoGrp (Part XV 2a-d)
  grants   SupplementaryInformationGrp/GrantOrContributionPdDurYrGrp (Part XV 3a: paid this year)
Recipient matching (in SQL, same state only; orgs covers the loaded states):
  exact       normalized name matches one org in the state                    score 1
  exact-city  normalized name matches several; the one in the same city wins   score 1
  fuzzy       trigram similarity >= 0.7 in the same city, >= 0.9 elsewhere     score = similarity (+0.1 same city)
"""
import csv, io, os, subprocess, sys, xml.etree.ElementTree as ET

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "data", "xml", "2025pf")
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
        grants.append([ein, year, name, first(g, "RecipientUSAddress/CityNm"), first(g, "RecipientUSAddress/StateAbbreviationCd")[:2],
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

def copy(table, cols, rows):
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows(rows)
    subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qc", f"\\copy {table} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')"],
                   input=out.getvalue(), text=True, check=True)

MATCH_SQL = f"""
UPDATE grants SET recipient_ein = NULL, match_score = NULL, match_method = NULL;
-- Normalize once: orgs in the loaded states, and each distinct recipient (name, city, state) once
CREATE TEMP TABLE o AS SELECT ein, org_norm(name) AS n, state AS st, upper(city) AS city FROM orgs;
CREATE INDEX ON o (st, n); CREATE INDEX ON o USING gin (n gin_trgm_ops); ANALYZE o;
CREATE TEMP TABLE r AS SELECT DISTINCT org_norm(recipient_name) AS n, upper(coalesce(recipient_city, '')) AS city, recipient_state AS st
  FROM grants WHERE recipient_state IN (SELECT DISTINCT st FROM o);
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
UPDATE grants g SET recipient_ein = r.ein, match_score = r.score, match_method = r.method
FROM r WHERE r.ein IS NOT NULL AND r.n = org_norm(g.recipient_name) AND r.st = g.recipient_state AND r.city = upper(coalesce(g.recipient_city, ''));
"""

def main():
    latest, bad = {}, 0  # ein -> (funder, grants); files sort by object id, which rises over time, so the last wins
    for f in sorted(os.listdir(SRC)):
        if not f.endswith(".xml"):
            continue
        try:
            r = parse(os.path.join(SRC, f))
        except ET.ParseError:
            bad += 1
            continue
        if r:
            latest[r[0][0]] = r
    subprocess.run(["psql", DB, "-qc", "TRUNCATE grants, funders RESTART IDENTITY"], check=True)
    copy("funders", "ein, object_id, tax_year, name, city, state, assets, grants_paid, grant_count, invite_only, website, "
         "apply_contact, apply_form, apply_deadlines, apply_restrictions", [f for f, _ in latest.values()])
    copy("grants", "funder_ein, tax_year, recipient_name, recipient_city, recipient_state, recipient_zip, amount, purpose, "
         "recipient_status, relationship", [g for _, gs in latest.values() for g in gs])
    print(f"funders: {len(latest)} · grants: {sum(len(gs) for _, gs in latest.values())} · unparseable files: {bad}")
    subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qc", MATCH_SQL], check=True)
    out = subprocess.run(["psql", DB, "-Atc", """SELECT count(*), count(recipient_ein),
        count(*) FILTER (WHERE recipient_state IN (SELECT DISTINCT state FROM orgs)),
        count(recipient_ein) FILTER (WHERE match_method = 'fuzzy') FROM grants"""], capture_output=True, text=True, check=True).stdout.strip()
    total, linked, in_states, fuzzy = map(int, out.split("|"))
    print(f"linked: {linked}/{total} grants ({linked / max(total, 1):.0%}); "
          f"{linked}/{in_states} to recipients in loaded states ({linked / max(in_states, 1):.0%}); fuzzy: {fuzzy}")

if __name__ == "__main__":
    main()
