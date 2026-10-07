#!/usr/bin/env python3
"""Download the IRS SOI annual extract (990 + 990-EZ) and load financials for EINs in `orgs`.

Column names come from the SOI field dictionary (YYeofinextractdoc.xlsx):
  990:    tax_pd, totrevenue (Pt VIII-12A), totfuncexpns (Pt IX-25A), totassetsend (Pt X-16B)
  990-EZ: taxpd,  totrevnue (Pt I-9),       totexpns (Pt I-17),       totassetsend
Usage: etl/load_soi.py [YY]   (default 24)
"""
import csv, io, os, subprocess, sys, urllib.request, zipfile

YY = sys.argv[1] if len(sys.argv) > 1 else "24"
DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
ROOT = os.path.join(os.path.dirname(__file__), "..")
DATA = os.path.join(ROOT, "data", "soi")
UA = {"User-Agent": "generosity-data (+https://github.com/institute-on-generosity/generosity-data)"}

FORMS = [  # (form, zip name, tax period col, revenue, expenses, assets)
    ("990",   f"{YY}eoextract990.zip",   "tax_pd", "totrevenue", "totfuncexpns", "totassetsend"),
    ("990EZ", f"{YY}eoextract990EZ.zip", "taxpd",  "totrevnue",  "totexpns",     "totassetsend"),
]

def psql(*args, stdin=None):
    p = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qAt", *args],
                       input=stdin, text=True, capture_output=True)
    if p.returncode:
        sys.exit(f"psql failed: {p.stderr.strip()}")
    return p.stdout

def num(v):
    v = (v or "").strip()
    return v if v and v.lstrip("-").isdigit() else ""

def main():
    os.makedirs(DATA, exist_ok=True)
    eins = set(psql("-c", "SELECT ein FROM orgs").split())
    out = io.StringIO(); w = csv.writer(out, lineterminator="\n"); kept = 0
    for form, zname, tp, rev, exp, ast in FORMS:
        path = os.path.join(DATA, zname)
        if not os.path.exists(path):
            print(f"download {zname}")
            req = urllib.request.Request(f"https://www.irs.gov/pub/irs-soi/{zname}", headers=UA)
            with urllib.request.urlopen(req) as r, open(path, "wb") as f:
                f.write(r.read())
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
            with z.open(name) as raw:
                rows = csv.DictReader(io.TextIOWrapper(raw, encoding="latin-1"))
                n = 0
                for r in rows:
                    ein = r["EIN"].strip().zfill(9)
                    period = (r.get(tp) or "").strip()
                    if ein not in eins or len(period) < 4:
                        continue
                    w.writerow([ein, period[:4], form, num(r[rev]), num(r[exp]), num(r[ast])])
                    n += 1
                print(f"{form}: {n} rows for loaded orgs")
                kept += n
    sql = """
CREATE TEMP TABLE soi_raw (ein text, tax_year int, form text, revenue bigint, expenses bigint, assets bigint);
\\copy soi_raw FROM STDIN WITH (FORMAT csv, NULL '')
"""
    psql("-c", "SELECT 1")  # connectivity check
    script = sql + out.getvalue() + "\\.\n" + """
INSERT INTO financials (ein, tax_year, form, revenue, expenses, assets)
SELECT DISTINCT ON (ein, tax_year, form) ein, tax_year, form, revenue, expenses, assets FROM soi_raw
ON CONFLICT (ein, tax_year, form) DO UPDATE SET
  revenue = EXCLUDED.revenue, expenses = EXCLUDED.expenses, assets = EXCLUDED.assets;
SELECT count(*) FROM financials;
"""
    print("financials rows:", psql(stdin=script).strip().splitlines()[-1])

if __name__ == "__main__":
    main()
