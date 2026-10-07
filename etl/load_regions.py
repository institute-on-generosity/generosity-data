#!/usr/bin/env python3
"""Load zip_regions: ZIP -> county (largest land-area share) and whether it's in Appalachia.

Sources (downloaded to data/geo/dl):
  Census 2020 county codes          national_county2020.txt
  Census 2020 ZCTA-county relations tab20_zcta520_county20_natl.txt
  ARC counties (Appalachian Regional Commission region), from the Wikipedia list's wikitext:
    https://en.wikipedia.org/wiki/List_of_Appalachian_Regional_Commission_counties
PO-box ZIPs (no ZCTA) take the most common county of other orgs in the same city.
Usage: etl/load_regions.py (run after load_bmf.sh)
"""
import csv, io, os, re, subprocess, sys, urllib.request

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
DL = os.path.join(os.path.dirname(__file__), "..", "data", "geo", "dl")
UA = {"User-Agent": "generosity-data (+https://github.com/institute-on-generosity/generosity-data)"}
FILES = {
    "counties.txt": "https://www2.census.gov/geo/docs/reference/codes2020/national_county2020.txt",
    "zcta_county.txt": "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt",
    "arc_wiki.txt": "https://en.wikipedia.org/w/index.php?title=List_of_Appalachian_Regional_Commission_counties&action=raw",
}
STATES = {"Alabama": "AL", "Georgia": "GA", "Kentucky": "KY", "Maryland": "MD", "Mississippi": "MS", "New York": "NY",
          "North Carolina": "NC", "Ohio": "OH", "Pennsylvania": "PA", "South Carolina": "SC", "Tennessee": "TN",
          "Virginia": "VA", "West Virginia": "WV"}

def fetch(name):
    path = os.path.join(DL, name)
    if not os.path.exists(path):
        print(f"download {name}")
        with urllib.request.urlopen(urllib.request.Request(FILES[name], headers=UA)) as r, open(path, "wb") as f:
            f.write(r.read())
    return path

def psql(stdin):
    p = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qAt"], input=stdin, text=True, capture_output=True)
    if p.returncode:
        sys.exit(f"psql failed: {p.stderr.strip()}")
    return p.stdout

def main():
    os.makedirs(DL, exist_ok=True)
    # County FIPS by (state, census name), e.g. ("VA", "Bristol city") -> "51520"
    fips, names = {}, {}
    for r in csv.DictReader(open(fetch("counties.txt"), encoding="utf-8"), delimiter="|"):
        code = r["STATEFP"] + r["COUNTYFP"]
        fips[(r["STATE"], r["COUNTYNAME"].lower())] = code
        names[code] = (r["COUNTYNAME"], r["STATE"])

    # ARC counties: table rows start with "| [[<County>, <State>|...]]" under each "==[[State]]==" heading
    arc, missing, state = set(), [], None
    for line in open(fetch("arc_wiki.txt"), encoding="utf-8"):
        h = re.match(r"==\s*\[\[(?:[^|\]]*\|)?([^\]]+)\]\]\s*==", line)
        if h:
            state = STATES.get(h.group(1).strip())
            continue
        m = re.match(r"\|\s*\[\[([^|\]]+?),\s*([^|\]]+)(?:\|[^\]]*)?\]\]", line)
        if not (state and m):
            continue
        place = m.group(1).strip()  # "Bibb County" or an independent city like "Bristol"
        code = fips.get((state, place.lower())) or fips.get((state, f"{place} city".lower())) or fips.get((state, f"{place} county".lower()))
        (arc.add(code) if code else missing.append(f"{place}, {state}"))
    print(f"ARC counties matched: {len(arc)}; unmatched: {missing}")
    if len(arc) < 400:
        sys.exit("too few ARC counties matched; check the source format")

    # ZIP -> the county holding the largest share of its land area
    best = {}
    with open(fetch("zcta_county.txt"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f, delimiter="|"):
            z, c = r["GEOID_ZCTA5_20"], r["GEOID_COUNTY_20"]
            if not z or not c:
                continue
            land = int(r["AREALAND_PART"] or 0)
            if land >= best.get(z, ("", -1))[1]:
                best[z] = (c, land)
    out = io.StringIO(); w = csv.writer(out, lineterminator="\n")
    for z, (c, _) in best.items():
        name, st = names.get(c, ("", ""))
        w.writerow([z, c, name, st, "t" if c in arc else "f"])
    print(psql("""
TRUNCATE zip_regions;
\\copy zip_regions FROM STDIN WITH (FORMAT csv)
""" + out.getvalue() + "\\.\n" + """
-- PO-box ZIPs have no Census ZCTA: give each one the county most common among
-- other organizations in the same city and state.
INSERT INTO zip_regions (zip5, county_fips, county_name, state, appalachia)
SELECT u.zip5, b.county_fips, b.county_name, b.state, b.appalachia
FROM (SELECT DISTINCT left(o.zip, 5) AS zip5, o.city, o.state FROM orgs o
      WHERE o.zip ~ '^[0-9]{5}' AND NOT EXISTS (SELECT 1 FROM zip_regions z WHERE z.zip5 = left(o.zip, 5))) u
CROSS JOIN LATERAL (
  SELECT z.county_fips, z.county_name, z.state, z.appalachia FROM orgs o2 JOIN zip_regions z ON z.zip5 = left(o2.zip, 5)
  WHERE o2.city = u.city AND o2.state = u.state GROUP BY 1, 2, 3, 4 ORDER BY count(*) DESC LIMIT 1) b
ON CONFLICT (zip5) DO NOTHING;
SELECT count(*) || ' ZIPs, ' || count(*) FILTER (WHERE appalachia) || ' in Appalachia' FROM zip_regions;
""").strip())

if __name__ == "__main__":
    main()
