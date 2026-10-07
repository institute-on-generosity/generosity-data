#!/usr/bin/env python3
"""Extract mission + program text from IRS 990 / 990-EZ e-file XML into `filing_text`.

Reads every *_public.xml under the given directory (default data/xml/2025), which
etl/fetch_990_xml.sh fills with filings for EINs already in `orgs`.
Tags (namespace-agnostic, so schema versions don't matter):
  990    mission  = ActivityOrMissionDesc (Part I) or MissionDesc (Part III line 1)
         programs = IRS990/Desc (Part III 4a) + ProgSrvcAccomActy{2,3,Other}Grp/Desc
  990-EZ mission  = PrimaryExemptPurposeTxt
         programs = ProgramSrvcAccomplishmentGrp/DescriptionProgramSrvcAccomTxt
"""
import csv, io, os, subprocess, sys, xml.etree.ElementTree as ET

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "data", "xml", "2025")
MAX = 4000  # chars per field; keeps embeddings and rows small

def local(tag):
    return tag.rsplit("}", 1)[-1]

def text(el):
    return " ".join((el.text or "").split()) if el is not None else ""

def find(root, path):
    """Namespace-agnostic child lookup: path like 'IRS990/Desc'."""
    nodes = [root]
    for part in path.split("/"):
        nodes = [c for n in nodes for c in n if local(c.tag) == part]
    return nodes

def parse(path):
    root = ET.parse(path).getroot()
    hdr = find(root, "ReturnHeader")[0]
    form = text(find(hdr, "ReturnTypeCd")[0]) if find(hdr, "ReturnTypeCd") else ""
    ein = text((find(hdr, "Filer/EIN") or [None])[0]).zfill(9)
    year = text((find(hdr, "TaxYr") or [None])[0])
    data = (find(root, "ReturnData") or [root])[0]
    if form == "990":
        f = (find(data, "IRS990") or [None])[0]
        if f is None: return None
        mission = text((find(f, "ActivityOrMissionDesc") or find(f, "MissionDesc") or [None])[0])
        progs = [text(e) for e in find(f, "Desc")]
        for g in ("ProgSrvcAccomActy2Grp", "ProgSrvcAccomActy3Grp", "ProgSrvcAccomActyOtherGrp"):
            progs += [text(e) for e in find(f, g + "/Desc")]
    elif form == "990EZ":
        f = (find(data, "IRS990EZ") or [None])[0]
        if f is None: return None
        mission = text((find(f, "PrimaryExemptPurposeTxt") or [None])[0])
        progs = [text(e) for e in find(f, "ProgramSrvcAccomplishmentGrp/DescriptionProgramSrvcAccomTxt")]
    else:
        return None
    programs = " | ".join(dict.fromkeys(p for p in progs if p))  # dedupe, keep order
    if not (mission or programs):
        return None
    oid = os.path.basename(path).split("_")[0]
    return [oid, ein, year or "", form, mission[:MAX], programs[:MAX]]

def main():
    out = io.StringIO(); w = csv.writer(out, lineterminator="\n")
    ok = empty = bad = 0
    for name in sorted(os.listdir(SRC)):
        if not name.endswith("_public.xml"): continue
        try:
            row = parse(os.path.join(SRC, name))
        except ET.ParseError:
            bad += 1; continue
        if row: w.writerow(row); ok += 1
        else: empty += 1
    print(f"parsed: {ok} with text, {empty} without text, {bad} unparseable")
    script = """CREATE TEMP TABLE ft_raw (object_id text, ein text, tax_year int, form text, mission text, programs text);
\\copy ft_raw FROM STDIN WITH (FORMAT csv, NULL '')
""" + out.getvalue() + """\\.
INSERT INTO filing_text (object_id, ein, tax_year, form, mission, programs)
SELECT object_id, ein, tax_year, form, nullif(mission, ''), nullif(programs, '') FROM ft_raw
ON CONFLICT (object_id) DO UPDATE SET mission = EXCLUDED.mission, programs = EXCLUDED.programs;
SELECT count(*) FROM filing_text;
"""
    p = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qAt"], input=script, text=True, capture_output=True)
    if p.returncode: sys.exit(f"psql failed: {p.stderr.strip()}")
    print("filing_text rows:", p.stdout.strip().splitlines()[-1])

if __name__ == "__main__":
    main()
