#!/usr/bin/env python3
"""Load Schedule I grants (Form 990, Part II: grants to domestic organizations and governments) into
`charity_filers` + `charity_grants` (migration 041), then link recipients to `orgs`.

  python3 etl/load_charity_grants.py [data/xml/2026 data/xml/2025 data/xml/2024 data/xml/2023]
  python3 etl/load_charity_grants.py --match-only     # relink recipients only

Reads the Form 990 e-file XML that etl/fetch_990_xml.sh extracts into data/xml/<year> (990 and 990-EZ for
EINs in orgs; YEAR=2023 TYPES=990 etl/fetch_990_xml.sh for years loaded only for this). Only returns of type
990 with an IRS990ScheduleI are read; 990-EZ filers don't file Schedule I. One return per filer per tax
period: the newest filed (highest object id), as in etl/load_grants.py.
  filer   ReturnHeader Filer; IRS990 DonorAdvisedFundInd (Part IV line 6), GrantsToDomesticOrgsGrp/TotalAmt
          (Part IX line 1); IRS990ScheduleD DonorAdvisedFunds{HeldCnt,ContriAmt,GrantsAmt,VlEOYAmt} (Part I)
  grants  IRS990ScheduleI/RecipientTable: name, US or foreign address, RecipientEIN, IRCSectionDesc,
          CashGrantAmt, NonCashAssistanceAmt, NonCashAssistanceDesc, PurposeOfGrantTxt
          Part III (grants to individuals) is not loaded.
Recipient linking:
  ein         the reported RecipientEIN is an org in `orgs`                         score 1
  then etl/org_matcher.py (the 990-PF matcher's rules), same state only, for lines with no usable EIN:
  exact, exact-city, fuzzy (same city, similarity >= 0.7)
"""
import csv, functools, os, subprocess, sys, tempfile, time, xml.etree.ElementTree as ET
sys.path.insert(0, os.path.dirname(__file__))
from multiprocessing import Pool

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
XML = os.path.join(os.path.dirname(__file__), "..", "data", "xml")
SRCS = [a for a in sys.argv[1:] if not a.startswith("--")] or sorted(
    (os.path.join(XML, d) for d in os.listdir(XML) if d.isdigit() and os.path.isdir(os.path.join(XML, d))), reverse=True)
print = functools.partial(print, flush=True)

def local(tag):
    return tag.rsplit("}", 1)[-1]

def text(el):
    return " ".join((el.text or "").split()) if el is not None else ""

def child(node, *names):
    """First descendant along a path of direct-child tag names."""
    for name in names:
        node = next((c for c in node if local(c.tag) == name), None) if node is not None else None
    return node

def desc(node, name):
    return next((e for e in node.iter() if local(e.tag) == name), None) if node is not None else None

def num(s):
    try:
        return str(int(round(float(s))))
    except (TypeError, ValueError):
        return ""

def flag(s):
    s = (s or "").strip().lower()
    return "t" if s in ("1", "true", "x") else "f" if s in ("0", "false") else ""

def parse(path):
    with open(path, "rb") as f:
        raw = f.read()
    if b"IRS990ScheduleI" not in raw:
        return None
    root = ET.fromstring(raw)
    hdr = child(root, "ReturnHeader")
    if hdr is None or text(child(hdr, "ReturnTypeCd")) != "990":
        return None
    data = child(root, "ReturnData")
    f990 = child(data, "IRS990")
    ein = text(child(hdr, "Filer", "EIN")).zfill(9)
    oid = os.path.basename(path).split("_")[0]
    year = text(child(hdr, "TaxYr"))
    filer_name = text(child(hdr, "Filer", "BusinessName", "BusinessNameLine1Txt"))
    addr = child(hdr, "Filer", "USAddress")
    city, state = (text(child(addr, "CityNm")), text(child(addr, "StateAbbreviationCd"))[:2]) if addr is not None else ("", "")
    sd = child(data, "IRS990ScheduleD")
    grants = []
    for si in (c for c in data if local(c.tag) == "IRS990ScheduleI"):
        for r in (c for c in si if local(c.tag) == "RecipientTable"):
            biz = child(r, "RecipientBusinessName")
            name = " ".join(text(e) for e in biz if local(e.tag).startswith("BusinessNameLine")) if biz is not None else ""
            if not name:
                continue
            us, fa = child(r, "USAddress"), child(r, "ForeignAddress")
            if us is not None:
                rc, rs, rz, country = text(child(us, "CityNm")), text(child(us, "StateAbbreviationCd"))[:2], text(child(us, "ZIPCd"))[:5], ""
            elif fa is not None:
                rc, rs, rz, country = text(child(fa, "CityNm")), "", "", text(child(fa, "CountryCd"))[:2] or "??"
            else:
                rc = rs = rz = country = ""
            rein = "".join(ch for ch in text(child(r, "RecipientEIN")) if ch.isdigit())
            cash, nonc = num(text(child(r, "CashGrantAmt"))), num(text(child(r, "NonCashAssistanceAmt")))
            amt = str(int(cash or 0) + int(nonc or 0)) if (cash or nonc) else ""
            grants.append([ein, oid, year, name[:300], rc[:80], rs, rz, country, rein if len(rein) == 9 and rein != "000000000" else "",
                           text(child(r, "IRCSectionDesc"))[:40], cash, nonc, amt, text(child(r, "PurposeOfGrantTxt"))[:500],
                           text(child(r, "NonCashAssistanceDesc"))[:200]])
    usd = sum(int(g[12] or 0) for g in grants)
    filer = [oid, ein, year, filer_name, city, state, flag(text(child(f990, "DonorAdvisedFundInd"))),
             num(text(desc(sd, "DonorAdvisedFundsHeldCnt"))), num(text(desc(sd, "DonorAdvisedFundsContriAmt"))),
             num(text(desc(sd, "DonorAdvisedFundsGrantsAmt"))), num(text(desc(sd, "DonorAdvisedFundsVlEOYAmt"))),
             num(text(child(f990, "GrantsToDomesticOrgsGrp", "TotalAmt"))), len(grants), usd]
    return filer, grants

def safe_parse(path):
    try:
        return parse(path)
    except (ET.ParseError, IndexError, OSError):
        return "bad"

def psql(sql, *args):
    r = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-q", *args], input=sql, text=True, capture_output=True)
    if r.returncode:
        sys.exit(f"psql failed: {r.stderr.strip()[:2000]}")
    return r.stdout

from org_matcher import link

def match():
    """Link recipients: the reported EIN when it's an org we know, then the shared name matcher."""
    link("charity_grants", "cm", pre="""
UPDATE charity_grants SET recipient_ein = NULL, match_score = NULL, match_method = NULL WHERE recipient_ein IS NOT NULL;
UPDATE charity_grants c SET recipient_ein = c.reported_ein, match_score = 1, match_method = 'ein'
FROM orgs o WHERE o.ein = c.reported_ein;""")

def main():
    if "--match-only" in sys.argv:
        return match()
    files = {}
    for src in SRCS:
        year = os.path.basename(os.path.normpath(src))[:4]
        meta = {r["OBJECT_ID"]: (r["EIN"].zfill(9), r["TAX_PERIOD"]) for r in csv.DictReader(open(os.path.join(XML, f"index_{year}.csv"), encoding="latin-1"))
                if r["RETURN_TYPE"] == "990"}
        for f in os.listdir(src):
            oid = f.split("_")[0]
            if f.endswith(".xml") and oid in meta:
                key = meta[oid]
                if key not in files or oid > files[key][0]:
                    files[key] = (oid, os.path.join(src, f))
    print(f"{len(files)} Form 990 returns in {', '.join(SRCS)}")
    tmp = tempfile.mkdtemp(prefix="charity-", dir=XML)
    fpath, gpath = os.path.join(tmp, "filers.csv"), os.path.join(tmp, "grants.csv")
    n_f = n_g = bad = 0
    t0 = time.time()
    with open(fpath, "w") as ff, open(gpath, "w") as gf, Pool() as pool:
        fw, gw = csv.writer(ff, lineterminator="\n"), csv.writer(gf, lineterminator="\n")
        for i, r in enumerate(pool.imap_unordered(safe_parse, [p for _, p in files.values()], chunksize=128)):
            if r == "bad":
                bad += 1
            elif r:
                fw.writerow(r[0]); gw.writerows(r[1]); n_f += 1; n_g += len(r[1])
            if i % 100000 == 0:
                print(f"  {i} read, {n_f} with Schedule I, {n_g} lines ({time.time() - t0:.0f}s)")
    print(f"parsed in {time.time() - t0:.0f}s: {n_f} returns with Schedule I · {n_g} grant lines · unparseable files: {bad}")
    psql(f"""BEGIN;
TRUNCATE charity_grants, charity_filers RESTART IDENTITY;
\\copy charity_filers (object_id, ein, tax_year, name, city, state, holds_dafs, daf_count, daf_contributions, daf_grants, daf_value, grants_domestic, sched_i_rows, sched_i_usd) FROM '{fpath}' WITH (FORMAT csv, NULL '')
\\copy charity_grants (funder_ein, object_id, tax_year, recipient_name, recipient_city, recipient_state, recipient_zip, recipient_country, reported_ein, irc_section, cash, noncash, amount, purpose, noncash_desc) FROM '{gpath}' WITH (FORMAT csv, NULL '')
COMMIT;""")
    for p in (fpath, gpath): os.remove(p)
    os.rmdir(tmp)
    match()

if __name__ == "__main__":
    main()
