#!/usr/bin/env python3
"""Load federal grants and cooperative agreements to nonprofits from USAspending.gov into `federal_awards`
(migration 044), then link recipients to `orgs` by name and place (etl/org_matcher.py).

  python3 etl/load_federal_awards.py                 # every data/gov/usaspending/FY*_All_Assistance_Full_*.zip
  python3 etl/load_federal_awards.py --match-only

Source: the USAspending award data archive (https://files.usaspending.gov/award_data_archive/), one zip per
federal fiscal year, FY<yyyy>_All_Assistance_Full_<date>.zip (~1.3-1.5 GB, ~10 GB of CSV, one row per
transaction). Download into data/gov/usaspending/ (git-ignored):
  curl -fLO https://files.usaspending.gov/award_data_archive/FY2024_All_Assistance_Full_20261006.zip
Kept: assistance types 02 block grant, 03 formula grant, 04 project grant, 05 cooperative agreement; recipient
in the US; business types with M (nonprofit with 501(c)(3)), N (nonprofit without 501(c)(3)) or O (private
college), and without a government or public-college code (A-H, I, J, L, U: state, county, city, township,
special district, school district, public college, tribal or territorial government), and not USAID's
"MISCELLANEOUS FOREIGN AWARDEES" aggregate line. Transactions are summed
per award and fiscal year (federal_action_obligation, so deobligations net out); the newest transaction gives
the agency, assistance listing, recipient and place of performance.
"""
import csv, functools, glob, os, subprocess, sys, tempfile, time
from multiprocessing import Pool
sys.path.insert(0, os.path.dirname(__file__))
from org_matcher import link

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
DIR = os.path.join(os.path.dirname(__file__), "..", "data", "gov", "usaspending")
TYPES = {"02", "03", "04", "05"}
GOV = set("ABCDEFGHIJLU")
print = functools.partial(print, flush=True)
csv.field_size_limit(1 << 26)

def kind(codes):
    c = set(codes)
    if not c & set("MNO") or c & GOV:
        return None
    return "private_college" if "O" in c else "nonprofit_501c3" if "M" in c else "nonprofit_other"

def part(args):
    """One CSV inside one zip -> {(award, fy): row} for the kept transactions."""
    zpath, name = args
    out = {}
    p = subprocess.Popen(["unzip", "-p", zpath, name], stdout=subprocess.PIPE)
    rd = csv.DictReader((line.decode("utf-8", "replace") for line in p.stdout))
    for r in rd:
        if r["assistance_type_code"] not in TYPES or r["recipient_country_code"] not in ("USA", ""):
            continue
        k = kind(r["business_types_code"])
        if not k:
            continue
        try:
            amt = float(r["federal_action_obligation"] or 0)
        except ValueError:
            amt = 0.0
        key = (r["assistance_award_unique_key"], r["action_date_fiscal_year"])
        cur = out.get(key)
        if cur is None:
            cur = out[key] = {"amount": 0.0, "actions": 0, "date": ""}
        cur["amount"] += amt
        cur["actions"] += 1
        if r["action_date"] >= cur["date"]:
            st, co = r["prime_award_transaction_place_of_performance_state_fips_code"], r["prime_award_transaction_place_of_performance_county_fips_code"]
            cur.update(date=r["action_date"], fain=r["award_id_fain"], agency_code=r["awarding_agency_code"], agency_name=r["awarding_agency_name"],
                       sub=r["awarding_sub_agency_name"], cfda=r["cfda_number"], cfda_title=r["cfda_title"], atype=r["assistance_type_code"],
                       uei=r["recipient_uei"], name=r["recipient_name"] or r["recipient_name_raw"], city=r["recipient_city_name"],
                       state=r["recipient_state_code"][:2], zip=r["recipient_zip_code"][:5], bt=r["business_types_code"], kind=k,
                       scope=r["primary_place_of_performance_scope"], pop_state_fips=st.zfill(2) if st.isdigit() else "",
                       pop_county=co if len(co) == 5 and co.isdigit() else (st.zfill(2) + co.zfill(3)) if st.isdigit() and co.isdigit() else "",
                       desc=(r["prime_award_base_transaction_description"] or r["transaction_description"])[:500])
    p.wait()
    return out

def main():
    if "--match-only" in sys.argv:
        return match()
    zips = sorted(glob.glob(os.path.join(DIR, "FY*_All_Assistance_Full_*.zip")))
    jobs = [(z, n) for z in zips for n in subprocess.run(["unzip", "-Z1", z], capture_output=True, text=True, check=True).stdout.split() if n.endswith(".csv")]
    print(f"{len(zips)} zips, {len(jobs)} CSV parts")
    t0 = time.time()
    merged = {}
    with Pool(min(8, len(jobs))) as pool:
        for i, out in enumerate(pool.imap_unordered(part, jobs)):
            for key, v in out.items():
                cur = merged.get(key)
                if cur is None:
                    merged[key] = v
                else:  # the same award and year across two CSV parts
                    newer = v if v["date"] >= cur["date"] else cur
                    merged[key] = {**newer, "amount": cur["amount"] + v["amount"], "actions": cur["actions"] + v["actions"]}
            print(f"  {i + 1}/{len(jobs)} parts, {len(merged)} award-years ({time.time() - t0:.0f}s)")
    tmp = tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv", dir=DIR)
    w = csv.writer(tmp, lineterminator="\n")
    for (award, fy), v in merged.items():
        if not v.get("name") or v["name"].upper().startswith("MISCELLANEOUS FOREIGN AWARDEES"):
            continue  # USAID's aggregate line for foreign recipients, not a US nonprofit
        w.writerow([award, fy, v["fain"], v["agency_code"], v["agency_name"], v["sub"], v["cfda"], v["cfda_title"], v["atype"], v["uei"],
                    v["name"], v["city"], v["state"], v["zip"], v["bt"], v["kind"], v["scope"], v["pop_state_fips"], v["pop_county"],
                    round(v["amount"]), v["actions"], v["desc"]])
    tmp.close()
    out = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qAt"], text=True, capture_output=True, input=f"""BEGIN;
CREATE TEMP TABLE fa_raw (award_key text, fiscal_year smallint, fain text, agency_code text, agency_name text, sub_agency_name text,
  cfda_number text, cfda_title text, assistance_type text, recipient_uei text, recipient_name text, recipient_city text, recipient_state text,
  recipient_zip text, business_types text, recipient_kind text, pop_scope text, pop_state_fips text, pop_county_fips text, amount bigint,
  actions int, description text);
\\copy fa_raw FROM '{tmp.name}' WITH (FORMAT csv, NULL '')
TRUNCATE federal_awards RESTART IDENTITY;
-- place of performance: state FIPS -> postal code through county_need's counties
INSERT INTO federal_awards (award_key, fiscal_year, fain, agency_code, agency_name, sub_agency_name, cfda_number, cfda_title, assistance_type,
  recipient_uei, recipient_name, recipient_city, recipient_state, recipient_zip, business_types, recipient_kind, pop_scope, pop_state,
  pop_county_fips, amount, actions, description)
SELECT r.award_key, r.fiscal_year, r.fain, r.agency_code, r.agency_name, r.sub_agency_name, r.cfda_number, r.cfda_title, r.assistance_type,
       r.recipient_uei, r.recipient_name, r.recipient_city, nullif(r.recipient_state, ''), r.recipient_zip, r.business_types, r.recipient_kind,
       r.pop_scope, s.state, r.pop_county_fips, r.amount, r.actions, r.description
FROM fa_raw r LEFT JOIN (SELECT DISTINCT left(county_fips, 2) AS st, state FROM county_need WHERE county_fips <> '00000') s ON s.st = r.pop_state_fips;
COMMIT;
SELECT fiscal_year || ': ' || count(*) || ' award-years, $' || round(sum(amount) / 1e9, 2) || 'B' FROM federal_awards GROUP BY fiscal_year ORDER BY fiscal_year;
""")
    os.remove(tmp.name)
    if out.returncode:
        sys.exit(f"psql failed: {out.stderr.strip()[:2000]}")
    print(f"loaded in {time.time() - t0:.0f}s\n{out.stdout.strip()}")
    match()

def match():
    link("federal_awards", "fm", pre="UPDATE federal_awards SET recipient_ein = NULL, match_score = NULL, match_method = NULL WHERE recipient_ein IS NOT NULL;")

if __name__ == "__main__":
    main()
