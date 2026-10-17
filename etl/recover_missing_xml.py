#!/usr/bin/env python3
"""Find IRS e-file returns the index lists but data/xml doesn't hold, and fill them in.

  python3 etl/recover_missing_xml.py                    # every year with an index: check, fetch, report
  python3 etl/recover_missing_xml.py 2025 2026 --dry-run   # report only
  python3 etl/recover_missing_xml.py --no-irs           # skip the IRS batch probe, mirror only

Why: the IRS index names one batch per month, but some months ship in two zips (2025 and 2026 "05A" rows sit
in 05A and 05B), and some zips have a broken central directory. etl/fetch_990_xml.sh only reads the batches
the index names, so those returns go missing.

Steps per year:
  1. zips      each local {year}_TEOS_XML_*.zip is listed with unzip; a failed listing is reported (unzip
               still extracts what the listing reaches, which is how a broken central directory is read)
  2. batches   for each month batch on disk, the next letters (05A -> 05B, 05C ...) are probed on the IRS
               server; any that exist and aren't on disk are fetched with etl/fetch_990_xml.sh
  3. gaps      index rows with no file: 990-PF (data/xml/{year}pf, every US foundation) and 990 / 990-EZ
               (data/xml/{year}, EINs in orgs only). A type counts for a year only when at least half its
               rows are on disk: 990-EZs were never fetched for 2023-2024, and those aren't gaps
  4. mirror    the rest come from the GivingTuesday 990 data lake (an S3 mirror of the IRS files), 8 at a
               time with retries. Each file must parse as XML, and its Filer EIN and ReturnTypeCd must
               match the index, or it isn't kept. Files land in the folders the loaders read
  5. report    data/xml/unrecovered_{year}.csv: what neither source had. data/xml/recovered/{year}[pf]:
               symlinks to every file this run added, so the loaders can read just those (incremental):
               python3 etl/load_grants.py --add data/xml/recovered/2025pf ...
               python3 etl/load_charity_grants.py --add data/xml/recovered/2025 ...
"""
import csv, glob, os, re, subprocess, sys, time, urllib.error, urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
XML = os.path.join(ROOT, "data", "xml")
DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
IRS = "https://apps.irs.gov/pub/epostcard/990/xml"
MIRROR = "https://gt990datalake-rawdata.s3.amazonaws.com/EfileData/XmlFiles/{}_public.xml"
UA = "generosity-data (+https://github.com/institute-on-generosity/generosity-data)"
WORKERS = 8
FORMS = {"990PF": "pf", "990": "", "990EZ": ""}

def local(tag):
    return tag.rsplit("}", 1)[-1]

def check_zips(year):
    broken = []
    for z in sorted(glob.glob(os.path.join(XML, f"{year}_TEOS_XML_*.zip"))):
        r = subprocess.run(["unzip", "-Z1", z], capture_output=True)
        if r.returncode not in (0, 1):
            broken.append(f"{os.path.basename(z)} ({r.stdout.count(b'.xml')} files listed)")
    return broken

def http_ok(url):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False

def extra_batches(year):
    """Batches on the IRS server that aren't on disk: the next letters after each month's last local zip."""
    have = {os.path.basename(z)[len(f"{year}_TEOS_XML_"):-4].upper() for z in glob.glob(os.path.join(XML, f"{year}_TEOS_XML_*.zip"))}
    found = []
    for month in sorted({b[:2] for b in have if re.fullmatch(r"\d\d[A-Z]", b)}):
        letter = max(b[2] for b in have if b[:2] == month)
        while letter < "Z":
            letter = chr(ord(letter) + 1)
            if not http_ok(f"{IRS}/{year}/{year}_TEOS_XML_{month}{letter}.zip"):
                break
            found.append(month + letter)
    return found

def on_disk(year):
    return {suffix: {f.split("_")[0] for f in os.listdir(os.path.join(XML, f"{year}{suffix}"))}
            for suffix in ("", "pf") if os.path.isdir(os.path.join(XML, f"{year}{suffix}"))}

def gaps(year, eins):
    disk = on_disk(year)
    rows, total, present = [], {}, {}
    for r in csv.DictReader(open(os.path.join(XML, f"index_{year}.csv"), encoding="latin-1")):
        t = r["RETURN_TYPE"]
        if t not in FORMS or FORMS[t] not in disk:
            continue
        if not FORMS[t] and r["EIN"].zfill(9) not in eins:
            continue
        total[t] = total.get(t, 0) + 1
        if r["OBJECT_ID"] in disk[FORMS[t]]:
            present[t] = present.get(t, 0) + 1
        else:
            rows.append((r["OBJECT_ID"], r["EIN"].zfill(9), t, r["TAX_PERIOD"], r.get("XML_BATCH_ID", "")))
    wanted = {t for t in total if present.get(t, 0) >= total[t] / 2}
    return [g for g in rows if g[2] in wanted], {t: (present.get(t, 0), total[t]) for t in total}

def fetch(row, year):
    """-> 'ok' | 'absent' | 'mismatch' | 'error'"""
    oid, ein, form = row[:3]
    dest_dir = os.path.join(XML, f"{year}{FORMS[form]}")
    dest = os.path.join(dest_dir, f"{oid}_public.xml")
    for attempt in range(4):
        try:
            req = urllib.request.Request(MIRROR.format(oid), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r:
                body = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return "absent"
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2 ** attempt)
    else:
        return "error"
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return "mismatch"
    hdr = next((c for c in root if local(c.tag) == "ReturnHeader"), None)
    got_ein = got_form = ""
    if hdr is not None:
        for e in hdr.iter():
            if local(e.tag) == "ReturnTypeCd" and not got_form: got_form = (e.text or "").strip()
            if local(e.tag) == "Filer":
                got_ein = next(((c.text or "").strip() for c in e if local(c.tag) == "EIN"), "")
    if got_ein.zfill(9) != ein or got_form != form:
        return "mismatch"
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(body)
    os.replace(tmp, dest)
    link_dir = os.path.join(XML, "recovered", f"{year}{FORMS[form]}")
    os.makedirs(link_dir, exist_ok=True)
    link = os.path.join(link_dir, os.path.basename(dest))
    if not os.path.lexists(link):
        os.symlink(os.path.abspath(dest), link)
    return "ok"

def link_new(year, before):
    """Symlink files an IRS batch fetch added into data/xml/recovered/{year}[pf]."""
    for suffix, names in on_disk(year).items():
        new = names - before.get(suffix, set())
        d = os.path.join(XML, "recovered", f"{year}{suffix}")
        os.makedirs(d, exist_ok=True)
        for oid in new:
            src = os.path.abspath(os.path.join(XML, f"{year}{suffix}", f"{oid}_public.xml"))
            if os.path.exists(src) and not os.path.lexists(os.path.join(d, f"{oid}_public.xml")):
                os.symlink(src, os.path.join(d, f"{oid}_public.xml"))

def main():
    years = [a for a in sys.argv[1:] if a.isdigit()] or sorted(
        re.fullmatch(r"index_(\d{4})\.csv", f).group(1) for f in os.listdir(XML) if re.fullmatch(r"index_\d{4}\.csv", f))
    dry = "--dry-run" in sys.argv
    eins = set(subprocess.run(["psql", DB, "-Atc", "SELECT ein FROM orgs"], capture_output=True, text=True, check=True).stdout.split())
    for year in years:
        print(f"== {year}")
        for b in check_zips(year):
            print(f"  unzip reports a damaged zip: {b}")
        if "--no-irs" not in sys.argv:
            extra = extra_batches(year)
            if extra:
                print(f"  IRS batches not on disk: {' '.join(extra)}")
            if extra and not dry:
                before = on_disk(year)
                env = dict(os.environ, YEAR=year, BATCHES=" ".join(extra))
                if os.path.isdir(os.path.join(XML, year)):
                    subprocess.run([os.path.join(ROOT, "etl", "fetch_990_xml.sh")], env=env, check=True)
                if os.path.isdir(os.path.join(XML, f"{year}pf")):
                    subprocess.run([os.path.join(ROOT, "etl", "fetch_990_xml.sh")], env=dict(env, TYPES="990PF"), check=True)
                link_new(year, before)
        missing, cover = gaps(year, eins)
        print("  on disk: " + ", ".join(f"{t} {p:,}/{n:,}" for t, (p, n) in sorted(cover.items())))
        by_form = {}
        for g in missing:
            by_form[g[2]] = by_form.get(g[2], 0) + 1
        print("  missing: " + (", ".join(f"{t} {n:,}" for t, n in sorted(by_form.items())) or "none"))
        if dry or not missing:
            continue
        t0, counts, left = time.time(), {}, []
        with ThreadPoolExecutor(WORKERS) as pool:
            for i, (row, res) in enumerate(zip(missing, pool.map(lambda r: fetch(r, year), missing))):
                counts[(row[2], res)] = counts.get((row[2], res), 0) + 1
                if res != "ok":
                    left.append(row + (res,))
                if i % 5000 == 4999:
                    print(f"  {i + 1:,}/{len(missing):,} ({time.time() - t0:.0f}s)", flush=True)
        print("  mirror: " + ", ".join(f"{t} {r} {n:,}" for (t, r), n in sorted(counts.items())))
        out = os.path.join(XML, f"unrecovered_{year}.csv")
        with open(out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["OBJECT_ID", "EIN", "RETURN_TYPE", "TAX_PERIOD", "XML_BATCH_ID", "REASON"])
            w.writerows(left)
        print(f"  unrecovered: {len(left):,} -> {out}")

if __name__ == "__main__":
    main()
