#!/usr/bin/env python3
"""Weekly analyst snapshot: a curated set of tables as Parquet (zstd), with a README and manifest.

  .venv-analytics/bin/python etl/export_snapshot.py                # ~/Exports/nombot-YYYY-MM-DD/
  .venv-analytics/bin/python etl/export_snapshot.py --zip --keep 3 # plus nombot-YYYY-MM-DD.zip; keep newest 3
  .venv-analytics/bin/python etl/export_snapshot.py --drive        # also copy into Google Drive for Desktop
  .venv-analytics/bin/python etl/export_snapshot.py --only grants,orgs --out /tmp/snap

Reads Postgres through DuckDB's postgres extension (read-only attach), streams each table straight to
Parquet (5M grants never sit in Python memory), then checks every file's row count against Postgres.
Writes into a .partial folder and renames it at the end, so a failed run never looks finished.
Skips raw, internal and scratch tables, embeddings, grant_unlinked, api tables and feedback.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile

import duckdb

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PG = os.environ.get("EXPORT_PG", "dbname=nombot")  # libpq string for DuckDB's ATTACH
PREFIX = "nombot-"
DATED = re.compile(r"^nombot-(\d{4}-\d{2}-\d{2})(\.zip)?$")

# name -> (source relation, select list, where clause, one-line description, {column: meaning})
TABLES = {
    "orgs": (
        "orgs",
        "ein, name, sort_name, care_of, street, city, state, zip, subsection, foundation, ruling, status, "
        "ntee_cd, asset_amt, income_amt, revenue_amt, tax_period, loaded_at",
        None,
        "Every exempt organization in the IRS Business Master File (terminated orgs skipped). One row per EIN.",
        {
            "ein": "9-digit EIN, text with leading zeros; joins every other file",
            "name": "Legal name (IRS master file)",
            "sort_name": "IRS secondary name (often a chapter's host or a DBA)",
            "care_of": "In-care-of name on the IRS address",
            "street / city / state / zip": "Mailing address; zip may be ZIP+4, use left(zip,5)",
            "subsection": "501(c) subsection code ('03' = 501(c)(3))",
            "foundation": "IRS foundation code (e.g. '04' private non-operating foundation, '15'/'16' public charity)",
            "ruling": "IRS ruling date YYYYMM: when exemption was granted, NOT the founding date",
            "status": "IRS exempt status code ('01' unconditional)",
            "ntee_cd": "NTEE cause code (first letter = major group, e.g. B education, E health)",
            "asset_amt / income_amt / revenue_amt": "Latest amounts the IRS has on file (dollars)",
            "tax_period": "Tax period of those amounts, YYYYMM",
            "loaded_at": "When the row was last loaded",
        },
    ),
    "filing_latest": (
        "filing_latest",
        "object_id, ein, tax_year, form, mission, programs, website, employees, volunteers",
        None,
        "Newest e-filed 990 / 990-EZ / 990-PF per org: mission, program text, website, staff counts. No vectors.",
        {
            "object_id": "IRS e-file object id of the return (unique per filing)",
            "ein": "Filer EIN",
            "tax_year": "Tax year of the return",
            "form": "990, 990EZ or 990PF",
            "mission": "Mission statement (Part I / Part III)",
            "programs": "Program service descriptions (Part III), concatenated",
            "website": "Website from the return header (placeholders dropped)",
            "employees / volunteers": "Part I lines 5-6 (not on 990-EZ)",
        },
    ),
    "financials_latest": (
        "financials_latest",
        "*",
        None,
        "Newest IRS SOI financial record per org (990 and 990-EZ filers).",
        {
            "ein": "Org EIN",
            "tax_year": "Tax year",
            "form": "990 or 990EZ",
            "revenue / expenses / assets": "Total revenue, total expenses, total assets (end of year), dollars",
            "net_assets": "Net assets or fund balances, dollars",
        },
    ),
    "flag_latest": (
        "flag_latest",
        "*",
        None,
        "Newest FLAG diligence numbers per org from 990 XML (same-year ties: newest filing, full 990 over EZ).",
        {
            "ein": "Org EIN",
            "tax_year": "Tax year",
            "cy_revenue / py_revenue": "Total revenue this year / prior year (Part I)",
            "cy_expenses": "Total expenses this year",
            "cash": "Cash, end of year (Part X)",
            "cy_contributions": "Contributions and grants received this year",
        },
    ),
    "funders": (
        "funders",
        "*",
        None,
        "One row per US private foundation (newest 990-PF): assets, giving, how to apply.",
        {
            "ein": "Foundation EIN",
            "object_id / tax_year": "Return it came from",
            "name / city / state": "Foundation name and location",
            "assets": "Fair market value of assets, dollars",
            "grants_paid": "Total grants paid that year, dollars",
            "grant_count": "Grants listed in Part XV 3a",
            "invite_only": "Only gives to preselected charities (Part XV 2)",
            "website": "Website from the return",
            "apply_contact / apply_form / apply_deadlines / apply_restrictions": "How to apply (Part XV 2a-d)",
        },
    ),
    "grants": (
        "grants",
        "id, funder_ein, tax_year, object_id, recipient_name, recipient_city, recipient_state, recipient_zip, "
        "amount, purpose, recipient_status, relationship, recipient_ein, recipient_entity, recipient_kind, "
        "match_method, match_score",
        None,
        "Every grant paid on a 990-PF (Part XV 3a), all loaded IRS years, with recipient links.",
        {
            "id": "Grant row id",
            "funder_ein": "Paying foundation; joins funders.ein / orgs.ein",
            "tax_year": "Tax year of the return",
            "object_id": "Return it came from (one per foundation per year, newest filed)",
            "recipient_name / _city / _state / _zip": "Recipient as written by the foundation",
            "amount": "Grant paid, dollars",
            "purpose": "Purpose as written",
            "recipient_status": "Recipient's foundation status as written (e.g. PC, 501(c)(3))",
            "relationship": "Relationship to a foundation manager, as written",
            "recipient_ein": "Linked recipient org (orgs.ein); NULL if not linked",
            "recipient_entity": "Linked school district or college (public_entities.id) when not an IRS org",
            "recipient_kind": "foreign / individual / placeholder: can never link; NULL = ordinary recipient",
            "match_method": "How the link was made (exact, exact-city, fuzzy, fuzzy-checked, claude-fix, override, "
                            "dba, alias, zip, claude-pick, claude-name, national, ipeds-ein, public)",
            "match_score": "Name similarity of the link (1 = exact)",
        },
    ),
    "board_interlocks": (
        "board_interlocks",
        "*",
        "confidence in ('high', 'medium')",
        "Same-state org pairs that share board members (high and medium confidence only).",
        {
            "ein_a / ein_b": "The two orgs",
            "state": "State both orgs are in",
            "shared": "Number of shared people",
            "overlap": "Shared people / smaller board",
            "people": "Shared people's names (public 990 data; list)",
            "kind": "funder-grantee, funder-funder, funder-nonprofit, nonprofit-nonprofit",
            "relation": "link (hidden link), sister (obvious affiliate), bank-trust (same bank trustee)",
            "confidence": "high (~100% same person when graded) or medium (~95%)",
            "grants / amount / years": "Grants paid between them, dollars, tax years",
        },
    ),
    "public_entities": (
        "public_entities",
        "*",
        None,
        "School districts (NCES CCD) and colleges (IPEDS): grant recipients outside the IRS list.",
        {
            "id": "Entity id; joins grants.recipient_entity",
            "kind": "school_district, college_public, college_private",
            "name / alias": "Name and alternate name",
            "city / state / zip": "Location",
            "ein": "IPEDS EIN (colleges only, may be NULL)",
            "website": "Website",
        },
    ),
    "county_need": (
        "county_need",
        "*",
        None,
        "Census need per county (SAIPE poverty + income, population estimates); '00000' = U.S.",
        {
            "county_fips": "5-digit county FIPS; joins zip_regions.county_fips",
            "name / state": "County name and state",
            "population / population_year": "Population estimate and its year",
            "poverty_rate / child_poverty_rate": "Percent in poverty (all ages, under 18)",
            "poverty_low / poverty_high": "90% range of poverty_rate",
            "people_in_poverty": "People in poverty",
            "median_income": "Median household income, dollars",
            "poverty_year": "SAIPE year",
        },
    ),
    "zip_regions": (
        "zip_regions",
        "*",
        None,
        "ZIP to county (largest land share) and Appalachia flag.",
        {
            "zip5": "5-digit ZIP (char 5); join on left(zip,5)",
            "county_fips / county_name / state": "County it maps to",
            "appalachia": "County is in the Appalachian Regional Commission area",
        },
    ),
    "insights": (
        "insights",
        "id, slug, section, headline, summary, points::varchar AS points, stats::varchar AS stats, "
        "data::varchar AS data, chart::varchar AS chart, method, caveats, sources::varchar AS sources, query, "
        "explore_query, org_eins, cause, state, score, verified, created_at, refreshed_at",
        None,
        "Verified patterns behind NomBot's Insights tab. JSON columns are JSON strings.",
        {
            "slug / section": "Insight id and section (funding-gaps, money-flows, ...)",
            "headline / summary": "Short claim and one-sentence fallback",
            "points": "JSON: 2-3 bullet strings",
            "stats": "JSON: 2-4 {label, value} key numbers",
            "data": "JSON: rows behind the finding",
            "chart": "JSON: Vega-Lite v5 spec (data inline)",
            "method / caveats / sources": "How it was computed, limits, sources (sources is JSON)",
            "query / explore_query": "SQL that reproduces it (Postgres dialect)",
            "org_eins": "EINs named in the finding",
            "cause / state / score / verified": "Filters, ranking score, recount agreed",
            "created_at / refreshed_at": "Timestamps",
        },
    ),
    "giving_circles": (
        "giving_circles",
        "*",
        None,
        "Giving circles that are their own nonprofit, found by name (hosted circles don't file, so missing).",
        {
            "ein / name / city / state": "The circle",
            "ntee_cd": "NTEE code",
            "kind": "How it was found (Impact 100, 100 Who Care, women's fund, giving circle)",
            "revenue": "Revenue (IRS master file), dollars",
            "grants_paid": "Grants paid (990 Part IX line 1), dollars",
            "website": "Website",
        },
    ),
}


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_commit():
    try:
        sha = subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", REPO, "status", "--porcelain"], capture_output=True, text=True).stdout.strip()
        return sha + ("-dirty" if dirty else "")
    except Exception:
        return None


def pg_scalar(con, sql):
    return con.execute("SELECT * FROM postgres_query('pg', ?)", [sql]).fetchone()[0]


def export(con, folder, names):
    results = []
    for name in names:
        rel, cols, where, _, _ = TABLES[name]
        path = os.path.join(folder, f"{name}.parquet")
        sql = f"SELECT {cols} FROM pg.public.{rel}" + (f" WHERE {where}" if where else "")
        t = time.time()
        con.execute(f"COPY ({sql}) TO '{path}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 122880)")
        rows = con.execute(f"SELECT count(*) FROM read_parquet('{path}')").fetchone()[0]
        pg_rows = pg_scalar(con, f"SELECT count(*) FROM public.{rel}" + (f" WHERE {where}" if where else ""))
        size = os.path.getsize(path)
        ok = rows == pg_rows
        print(f"  {name:18} {rows:>10,} rows  {human(size):>9}  {time.time() - t:6.1f}s  {'ok' if ok else f'MISMATCH (postgres {pg_rows:,})'}", flush=True)
        results.append({"file": f"{name}.parquet", "table": rel, "filter": where, "rows": rows,
                        "postgres_rows": pg_rows, "rows_match": ok, "bytes": size, "sha256": sha256(path)})
    return results


def columns(con, path):
    return con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()


def stats(con, folder):
    """A few live numbers for the caveats, computed from the exported files (never hard-coded)."""
    g = os.path.join(folder, "grants.parquet")
    if not os.path.exists(g):
        return None
    r = con.execute(f"""
      SELECT count(*), count(*) FILTER (WHERE recipient_ein IS NOT NULL OR recipient_entity IS NOT NULL),
             count(*) FILTER (WHERE recipient_kind IS NULL),
             count(*) FILTER (WHERE recipient_kind IS NULL AND (recipient_ein IS NOT NULL OR recipient_entity IS NOT NULL)),
             sum(amount), sum(amount) FILTER (WHERE recipient_ein IS NOT NULL OR recipient_entity IS NOT NULL),
             min(tax_year), max(tax_year),
             count(*) FILTER (WHERE recipient_kind = 'foreign'), count(*) FILTER (WHERE recipient_kind = 'individual'),
             count(*) FILTER (WHERE recipient_kind = 'placeholder')
      FROM read_parquet('{g}')""").fetchone()
    years = con.execute(f"SELECT tax_year, count(*) FROM read_parquet('{g}') GROUP BY 1 ORDER BY 1").fetchall()
    return {"grants": r[0], "linked": r[1], "linkable": r[2], "linkable_linked": r[3], "dollars": r[4],
            "dollars_linked": r[5], "year_min": r[6], "year_max": r[7], "foreign": r[8], "individual": r[9],
            "placeholder": r[10], "years": years}


def write_readme(con, folder, results, meta, s):
    L = []
    L.append(f"# NomBot data snapshot, {meta['date']}\n")
    L.append("Curated copy of the Institute on Generosity's shared nonprofit database (IRS 990s, 990-PF grants, "
             "IRS master file, Census) for analysis. Parquet files, zstd-compressed. Open them with DuckDB, pandas or "
             "polars; no database access needed.\n")
    L.append(f"- **Exported:** {meta['exported_at']} from Postgres `nombot` ({meta['source_db_size']})")
    L.append(f"- **Code:** generosity-data commit `{meta['git_commit']}`")
    L.append(f"- **Total:** {len(results)} files, {human(sum(r['bytes'] for r in results))}. Checksums in `manifest.json`.")
    L.append("- **Internal use.** Public IRS and Census data, but `board_interlocks.people` lists board members by name: "
             "don't republish it.\n")

    L.append("## Files\n")
    L.append("| File | Rows | Size | What it is |")
    L.append("|---|---:|---:|---|")
    for r in results:
        name = r["file"][:-8]
        L.append(f"| `{r['file']}` | {r['rows']:,} | {human(r['bytes'])} | {TABLES[name][3]} |")
    L.append("")

    L.append("## Joins\n")
    L.append("- `ein` (9-char text, keep leading zeros) joins `orgs`, `filing_latest`, `financials_latest`, `flag_latest`, `funders`, `giving_circles`.")
    L.append("- `grants.funder_ein` -> `funders.ein`; `grants.recipient_ein` -> `orgs.ein`; `grants.recipient_entity` -> `public_entities.id`.")
    L.append("- `left(grants.recipient_zip, 5)` or `left(orgs.zip, 5)` -> `zip_regions.zip5` -> `county_need.county_fips`.")
    L.append("- `board_interlocks.ein_a / ein_b` -> `orgs.ein`.\n")

    L.append("## Caveats\n")
    if s:
        pct = lambda a, b: f"{100 * a / b:.1f}%" if b else "n/a"
        L.append(f"- **Grant links are name matches.** The IRS 990-PF has no recipient EIN. {pct(s['linked'], s['grants'])} of grants "
                 f"are linked to an org or public entity ({pct(s['linkable_linked'], s['linkable'])} of linkable ones; "
                 f"{pct(s['dollars_linked'] or 0, s['dollars'])} of dollars). Methods graded 93-100% right, except unchecked `fuzzy` (~91%) and other-city `exact` "
                 "links whose name 2-3 orgs share (~60%). Unlinked does not mean the recipient isn't a nonprofit.")
        yrs = ", ".join(f"{y}: {n:,}" for y, n in s["years"])
        L.append(f"- **Coverage years:** IRS processing years 2023-2026; grant tax years {s['year_min']}-{s['year_max']}, mostly 2022-2025 "
                 f"({yrs}). The newest year is incomplete (late filers), so don't read a drop in the last year as a trend.")
        L.append(f"- **`recipient_kind`:** foreign {s['foreign']:,}, individual {s['individual']:,}, placeholder {s['placeholder']:,} "
                 "(\"see attached\", HIPAA notes). These never link; few by count but large by dollars. Exclude them from dollar totals "
                 "to US organizations (`WHERE recipient_kind IS NULL`).")
    L.append("- **Board interlocks:** people are matched by first + last name within a state. Only `high` (~100% same person when "
             "graded) and `medium` (~95%) pairs are here; `low` is left out. `relation = 'sister'` / `'bank-trust'` are obvious "
             "affiliates or shared bank trustees, not hidden links. Nonprofit boards come from the 25 best-paid people per filing, "
             "so very large boards are cut short.")
    L.append("- **`orgs.ruling` is the IRS ruling date** (when exemption was granted), not the founding date. Orgs reinstated after revocation can carry a recent one.")
    L.append("- **990 text only for e-filers:** `filing_latest` covers orgs that e-file a 990; small orgs (990-N) have no text or financials.")
    L.append("- **One row per org in `*_latest`:** newest filing only; different orgs can have different tax years.")
    L.append("- **Money columns are nominal dollars** as reported; some returns have typos (an extra zero). Check outliers before ranking.")
    L.append("- **`insights.query`** is Postgres SQL against the source database; it won't run unchanged on these files.\n")

    L.append("## Columns\n")
    for r in results:
        name = r["file"][:-8]
        notes = TABLES[name][4]
        expanded = {}
        for k, v in notes.items():
            for part in k.split(" / "):
                part = part.strip()
                if part.startswith("_") and expanded:
                    part = list(expanded)[-1].rsplit("_", 1)[0] + part
                expanded[part] = v
        L.append(f"### `{r['file']}`\n")
        L.append("| Column | Type | Meaning |")
        L.append("|---|---|---|")
        for col, typ, *_ in columns(con, os.path.join(folder, r["file"])):
            L.append(f"| `{col}` | {typ} | {expanded.get(col, '')} |")
        L.append("")

    L.append("## Example queries\n")
    L.append("Install: `brew install duckdb` (CLI) or `pip install duckdb pandas pyarrow polars`. Run from inside this folder.\n")
    L.append("```bash")
    L.append("# Biggest funders of linked recipients in Kentucky, 2022-2024")
    L.append("duckdb -c \"select f.name, sum(g.amount) as dollars, count(*) as grants")
    L.append("  from 'grants.parquet' g join 'funders.parquet' f on f.ein = g.funder_ein")
    L.append("  join 'orgs.parquet' o on o.ein = g.recipient_ein")
    L.append("  where o.state = 'KY' and g.tax_year between 2022 and 2024")
    L.append("  group by 1 order by 2 desc limit 20\"")
    L.append("")
    L.append("# Grant dollars per person by county (grants to US orgs only)")
    L.append("duckdb -c \"select c.county_fips, c.name, c.state, sum(g.amount) / any_value(c.population) as per_person")
    L.append("  from 'grants.parquet' g join 'zip_regions.parquet' z on z.zip5 = left(g.recipient_zip, 5)")
    L.append("  join 'county_need.parquet' c on c.county_fips = z.county_fips")
    L.append("  where g.recipient_kind is null and g.tax_year = 2023 group by all order by per_person desc limit 20\"")
    L.append("")
    L.append("# Foundations that pay an org they share board members with")
    L.append("duckdb -c \"select a.name as funder, b.name as grantee, i.shared, i.amount")
    L.append("  from 'board_interlocks.parquet' i join 'orgs.parquet' a on a.ein = i.ein_a join 'orgs.parquet' b on b.ein = i.ein_b")
    L.append("  where i.kind = 'funder-grantee' and i.relation = 'link' order by i.amount desc nulls last limit 20\"")
    L.append("")
    L.append("# Peek at any file's columns")
    L.append("duckdb -c \"describe select * from 'filing_latest.parquet'\"")
    L.append("```\n")
    L.append("```python")
    L.append("import duckdb, pandas as pd, polars as pl")
    L.append("")
    L.append("# pandas: small tables load whole")
    L.append("need = pd.read_parquet('county_need.parquet')")
    L.append("")
    L.append("# pandas: big tables, read only the columns and rows you need")
    L.append("ky = pd.read_parquet('grants.parquet', columns=['funder_ein', 'recipient_ein', 'amount', 'tax_year'],")
    L.append("                     filters=[('recipient_state', '==', 'KY')])")
    L.append("")
    L.append("# polars: lazy scan, only what the query needs is read")
    L.append("top = (pl.scan_parquet('grants.parquet')")
    L.append("       .filter(pl.col('recipient_kind').is_null())")
    L.append("       .group_by('funder_ein').agg(pl.col('amount').sum().alias('dollars'))")
    L.append("       .sort('dollars', descending=True).head(20).collect())")
    L.append("")
    L.append("# DuckDB from Python, result as a DataFrame")
    L.append("df = duckdb.sql(\"select ntee_cd, count(*) n from 'orgs.parquet' group by 1 order by 2 desc\").df()")
    L.append("")
    L.append("# JSON columns in insights are strings")
    L.append("import json; ins = pd.read_parquet('insights.parquet'); json.loads(ins.stats[0])")
    L.append("```\n")
    L.append("Full source docs: generosity-data `README.md` (tables, gotchas, matching method).\n")
    with open(os.path.join(folder, "README.md"), "w") as f:
        f.write("\n".join(L))


def make_zip(folder):
    """Parquet is already compressed, so store files as-is (fast, same size)."""
    path = folder.rstrip("/") + ".zip"
    tmp = path + ".partial"
    base = os.path.basename(folder.rstrip("/"))
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for fn in sorted(os.listdir(folder)):
            z.write(os.path.join(folder, fn), f"{base}/{fn}")
    os.replace(tmp, path)
    return path


def prune(root, keep):
    """Keep the newest `keep` dated snapshots (folders and zips) in root; delete older ones and stale partials."""
    entries = {}
    for fn in os.listdir(root):
        if fn.endswith(".partial"):
            p = os.path.join(root, fn)
            shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
            continue
        m = DATED.match(fn)
        if m:
            entries.setdefault(m.group(1), []).append(fn)
    removed = []
    for date in sorted(entries, reverse=True)[keep:]:
        for fn in entries[date]:
            p = os.path.join(root, fn)
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
            removed.append(fn)
    return removed


def drive_root():
    base = os.path.expanduser("~/Library/CloudStorage")
    if not os.path.isdir(base):
        return None
    for fn in sorted(os.listdir(base)):
        if fn.startswith("GoogleDrive-"):
            p = os.path.join(base, fn, "My Drive")
            if os.path.isdir(p):
                return p
    return None


def copy_to_drive(folder, subdir, keep):
    root = drive_root()
    if not root:
        print("\nGoogle Drive for Desktop not found (~/Library/CloudStorage/GoogleDrive-*/My Drive/).")
        print("  1. Install it: https://www.google.com/drive/download/ and sign in with your IoG account.")
        print("  2. Re-run with --drive. The snapshot is copied to 'My Drive/<--drive-subdir>/' and syncs from there.")
        print("  3. Share that Drive folder with teammates (Viewer).")
        return None
    dest_root = os.path.join(root, subdir)
    os.makedirs(dest_root, exist_ok=True)
    name = os.path.basename(folder.rstrip("/"))
    dest = os.path.join(dest_root, name)
    tmp = dest + ".partial"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(folder, tmp)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.replace(tmp, dest)
    removed = prune(dest_root, keep)
    print(f"\nCopied to Google Drive: {dest}" + (f" (removed {', '.join(removed)})" if removed else ""))
    return dest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="~/Exports", help="root folder; the snapshot goes in <out>/nombot-YYYY-MM-DD/ (default ~/Exports)")
    ap.add_argument("--date", default=dt.date.today().isoformat(), help="snapshot date in the folder name (default today)")
    ap.add_argument("--only", help="comma-separated subset of tables (for testing)")
    ap.add_argument("--zip", action="store_true", help="also write <out>/nombot-YYYY-MM-DD.zip")
    ap.add_argument("--keep", type=int, default=3, help="keep the newest N snapshots in --out (and on Drive); default 3, 0 = keep all")
    ap.add_argument("--drive", action="store_true", help="copy the finished folder into Google Drive for Desktop")
    ap.add_argument("--drive-subdir", default="IoG Data Snapshots", help="folder under 'My Drive' (default 'IoG Data Snapshots')")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--memory", default="4GB", help="DuckDB memory limit (spills to disk beyond it)")
    args = ap.parse_args()

    names = list(TABLES)
    if args.only:
        names = [n.strip() for n in args.only.split(",") if n.strip()]
        bad = [n for n in names if n not in TABLES]
        if bad:
            sys.exit(f"unknown table(s): {', '.join(bad)}; choose from {', '.join(TABLES)}")

    root = os.path.abspath(os.path.expanduser(args.out))
    os.makedirs(root, exist_ok=True)
    final = os.path.join(root, PREFIX + args.date)
    folder = final + ".partial"
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)

    t0 = time.time()
    con = duckdb.connect()
    con.execute(f"SET threads = {args.threads}; SET memory_limit = '{args.memory}'; SET preserve_insertion_order = false")
    con.execute(f"SET temp_directory = '{os.path.join(folder, '.tmp')}'")
    con.execute("INSTALL postgres; LOAD postgres;")
    con.execute(f"ATTACH '{PG}' AS pg (TYPE postgres, READ_ONLY)")
    db_bytes = pg_scalar(con, "SELECT pg_database_size(current_database())")
    print(f"Exporting {len(names)} tables to {final}", flush=True)

    results = export(con, folder, names)
    exported_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    s = stats(con, folder)
    meta = {"date": args.date, "exported_at": exported_at, "source_db": PG, "source_db_bytes": db_bytes,
            "source_db_size": human(db_bytes), "git_commit": git_commit()}
    write_readme(con, folder, results, meta, s)
    con.close()
    shutil.rmtree(os.path.join(folder, ".tmp"), ignore_errors=True)

    manifest = dict(meta, runtime_seconds=round(time.time() - t0, 1),
                    total_bytes=sum(r["bytes"] for r in results), files=results)
    with open(os.path.join(folder, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, default=str)

    bad = [r["file"] for r in results if not r["rows_match"]]
    if os.path.exists(final):
        shutil.rmtree(final)
    os.replace(folder, final)
    print(f"\nDone in {time.time() - t0:.0f}s: {final} ({human(manifest['total_bytes'])})")

    if args.zip:
        z = make_zip(final)
        print(f"Zip: {z} ({human(os.path.getsize(z))})")
    if args.keep > 0:
        removed = prune(root, args.keep)
        if removed:
            print(f"Removed old snapshots: {', '.join(removed)}")
    if args.drive:
        copy_to_drive(final, args.drive_subdir, args.keep if args.keep > 0 else 10 ** 6)
    elif not drive_root():
        print("(Google Drive for Desktop not detected; --drive would print setup steps.)")

    if bad:
        sys.exit(f"Row count mismatch vs Postgres: {', '.join(bad)} (a refresh may have run during the export)")


if __name__ == "__main__":
    main()
