#!/usr/bin/env python3
"""Download the IRS SOI annual extract (990 + 990-EZ) and load financials for EINs in `orgs`.

Column names come from the SOI field dictionary (YYeofinextractdoc.xlsx):
  990:    tax_pd, totrevenue (Pt VIII-12A), totfuncexpns (Pt IX-25A), totassetsend (Pt X-16B)
  990-EZ: taxpd,  totrevnue (Pt I-9),       totexpns (Pt I-17),       totassetsend
Also keeps each row's non-empty, non-zero fields (financials.raw) and loads the field
dictionary (YYeofinextractdoc.xlsx) into soi_fields, for NomBot's readable SOI viewer.
Usage: etl/load_soi.py [YY]   (default 24)
"""
import csv, io, json, os, re, subprocess, sys, urllib.request, zipfile
import xml.etree.ElementTree as ET

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

def download(name):
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        print(f"download {name}")
        req = urllib.request.Request(f"https://www.irs.gov/pub/irs-soi/{name}", headers=UA)
        with urllib.request.urlopen(req) as r, open(path, "wb") as f:
            f.write(r.read())
    return path

def xlsx_rows(path):
    """{sheet name: [{column letter: text}]} from an .xlsx, stdlib only."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    r_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    z = zipfile.ZipFile(path)
    strings = ["".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t"))
               for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns)]
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    out = {}
    for sheet in ET.fromstring(z.read("xl/workbook.xml")).find("m:sheets", ns):
        rows = []
        for row in ET.fromstring(z.read("xl/" + rels[sheet.get(r_ns)].lstrip("/").removeprefix("xl/"))).find("m:sheetData", ns):
            cells = {}
            for c in row:
                v = c.find("m:v", ns)
                if v is not None:
                    cells[re.sub(r"\d", "", c.get("r"))] = (strings[int(v.text)] if c.get("t") == "s" else v.text).strip()
            rows.append(cells)
        out[sheet.get("name")] = rows
    return out

def field_dictionary():
    """CSV rows for soi_fields: form, col, position, description, location, codes (json)."""
    sheets = xlsx_rows(download(f"{YY}eofinextractdoc.xlsx"))
    codes, field = {}, None  # Codes sheet: a field-name row, then value | meaning rows
    for r in sheets.get("Codes", [])[3:]:
        a, b = r.get("A", ""), r.get("B", "")
        if a and (not b or b.startswith("http")) and not a.isdigit() and len(a) > 2:
            field = re.sub(r"\s*\(.*\)$", "", a).lower()
            codes[field] = {"_link": b} if b else {}
        elif field and a:
            codes[field][a] = b
        elif field and b and codes[field]:  # continuation of the previous meaning
            last = list(codes[field])[-1]
            codes[field][last] += " / " + b
    out = []
    for sheet, form in (("990", "990"), ("990-EZ", "990EZ"), ("990-PF", "990PF")):
        for i, r in enumerate(sheets.get(sheet, [])[3:]):
            col = r.get("A", "").lower()
            if col:
                out.append([form, col, i, r.get("B", ""), r.get("C", ""), json.dumps(codes[col]) if col in codes else ""])
    return out

def num(v):
    v = (v or "").strip()
    return v if v and v.lstrip("-").isdigit() else ""

def main():
    os.makedirs(DATA, exist_ok=True)
    eins = set(psql("-c", "SELECT ein FROM orgs").split())
    out = io.StringIO(); w = csv.writer(out, lineterminator="\n"); kept = 0
    for form, zname, tp, rev, exp, ast in FORMS:
        path = download(zname)
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
                    raw = {k.lower(): v.strip() for k, v in r.items() if k and v and v.strip() not in ("", "0")}
                    w.writerow([ein, period[:4], form, num(r[rev]), num(r[exp]), num(r[ast]), json.dumps(raw, separators=(",", ":"))])
                    n += 1
                print(f"{form}: {n} rows for loaded orgs")
                kept += n
    sql = """
CREATE TEMP TABLE soi_raw (ein text, tax_year int, form text, revenue bigint, expenses bigint, assets bigint, raw jsonb);
\\copy soi_raw FROM STDIN WITH (FORMAT csv, NULL '')
"""
    psql("-c", "SELECT 1")  # connectivity check
    script = sql + out.getvalue() + "\\.\n" + """
INSERT INTO financials (ein, tax_year, form, revenue, expenses, assets, raw)
SELECT DISTINCT ON (ein, tax_year, form) ein, tax_year, form, revenue, expenses, assets, raw FROM soi_raw
ON CONFLICT (ein, tax_year, form) DO UPDATE SET
  revenue = EXCLUDED.revenue, expenses = EXCLUDED.expenses, assets = EXCLUDED.assets, raw = EXCLUDED.raw;
SELECT count(*) FROM financials;
"""
    print("financials rows:", psql(stdin=script).strip().splitlines()[-1])

    fields = io.StringIO(); csv.writer(fields, lineterminator="\n").writerows(field_dictionary())
    print("soi_fields rows:", psql(stdin="""
CREATE TEMP TABLE f (form text, col text, position int, description text, location text, codes jsonb);
\\copy f FROM STDIN WITH (FORMAT csv, NULL '')
""" + fields.getvalue() + "\\.\n" + """
INSERT INTO soi_fields SELECT DISTINCT ON (form, col) * FROM f
ON CONFLICT (form, col) DO UPDATE SET position = EXCLUDED.position, description = EXCLUDED.description,
  location = EXCLUDED.location, codes = EXCLUDED.codes;
SELECT count(*) FROM soi_fields;
""").strip().splitlines()[-1])

if __name__ == "__main__":
    main()
