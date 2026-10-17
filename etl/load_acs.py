#!/usr/bin/env python3
"""Load Census ACS 5-year estimates (2020-2024) into `county_acs` (migration 043): every county, state and
the nation. The Census API now asks for a key, so this reads the table-based summary files instead:

  https://www2.census.gov/programs-surveys/acs/summary_file/2024/table-based-SF/data/5YRData/acsdt5y2024-<table>.dat
  pipe-delimited, one row per geography (GEO_ID), columns <TABLE>_E<nnn> (estimate) and _M<nnn> (margin)

Tables: B01003 population · B17001 poverty status (001 universe, 002 below poverty) · B19013 median household
income · B01001 sex by age (under 18 = 003-006 + 027-030, 65+ = 020-025 + 044-049) · B03002 Hispanic or Latino
origin by race. Files are cached in data/census/acs2024/ (git-ignored). Tracts are in the files but not
loaded: nothing else in the database is placed by tract.

  python3 etl/load_acs.py
"""
import csv, io, os, subprocess, sys, urllib.request

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
YEAR = 2024
DIR = os.path.join(os.path.dirname(__file__), "..", "data", "census", f"acs{YEAR}")
BASE = f"https://www2.census.gov/programs-surveys/acs/summary_file/{YEAR}/table-based-SF/data/5YRData"
UA = "generosity-data (+https://github.com/institute-on-generosity/generosity-data)"
TABLES = ["b01003", "b17001", "b19013", "b01001", "b03002"]
LEVELS = {"0500000US": "county", "0400000US": "state", "0100000US": "us"}

def fetch(table):
    os.makedirs(DIR, exist_ok=True)
    path = os.path.join(DIR, f"acsdt5y{YEAR}-{table}.dat")
    if not os.path.exists(path):
        req = urllib.request.Request(f"{BASE}/acsdt5y{YEAR}-{table}.dat", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=600) as r, open(path + ".part", "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        os.rename(path + ".part", path)
    return path

def read(table):
    """{fips: {column: value}} for counties, states and the nation."""
    out = {}
    with open(fetch(table), encoding="latin-1") as f:
        head = f.readline().rstrip("\n").split("|")
        for line in f:
            geo = line[:9]
            if geo not in LEVELS:
                continue
            row = line.rstrip("\n").split("|")
            gid = row[0][9:]
            fips = "00000" if geo == "0100000US" else gid.zfill(2) + "000" if geo == "0400000US" else gid
            out[fips] = dict(zip(head[1:], row[1:]))
            out[fips]["_level"] = LEVELS[geo]
    return out

def n(d, col):
    v = (d or {}).get(col, "")
    try:
        x = float(v)
        return None if x < 0 else int(round(x))  # ACS uses negative sentinels for "not available"
    except ValueError:
        return None

def main():
    t = {k: read(k) for k in TABLES}
    # Names and postal codes come from county_need (SAIPE), the county list the app already uses.
    names = dict(line.split("|", 1) for line in subprocess.run(["psql", DB, "-Atc", "SELECT county_fips, name || '|' || state FROM county_need"],
                                                           capture_output=True, text=True, check=True).stdout.splitlines())
    st_abbr = {f[:2]: v.split("|")[1] for f, v in names.items() if f != "00000"}
    w = io.StringIO(); cw = csv.writer(w, lineterminator="\n")
    rows = 0
    for fips, pop in t["b01003"].items():
        lvl = pop["_level"]
        if lvl == "county":
            name, abbr = names[fips].split("|") if fips in names else ("", st_abbr.get(fips[:2], ""))
        elif lvl == "state":
            name, abbr = "", st_abbr.get(fips[:2], "")
        else:
            name, abbr = "United States", "US"
        if lvl != "us" and not abbr:
            continue  # Puerto Rico: county_need (SAIPE) doesn't cover it
        p, a, inc, r = t["b17001"].get(fips), t["b01001"].get(fips), t["b19013"].get(fips), t["b03002"].get(fips)
        uni, pov = n(p, "B17001_E001"), n(p, "B17001_E002")
        u18 = sum(n(a, f"B01001_E{i:03d}") or 0 for i in [*range(3, 7), *range(27, 31)]) if a else None
        o65 = sum(n(a, f"B01001_E{i:03d}") or 0 for i in [*range(20, 26), *range(44, 50)]) if a else None
        rtot, wnh = n(r, "B03002_E001"), n(r, "B03002_E003")
        cw.writerow([fips, lvl, name or abbr, abbr, YEAR, n(pop, "B01003_E001"), uni, pov, n(p, "B17001_M002"),
                     round(100 * pov / uni, 1) if uni and pov is not None else "", n(inc, "B19013_E001"), u18, o65,
                     wnh, n(r, "B03002_E004"), n(r, "B03002_E005"), n(r, "B03002_E006"), n(r, "B03002_E007"), n(r, "B03002_E008"),
                     n(r, "B03002_E009"), n(r, "B03002_E012"), round(100 * (1 - wnh / rtot), 1) if rtot and wnh is not None else ""])
        rows += 1
    script = """BEGIN;
CREATE TEMP TABLE acs_raw (LIKE county_acs);
\\copy acs_raw FROM STDIN WITH (FORMAT csv, NULL '')
""" + w.getvalue() + """\\.
DELETE FROM county_acs;
INSERT INTO county_acs SELECT * FROM acs_raw;
COMMIT;
SELECT level || ': ' || count(*) FROM county_acs GROUP BY level ORDER BY level;
SELECT 'counties in county_need without ACS: ' || count(*) FROM county_need n WHERE county_fips <> '00000' AND NOT EXISTS (SELECT 1 FROM county_acs a WHERE a.county_fips = n.county_fips);
"""
    out = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qAt"], input=script, text=True, capture_output=True)
    if out.returncode:
        sys.exit(f"psql failed: {out.stderr.strip()[:2000]}")
    print(f"parsed {rows} geographies\n" + out.stdout.strip())

if __name__ == "__main__":
    main()
