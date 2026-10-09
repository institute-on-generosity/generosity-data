#!/usr/bin/env python3
"""Load county need into `county_need`: Census SAIPE poverty + income and Census population estimates.

Downloads (no API key needed) into data/census/:
  SAIPE 2023 state and county estimates, fixed width (layout: 2023-estimate-layout.txt)
    cols 1-2 state FIPS, 4-6 county FIPS, 8-15 people in poverty, 35-38 % all ages in poverty (40-43 / 45-48:
    90% confidence bounds), 77-80 % age 0-17 in poverty,
    134-139 median household income, 194-238 name, 240-241 state abbreviation
  Population estimates 2020-2024, county totals CSV (POPESTIMATE2024)
All US counties are loaded (about 3,100) plus the national row as county_fips '00000' (state 'US');
the app joins counties to orgs through zip_regions.county_fips.
"""
import csv, io, os, subprocess, sys, urllib.request

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
DIR = os.path.join(os.path.dirname(__file__), "..", "data", "census")
SAIPE = ("https://www2.census.gov/programs-surveys/saipe/datasets/2023/2023-state-and-county/est23all.txt", "est23all.txt", 2023)
POP = ("https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/counties/totals/co-est2024-alldata.csv", "co-est2024-alldata.csv", 2024)
UA = "generosity-data (+https://github.com/institute-on-generosity/generosity-data)"

def fetch(url, name):
    os.makedirs(DIR, exist_ok=True)
    path = os.path.join(DIR, name)
    if not os.path.exists(path):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
            f.write(r.read())
    return path

def num(s):
    s = s.strip()
    try: return str(int(s)) if s.isdigit() else str(float(s)) if s and s != "." else ""
    except ValueError: return ""

def main():
    pop = {}
    with open(fetch(POP[0], POP[1]), encoding="latin-1") as f:
        for r in csv.DictReader(f):
            if r["SUMLEV"] == "050":
                pop[r["STATE"].zfill(2) + r["COUNTY"].zfill(3)] = r[f"POPESTIMATE{POP[2]}"]
    out = io.StringIO(); w = csv.writer(out, lineterminator="\n")
    n = 0
    with open(fetch(SAIPE[0], SAIPE[1]), encoding="latin-1") as f:
        for line in f:
            if len(line) < 241: continue
            st, co = line[0:2].strip(), line[3:6].strip()
            if not st.isdigit() or not co.isdigit(): continue
            if int(co) == 0 and int(st) != 0: continue  # state rows; keep the US row (00 0) as '00000'
            fips = st.zfill(2) + co.zfill(3)
            name, abbr = (line[193:238].strip(), line[239:241].strip()) if int(st) else ("United States", "US")
            w.writerow([fips, name, abbr, pop.get(fips, ""), num(line[34:38]), num(line[76:80]), num(line[133:139]), SAIPE[2], POP[2] if fips in pop else "",
                        num(line[7:15]), num(line[39:43]), num(line[44:48])])
            n += 1
    print(f"parsed: {n} counties ({sum(1 for k in pop)} with population)")
    script = """CREATE TEMP TABLE cn_raw (LIKE county_need);
\\copy cn_raw (county_fips, name, state, population, poverty_rate, child_poverty_rate, median_income, poverty_year, population_year, people_in_poverty, poverty_low, poverty_high) FROM STDIN WITH (FORMAT csv, NULL '')
""" + out.getvalue() + """\\.
INSERT INTO county_need SELECT * FROM cn_raw
ON CONFLICT (county_fips) DO UPDATE SET name = EXCLUDED.name, state = EXCLUDED.state, population = EXCLUDED.population,
  poverty_rate = EXCLUDED.poverty_rate, child_poverty_rate = EXCLUDED.child_poverty_rate, median_income = EXCLUDED.median_income,
  poverty_year = EXCLUDED.poverty_year, population_year = EXCLUDED.population_year,
  people_in_poverty = EXCLUDED.people_in_poverty, poverty_low = EXCLUDED.poverty_low, poverty_high = EXCLUDED.poverty_high;
SELECT count(*) FROM county_need;
"""
    p = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qAt"], input=script, text=True, capture_output=True)
    if p.returncode: sys.exit(f"psql failed: {p.stderr.strip()[:2000]}")
    print("county_need rows:", p.stdout.strip().splitlines()[-1])

if __name__ == "__main__":
    main()
