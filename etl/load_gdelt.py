#!/usr/bin/env python3
"""Load recent English news about philanthropy and the nonprofit sector from GDELT (DOC 2.0 API, free,
no key) into news_articles. https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/

  python3 etl/load_gdelt.py              # since the newest loaded article (first run: last 7 days)
  python3 etl/load_gdelt.py --days=30    # backfill 30 days (one request per topic per day: ~15 min)

One request per topic per day window (GDELT returns at most 250 articles a request and allows one request
every 5 seconds). Upserts by URL and keeps every topic that found an article. Logs the run in source_updates.
"""
import csv, tempfile, datetime as dt, io, json, os, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
API = "https://api.gdeltproject.org/api/v2/doc/doc"
UA = "generosity-data (+https://github.com/institute-on-generosity/generosity-data)"
PAUSE = 6  # seconds between requests (GDELT asks for one every 5)
# Topic -> GDELT query. Kept narrow on purpose: sector news, not every story that says "foundation".
TOPICS = {
    "nonprofit": '(nonprofit OR "non-profit")',
    "philanthropy": "(philanthropy OR philanthropic OR philanthropist)",
    "foundation grants": '("foundation grant" OR "foundation awards" OR "grant from the" foundation)',
    "donations": '(donation OR donors) charity',
    "nonprofit cuts": '(nonprofit OR charity) (layoffs OR closing OR "funding cuts" OR shutdown)',
    "giving": '("charitable giving" OR "giving circle" OR "donor-advised")',
}

def psql(sql, stdin=None):
    r = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qAt"], input=stdin if stdin is not None else sql, text=True, capture_output=True)
    if r.returncode:
        sys.exit(f"psql failed: {r.stderr.strip()[:800]}")
    return r.stdout.strip()

def fetch(query, start, end):
    params = {"query": f"{query} sourcelang:english",  # sourcecountry:US matched nothing; country is stored per article instead "mode": "artlist", "format": "json", "maxrecords": "250",
              "sort": "datedesc", "startdatetime": start.strftime("%Y%m%d%H%M%S"), "enddatetime": end.strftime("%Y%m%d%H%M%S")}
    url = f"{API}?{urllib.parse.urlencode(params)}"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:
                body = r.read().decode("utf-8", "replace")
            if body.lstrip().startswith("{"):
                return json.loads(body).get("articles", [])
            if "limit requests" in body:  # rate limited: back off (GDELT keeps refusing for a while after heavy use)
                time.sleep(60 * (attempt + 1))
                continue
            return []  # GDELT answers plain text for empty or bad queries
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(60 * (attempt + 1))
                continue
            time.sleep(PAUSE * (attempt + 2))
        except Exception:
            time.sleep(PAUSE * (attempt + 2))
    return []

def main():
    days = next((int(a.split("=")[1]) for a in sys.argv if a.startswith("--days=")), None)
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    last = psql("SELECT max(seen_at) FROM news_articles")
    start = now - dt.timedelta(days=days) if days else (dt.datetime.fromisoformat(last) - dt.timedelta(hours=2) if last else now - dt.timedelta(days=7))
    tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")  # a file, not stdin: fields may hold line breaks
    w = csv.writer(tmp, lineterminator="\n")
    n, cur = 0, start
    while cur < now:
        end = min(now, cur + dt.timedelta(days=1))
        got = 0
        for topic, query in TOPICS.items():
            arts = fetch(query, cur, end)
            print(f"  {cur:%Y-%m-%d} {topic}: {len(arts)}", flush=True)
            for a in arts:
                seen = dt.datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
                w.writerow([a["url"], a.get("title") or "", seen.isoformat(), a.get("domain"), a.get("sourcecountry"), a.get("language"), topic])
                got += 1
            time.sleep(PAUSE)
        print(f"{cur:%Y-%m-%d}: {got} articles", flush=True)
        n += got
        cur = end
    tmp.close()
    before = int(psql("SELECT count(*) FROM news_articles"))
    psql(None, stdin=f"""CREATE TEMP TABLE g (url text, title text, seen_at timestamptz, domain text, source_country text, language text, topic text);
\\copy g FROM '{tmp.name}' WITH (FORMAT csv, NULL '')
INSERT INTO news_articles (url, title, seen_at, domain, source_country, language, queries)
SELECT url, max(title), min(seen_at), max(domain), max(source_country), max(language), array_agg(DISTINCT topic ORDER BY topic)
FROM g WHERE title <> '' GROUP BY url
ON CONFLICT (url) DO UPDATE SET queries = (SELECT array_agg(DISTINCT q ORDER BY q) FROM unnest(news_articles.queries || EXCLUDED.queries) q);
""")
    os.unlink(tmp.name)
    after, newest = psql("SELECT count(*), max(seen_at) FROM news_articles").split("|")
    psql(f"INSERT INTO source_updates (source, rows_added, newest, note) VALUES ('gdelt', {int(after) - before}, '{newest}', 'fetched {n} since {start:%Y-%m-%d %H:%M}')")
    print(f"news_articles: {after} articles ({int(after) - before} new), newest {newest}")

if __name__ == "__main__":
    main()
