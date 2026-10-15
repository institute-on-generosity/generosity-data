#!/usr/bin/env python3
"""Load bills from the current Congress into congress_bills (Congress.gov API).

  CONGRESS_API_KEY=... python3 etl/load_congress.py          # bills updated since the last run (first run: all)
  CONGRESS_API_KEY=... python3 etl/load_congress.py --all    # every bill in the current Congress again

Needs a free api.data.gov key: https://api.congress.gov/sign-up/ (put it in CONGRESS_API_KEY, or in
generosity-data/.env as CONGRESS_API_KEY=...). Without one it falls back to DEMO_KEY, which allows only
~30 requests an hour, so it stops after a few pages. 250 bills per request; upserts by (congress, type,
number). Logs the run in source_updates.
"""
import csv, tempfile, datetime as dt, io, json, os, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
API = "https://api.congress.gov/v3/bill"
UA = "generosity-data/1.0 (+https://github.com/institute-on-generosity/generosity-data)"
SLUG = {"HR": "house-bill", "S": "senate-bill", "HRES": "house-resolution", "SRES": "senate-resolution",
        "HJRES": "house-joint-resolution", "SJRES": "senate-joint-resolution",
        "HCONRES": "house-concurrent-resolution", "SCONRES": "senate-concurrent-resolution"}

def psql(sql, stdin=None):
    r = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qAt"], input=stdin if stdin is not None else sql, text=True, capture_output=True)
    if r.returncode:
        sys.exit(f"psql failed: {r.stderr.strip()[:800]}")
    return r.stdout.strip()

def api_key():
    if os.environ.get("CONGRESS_API_KEY"):
        return os.environ["CONGRESS_API_KEY"]
    env = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env):
        for line in open(env):
            if line.startswith("CONGRESS_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None

def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"

def main():
    key = api_key()
    demo = key is None
    if demo:
        print("No CONGRESS_API_KEY: using DEMO_KEY (about 30 requests an hour). Get a free key at https://api.congress.gov/sign-up/", flush=True)
        key = "DEMO_KEY"
    congress = (dt.date.today().year - 1789) // 2 + 1
    # A run cut short by the rate limit leaves "stopped at offset N" in its note: resume the full listing there.
    stopped = psql("SELECT substring(note FROM 'stopped at offset ([0-9]+)') FROM source_updates WHERE source = 'congress' ORDER BY run_at DESC LIMIT 1")
    resume = int(stopped) if stopped and "--all" not in sys.argv else 0
    last = None if "--all" in sys.argv or resume else psql(f"SELECT max(updated_at) FROM congress_bills WHERE congress = {congress}") or None
    params = {"format": "json", "limit": "250", "sort": "updateDate+desc", "api_key": key}
    if last:
        params["fromDateTime"] = dt.datetime.fromisoformat(last).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")  # a file, not stdin: fields may hold line breaks
    w = csv.writer(tmp, lineterminator="\n")
    n, offset, total, cut = 0, resume, None, False
    if resume:
        print(f"Resuming the full listing at bill {resume}", flush=True)
    while total is None or offset < total:
        url = f"{API}/{congress}?{urllib.parse.urlencode({**params, 'offset': offset}, safe='+:')}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:  # the default Python agent gets a 403
                page = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print("Rate limit reached: stopping here (rerun later to resume, or set CONGRESS_API_KEY).", flush=True)
                cut = True
                break
            sys.exit(f"Congress.gov API: {e.code} {e.read()[:300]!r}")
        bills = page.get("bills", [])
        total = page.get("pagination", {}).get("count", 0)
        for b in bills:
            t = (b.get("type") or "").upper()
            la = b.get("latestAction") or {}
            w.writerow([b["congress"], t.lower(), b["number"], b.get("title") or "", b.get("originChamber"), b.get("introducedDate"),
                        la.get("actionDate"), la.get("text"), b.get("updateDateIncludingText") or b.get("updateDate"),
                        f"https://www.congress.gov/bill/{ordinal(b['congress'])}-congress/{SLUG.get(t, t.lower())}/{b['number']}"])
        n += len(bills)
        offset += 250
        print(f"{min(offset, total)}/{total} bills", flush=True)
        if not bills:
            break
        time.sleep(1 if not demo else 2)
    tmp.close()
    before = int(psql("SELECT count(*) FROM congress_bills"))
    psql(None, stdin=f"""CREATE TEMP TABLE c (congress int, bill_type text, number int, title text, origin_chamber text, introduced date,
                                         latest_action_date date, latest_action text, updated_at timestamptz, url text);
\\copy c FROM '{tmp.name}' WITH (FORMAT csv, NULL '')
INSERT INTO congress_bills SELECT DISTINCT ON (congress, bill_type, number) * FROM c ORDER BY congress, bill_type, number, updated_at DESC
ON CONFLICT (congress, bill_type, number) DO UPDATE SET title = EXCLUDED.title, origin_chamber = EXCLUDED.origin_chamber,
  introduced = coalesce(EXCLUDED.introduced, congress_bills.introduced), latest_action_date = EXCLUDED.latest_action_date,
  latest_action = EXCLUDED.latest_action, updated_at = EXCLUDED.updated_at, url = EXCLUDED.url;
""")
    os.unlink(tmp.name)
    after, newest = psql(f"SELECT count(*), max(updated_at) FROM congress_bills").split("|")
    note = f"congress {congress}, fetched {n} of {total}{' with DEMO_KEY' if demo else ''}{f', stopped at offset {offset}' if cut else ''}"
    newest_sql = f"'{newest}'" if newest else "NULL"
    psql(f"INSERT INTO source_updates (source, rows_added, newest, note) VALUES ('congress', {int(after) - before}, {newest_sql}, '{note}')")
    print(f"congress_bills: {after} bills ({int(after) - before} new), newest update {newest}")

if __name__ == "__main__":
    main()
