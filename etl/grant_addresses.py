#!/usr/bin/env python3
"""Street addresses for grant recipients still unmatched (load_grants.py doesn't keep them): re-reads the
990-PF returns those grants came from and writes rm_addr (name, city, state, ZIP, street), for the address
rule in etl/rematch_grants.py --address.

  python3 etl/grant_addresses.py
"""
import glob, os, subprocess, sys
from multiprocessing import Pool
sys.path.insert(0, os.path.dirname(__file__))
from load_grants import ET, find, first, name_of, XML  # noqa: E402

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "rematch", "addr.tsv")

def read(path):
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return []
    rows = []
    for g in find(root, "ReturnData/IRS990PF/SupplementaryInformationGrp/GrantOrContributionPdDurYrGrp"):
        street = first(g, "RecipientUSAddress/AddressLine1Txt")
        name = name_of(g)
        if street and name:
            rows.append("\t".join(x.replace("\t", " ") for x in (name, first(g, "RecipientUSAddress/CityNm"),
                        first(g, "RecipientUSAddress/StateAbbreviationCd")[:2], first(g, "RecipientUSAddress/ZIPCd")[:5], street)))
    return rows

def main():
    oids = set(subprocess.run(["psql", DB, "-Atc", "SELECT DISTINCT object_id FROM grants WHERE recipient_ein IS NULL"],
                              capture_output=True, text=True, check=True).stdout.split())
    files = [p for d in glob.glob(os.path.join(XML, "*pf")) for p in glob.glob(os.path.join(d, "*.xml"))
             if os.path.basename(p).split("_")[0] in oids]
    print(f"{len(files)} returns with unmatched grants")
    seen = set()
    with Pool(10) as pool, open(OUT, "w") as f:
        for rows in pool.imap_unordered(read, files, chunksize=200):
            for r in rows:
                if r not in seen:
                    seen.add(r)
                    f.write(r + "\n")
    sql = f"""
DROP TABLE IF EXISTS rm_addr;
CREATE UNLOGGED TABLE rm_addr (raw text, city text, st text, zip text, street text);
\\copy rm_addr FROM '{OUT}' WITH (FORMAT text)
SELECT count(*) FROM rm_addr;"""
    r = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qAt"], input=sql, text=True, capture_output=True)
    print(f"rm_addr rows: {r.stdout.strip() or r.stderr.strip()}")

if __name__ == "__main__":
    main()
