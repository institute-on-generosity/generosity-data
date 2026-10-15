#!/usr/bin/env python3
"""Load Federal Register documents (rules, proposed rules, notices incl. funding notices, presidential
documents) into federal_register_docs. Free API, no key: https://www.federalregister.gov/developers

  python3 etl/load_federal_register.py              # since the newest loaded date (first run: last 365 days)
  python3 etl/load_federal_register.py --days=30    # re-check the last 30 days

Queries one month at a time (the API returns at most 10,000 documents per query). Upserts by document
number, so reruns are safe. Logs the run in source_updates.
"""
import csv, tempfile, datetime as dt, io, json, os, subprocess, sys, time, urllib.parse, urllib.request

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
API = "https://www.federalregister.gov/api/v1/documents.json"
FIELDS = ["document_number", "title", "type", "abstract", "agencies", "action", "publication_date", "significant", "html_url"]
UA = "generosity-data (+https://github.com/institute-on-generosity/generosity-data)"

def psql(sql, stdin=None):
    r = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qAt"], input=stdin if stdin is not None else sql, text=True, capture_output=True)
    if r.returncode:
        sys.exit(f"psql failed: {r.stderr.strip()[:800]}")
    return r.stdout.strip()

def get(url):
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=120) as r:
                return json.loads(r.read())
        except Exception as e:  # network hiccup or 5xx: back off and retry
            if attempt == 3:
                sys.exit(f"Federal Register API failed: {e}")
            time.sleep(5 * (attempt + 1))

def window(start, end):
    """Every document published from start to end (inclusive)."""
    params = [("per_page", "1000"), ("order", "oldest"),
              ("conditions[publication_date][gte]", start.isoformat()), ("conditions[publication_date][lte]", end.isoformat())]
    params += [("fields[]", f) for f in FIELDS]
    url = f"{API}?{urllib.parse.urlencode(params)}"
    while url:
        page = get(url)
        yield from page.get("results", [])
        url = page.get("next_page_url")

def main():
    days = next((int(a.split("=")[1]) for a in sys.argv if a.startswith("--days=")), None)
    last = psql("SELECT max(publication_date) FROM federal_register_docs")
    today = dt.date.today()
    start = today - dt.timedelta(days=days) if days else (dt.date.fromisoformat(last) if last else today - dt.timedelta(days=365))
    tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")  # a file, not stdin: fields may hold line breaks
    w = csv.writer(tmp, lineterminator="\n")
    n, cur = 0, start
    while cur <= today:
        end = min(today, (cur.replace(day=1) + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1))
        k = 0
        for d in window(cur, end):
            agencies = "{" + ",".join('"' + (a.get("name") or a.get("raw_name") or "").replace('"', "'") + '"' for a in d.get("agencies") or []) + "}"
            w.writerow([d["document_number"], d.get("title") or "", d.get("type"), d.get("abstract"), agencies, d.get("action"),
                        d["publication_date"], "t" if d.get("significant") else "f" if d.get("significant") is False else "", d.get("html_url")])
            k += 1
        print(f"{cur:%Y-%m}: {k} documents", flush=True)
        n += k
        cur = end + dt.timedelta(days=1)
    tmp.close()
    before = int(psql("SELECT count(*) FROM federal_register_docs"))
    psql(None, stdin=f"""CREATE TEMP TABLE fr (document_number text, title text, type text, abstract text, agencies text[], action text,
                                          publication_date date, significant boolean, html_url text);
\\copy fr FROM '{tmp.name}' WITH (FORMAT csv, NULL '')
INSERT INTO federal_register_docs (document_number, title, type, abstract, agencies, action, publication_date, significant, html_url)
SELECT DISTINCT ON (document_number) * FROM fr ORDER BY document_number
ON CONFLICT (document_number) DO UPDATE SET title = EXCLUDED.title, type = EXCLUDED.type, abstract = EXCLUDED.abstract,
  agencies = EXCLUDED.agencies, action = EXCLUDED.action, publication_date = EXCLUDED.publication_date,
  significant = EXCLUDED.significant, html_url = EXCLUDED.html_url;
""")
    os.unlink(tmp.name)
    after, funding, newest = psql("SELECT count(*), count(*) FILTER (WHERE funding_notice), max(publication_date) FROM federal_register_docs").split("|")
    psql(f"INSERT INTO source_updates (source, rows_added, newest, note) VALUES ('federal_register', {int(after) - before}, '{newest}', 'fetched {n} since {start}')")
    print(f"federal_register_docs: {after} documents ({int(after) - before} new), {funding} funding notices, newest {newest}")

if __name__ == "__main__":
    main()
