#!/usr/bin/env python3
"""Load every organization into a local OpenSearch (Elasticsearch-compatible) index, for the keyword
(BM25) side of NomBot's search benchmark (web/eval/bench.mts, method "bm25").

  brew install opensearch && brew services start opensearch     # listens on localhost:9200
  python3 etl/load_search_index.py                               # (re)builds index "orgs" (~2M docs, minutes)

One document per org, the same set and fields the Postgres search filters and ranks on: name, place,
cause (NTEE), revenue (newest of the IRS master file and SOI, as lib/search.ts picks it), Appalachia,
and mission + programs from the newest e-filed 990 (filing_latest). Text uses the English analyzer.
Stdlib only (urllib); OPENSEARCH_URL overrides the address.
"""
import csv, io, json, os, subprocess, sys, time, urllib.request

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
OS = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")
INDEX = "orgs"
BATCH = 5000

def call(method, path, body=None, ndjson=False):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(f"{OS}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/x-ndjson" if ndjson else "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if method == "DELETE" and e.code == 404:
            return {}
        sys.exit(f"OpenSearch {method} {path}: {e.code} {e.read()[:500]!r}")

MAPPING = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "-1"},
    "mappings": {"properties": {
        "ein": {"type": "keyword"},
        "name": {"type": "text", "analyzer": "english"},
        "city": {"type": "keyword"}, "state": {"type": "keyword"}, "ntee": {"type": "keyword"},
        "rev": {"type": "long"}, "rev_year": {"type": "integer"}, "appalachia": {"type": "boolean"},
        "mission": {"type": "text", "analyzer": "english"}, "programs": {"type": "text", "analyzer": "english"},
    }},
}

SQL = """\\copy (
  SELECT o.ein, o.name, upper(o.city), o.state, o.ntee_cd,
         CASE WHEN o.revenue_amt IS NOT NULL AND (f.ein IS NULL OR left(o.tax_period, 4)::int > f.tax_year) THEN o.revenue_amt
              WHEN f.ein IS NOT NULL THEN f.revenue END::bigint,
         CASE WHEN o.revenue_amt IS NOT NULL AND (f.ein IS NULL OR left(o.tax_period, 4)::int > f.tax_year) THEN left(o.tax_period, 4)::int
              WHEN f.ein IS NOT NULL THEN f.tax_year END,
         coalesce(z.appalachia, false), t.mission, t.programs
  FROM orgs o
  LEFT JOIN financials_latest f USING (ein)
  LEFT JOIN filing_latest t USING (ein)
  LEFT JOIN zip_regions z ON z.zip5 = left(o.zip, 5)
) TO STDOUT WITH (FORMAT csv)"""

def main():
    t0 = time.time()
    call("DELETE", f"/{INDEX}")
    call("PUT", f"/{INDEX}", MAPPING)
    p = subprocess.Popen(["psql", DB, "-qAt", "-c", SQL], stdout=subprocess.PIPE, text=True)
    lines, n = [], 0
    for row in csv.reader(p.stdout):
        ein, name, city, state, ntee, rev, rev_year, app, mission, programs = row
        doc = {"ein": ein, "name": name, "city": city or None, "state": state or None, "ntee": ntee or None,
               "rev": int(rev) if rev else None, "rev_year": int(rev_year) if rev_year else None, "appalachia": app == "t",
               "mission": mission or None, "programs": programs or None}
        lines.append(json.dumps({"index": {"_index": INDEX, "_id": ein}}))
        lines.append(json.dumps(doc))
        if len(lines) >= 2 * BATCH:
            n += flush(lines)
    n += flush(lines)
    if p.wait():
        sys.exit("psql export failed")
    call("PUT", f"/{INDEX}/_settings", {"index": {"refresh_interval": "1s"}})
    call("POST", f"/{INDEX}/_refresh")
    size = call("GET", f"/{INDEX}/_stats/store")["_all"]["primaries"]["store"]["size_in_bytes"]
    print(f"indexed {n:,} orgs in {time.time() - t0:.0f}s, index size {size / 1e9:.2f} GB")

def flush(lines):
    if not lines:
        return 0
    r = call("POST", "/_bulk", ("\n".join(lines) + "\n").encode(), ndjson=True)
    if r.get("errors"):
        bad = next(i for i in r["items"] if i["index"].get("error"))
        sys.exit(f"bulk error: {bad['index']['error']}")
    k = len(lines) // 2
    lines.clear()
    return k

if __name__ == "__main__":
    main()
