#!/usr/bin/env python3
"""Write docs/db-health.md: what is in the shared database, how big, how fresh, and checks that flag problems.

  python3 etl/db_health.py              # rewrite docs/db-health.md from the database
  python3 etl/db_health.py --print      # print it instead

Run after any load (or nightly) and commit the file, so everyone can see the database's state on GitHub.
"""
import datetime as dt
import os
import subprocess
import sys

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
OUT = os.path.join(os.path.dirname(__file__), "..", "docs", "db-health.md")
MODEL = "local:nomic-embed-text-v1.5-fp32@512"
LINK_GOAL = 0.70

# Table -> what it holds (shown in the table list; anything else in public is listed without a note)
ABOUT = {
    "orgs": "Every exempt organization (IRS master file)",
    "financials": "Revenue, expenses, assets by year (IRS SOI)",
    "filing_text": "Mission and programs per 990, with vectors",
    "filing_flag": "Diligence fields per 990: finances, board, policies, officers",
    "funders": "Private foundations, newest 990-PF",
    "grants": "Every 990-PF grant, linked to recipients by name",
    "board_members": "Board seats from 990 Part VII and 990-PF officers",
    "zip_regions": "ZIP to county, Appalachia flag",
    "county_need": "County poverty, income, population (Census)",
    "feedback": "Thumbs up or down from NomBot users",
    "filing_latest": "View: newest filing per org (search index)",
    "financials_latest": "View: newest financials per org",
    "flag_latest": "View: newest diligence fields per org",
    "funder_purposes": "View: grant purposes per funder",
    "board_interlocks": "View: org pairs that share board members",
    "news_articles": "GDELT news on philanthropy and nonprofits",
    "congress_bills": "Bills in the current Congress (Congress.gov)",
    "federal_register_docs": "Federal Register rules, notices, funding notices",
    "source_updates": "Freshness log: one row per loader run",
}


def q(sql):
    r = subprocess.run(["psql", DB, "-At", "-F", "\t", "-c", sql], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"psql failed: {r.stderr.strip()[:500]}")
    return [l.split("\t") for l in r.stdout.splitlines() if l]


def one(sql):
    return q(sql)[0]


def n(x):
    return f"{int(float(x)):,}"


def main():
    now = dt.datetime.now().astimezone()
    size = one("SELECT pg_size_pretty(pg_database_size(current_database()))")[0]
    tables = q("""SELECT c.relname, c.relkind, c.reltuples::bigint, pg_size_pretty(pg_total_relation_size(c.oid)), pg_total_relation_size(c.oid)
                  FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace
                  WHERE s.nspname = 'public' AND c.relkind IN ('r', 'm') AND c.relname NOT LIKE 'match\\_%'
                  ORDER BY pg_total_relation_size(c.oid) DESC""")
    orgs, = one("SELECT count(*) FROM orgs")
    filings, orgs_text, embedded, stale = one(f"""SELECT count(*), count(DISTINCT ein), count(embedding),
        count(*) FILTER (WHERE embedding IS NULL OR embedding_model IS DISTINCT FROM '{MODEL}') FROM filing_text""")
    years = q("SELECT tax_year, count(*) FROM filing_text GROUP BY 1 ORDER BY 1 DESC LIMIT 5")
    newest_batch, = one("SELECT max(object_id) FROM filing_text")
    funders, = one("SELECT count(*) FROM funders")
    grants, linked, amount, gy0, gy1 = one("SELECT count(*), count(recipient_ein), round(sum(amount)), min(tax_year), max(tax_year) FROM grants")
    seats, seat_orgs = one("SELECT count(*), count(DISTINCT ein) FROM board_members")
    pairs, hidden, grantee = one("""SELECT count(*), count(*) FILTER (WHERE relation = 'link' AND confidence = 'high'),
        count(*) FILTER (WHERE kind = 'funder-grantee') FROM board_interlocks""")
    circles, = one("SELECT count(*) FROM giving_circles")
    counties, = one("SELECT count(*) FROM county_need WHERE county_fips <> '00000'")
    latest, = one("SELECT count(*) FROM filing_latest")
    migs = q("SELECT name, to_char(applied_at, 'YYYY-MM-DD') FROM schema_migrations ORDER BY name DESC LIMIT 5")
    runs = q("""SELECT DISTINCT ON (source) source, to_char(run_at, 'YYYY-MM-DD HH24:MI'), rows_added, coalesce(to_char(newest, 'YYYY-MM-DD'), ''), coalesce(note, '')
                FROM source_updates ORDER BY source, run_at DESC""")

    link_rate = int(linked) / max(1, int(grants))
    checks = [
        (int(stale) == 0, f"Every filing embedded with the current model ({MODEL})" if int(stale) == 0 else f"{n(stale)} filings need embedding: run `embed/embed_gpu.py`"),
        (int(latest) > 0, f"Search index view populated ({n(latest)} orgs)" if int(latest) else "filing_latest is empty: run `db/refresh.sql`"),
        (int(pairs) > 0, "Board interlocks built" if int(pairs) else "board_interlocks is empty: run `etl/load_board.py`"),
        (link_rate >= LINK_GOAL, f"Grants linked to a recipient: {link_rate:.0%} (goal {LINK_GOAL:.0%})"),
    ]

    out = [
        "# Database health", "",
        f"*Generated {now:%Y-%m-%d %H:%M %Z} by `etl/db_health.py`. Database: {size}.*", "",
        "## Checks", "",
        *[f"- {'✅' if ok else '⚠️'} {text}" for ok, text in checks], "",
        "## What's in it", "",
        "| | Count |", "|---|---|",
        f"| Organizations (IRS master file) | {n(orgs)} |",
        f"| Orgs with an e-filed 990 (searchable) | {n(orgs_text)} |",
        f"| 990 filings (text + diligence fields) | {n(filings)}, {n(embedded)} with vectors |",
        f"| Foundations (990-PF) | {n(funders)} |",
        f"| Grants | {n(grants)}, ${int(float(amount or 0)) / 1e9:.0f}B, tax years {gy0}–{gy1} |",
        f"| Grants linked to a recipient org | {n(linked)} ({link_rate:.0%}) |",
        f"| Board seats | {n(seats)} at {n(seat_orgs)} orgs |",
        f"| Board interlocks (same-state org pairs) | {n(pairs)}: {n(hidden)} high-confidence hidden links, {n(grantee)} foundation-grantee |",
        f"| Giving circles | {n(circles)} |",
        f"| Counties with need data | {n(counties)} |", "",
        "## Freshness", "",
        f"- Newest 990 loaded: object id `{newest_batch}` (IRS batch year {newest_batch[:4]})",
        "- Filings by tax year: " + ", ".join(f"{y}: {n(c)}" for y, c in years),
        "- Latest migrations: " + ", ".join(f"`{m}` ({d})" for m, d in migs), "",
        "Latest run per live source (`source_updates`):", "",
        "| Source | Last run | Rows added | Newest item | Note |", "|---|---|---|---|---|",
        *[f"| {s_} | {r} | {n(a)} | {nw} | {nt} |" for s_, r, a, nw, nt in runs], "",
        "## Tables", "",
        "| Table | Rows (approx.) | Size | Holds |", "|---|---|---|---|",
        *[f"| `{t}`{' (view)' if k == 'm' else ''} | {n(max(0, int(r)))} | {sz} | {ABOUT.get(t, '')} |" for t, k, r, sz, _ in tables], "",
        "Row counts for tables come from Postgres statistics (refreshed by ANALYZE), so they can lag a load slightly; the counts above are exact.", "",
    ]
    text = "\n".join(out)
    if "--print" in sys.argv:
        print(text)
        return
    with open(OUT, "w") as f:
        f.write(text)
    print(f"wrote {os.path.normpath(OUT)}")


if __name__ == "__main__":
    main()
