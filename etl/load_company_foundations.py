#!/usr/bin/env python3
"""Flag company (corporate) foundations: `pf_business_contributors` and `company_foundations` (migration 045).

  python3 etl/load_company_foundations.py            # parse 990-PF XML, fetch SEC names, score, load
  python3 etl/load_company_foundations.py --no-xml   # reuse pf_business_contributors already in the DB
  python3 etl/load_company_foundations.py --dry      # score and print, write nothing

The IRS doesn't flag company foundations and NTEE T21 is unreliable (it codes the Ford Foundation as
corporate), so the flag is built from evidence on the return itself:

  contributor  Schedule B (990-PF) lists a business contributor whose name is the foundation's name stem
               ("Walmart Foundation" <- "Walmart Inc."), not a trust, estate, fund or family entity
  corp_name    the foundation's name says it belongs to a company ("Ford Motor Company Fund",
               "Alcoa Corporate Foundation", "Xerox Corp Foundation")
  sec          the name stem equals a company that files with the SEC (EDGAR company_tickers.json by name or
               ticker, or the full EDGAR CIK list for names carrying a corporate suffix)
  hq           that SEC company's business or mailing address is the foundation's city and state
               (data.sec.gov submissions, cached in data/sec/addresses.json)
  address      the contributor shares the foundation's street address (the company houses it)

A foundation is flagged with `corp_name`, or `contributor`, or `sec` + `hq`. Namesake-family foundations
(Ford, Hewlett, Packard, Kellogg, Gates, Walton) fail on evidence: their stems are people's names
("W K Kellogg", "Bill & Melinda Gates"), and their Schedule B donors are the family's trusts, estates and
people ("WK KELLOGG FOUNDATION TRUST", "LUKAS T WALTON"), which blocks a name-only match. A short surname
list backs this up for famous cases. Bank-trustee names ("SMITH TRUST UW BANK OF AMERICA NA") never
match: the stem is the whole name, not the bank at the end. Hand checks: docs/research-layer.md.

Inputs: data/xml/*pf/*.xml (all 990-PF years), data/sec/company_tickers.json and cik-lookup-data.txt
(downloaded when missing; free EDGAR bulk files). Re-run after new 990-PF batches load (no
materialized views depend on these tables; answers read them directly).
"""
import csv, json, os, re, subprocess, sys, tempfile, time, urllib.request
from multiprocessing import Pool

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
XML = os.path.join(ROOT, "data", "xml")
SEC = os.path.join(ROOT, "data", "sec")
UA = "NomBot generosity-data ETL (Institute on Generosity research)"


def psql(sql):
    out = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qAt"], input=sql, text=True, capture_output=True)
    if out.returncode: sys.exit(f"psql failed: {out.stderr.strip()[:2000]}")
    return out.stdout


# ---------------------------------------------------------------- stage 1: Schedule B business contributors
def tag(s, name):
    m = re.search(rf"<{name}>([^<]*)</{name}>", s)
    return " ".join(m.group(1).split()) if m else ""


def unesc(s):
    return s.replace("&amp;", "&").replace("&apos;", "'").replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">")


def parse(path):
    """One 990-PF -> (filer row, [business contributor rows])."""
    try:
        s = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    oid = os.path.basename(path).split("_")[0]
    fi = re.search(r"<Filer>(.*?)</Filer>", s, re.S)
    if not fi or "<ReturnTypeCd>990PF" not in s: return None
    f = fi.group(1)
    ein = tag(f, "EIN")
    filer = [ein, oid, tag(s, "TaxYr"), unesc(tag(f, "AddressLine1Txt")), unesc(tag(f, "CityNm")), tag(f, "StateAbbreviationCd"), tag(f, "ZIPCd")[:5]]
    rows = []
    i = s.find("<IRS990ScheduleB")
    if i >= 0 and "ContributorBusinessName" in s[i:]:
        for g in re.findall(r"<ContributorInformationGrp>(.*?)</ContributorInformationGrp>", s[i:], re.S):
            b = re.search(r"<ContributorBusinessName>(.*?)</ContributorBusinessName>", g, re.S)
            if not b: continue
            name = " ".join(x for x in (tag(b.group(1), "BusinessNameLine1Txt"), tag(b.group(1), "BusinessNameLine2Txt")) if x)
            if not name: continue
            amt = tag(g, "TotalContributionsAmt")
            rows.append([ein, oid, filer[2], unesc(name), unesc(tag(g, "AddressLine1Txt")), unesc(tag(g, "CityNm")),
                         tag(g, "StateAbbreviationCd"), tag(g, "ZIPCd")[:5], amt if amt.lstrip("-").isdigit() else ""])
    return filer, rows


def load_xml():
    files = []
    for d in sorted(os.listdir(XML)):
        if d.endswith("pf"):
            files += [os.path.join(XML, d, f) for f in os.listdir(os.path.join(XML, d)) if f.endswith(".xml")]
    print(f"parsing {len(files):,} 990-PF returns", flush=True)
    tmp = tempfile.mkdtemp()
    fp, cp = os.path.join(tmp, "filers.tsv"), os.path.join(tmp, "contrib.tsv")
    n = 0
    with open(fp, "w", newline="") as fo, open(cp, "w", newline="") as co, Pool(int(os.environ.get("WORKERS", "6"))) as pool:
        fw, cw = csv.writer(fo, delimiter="\t"), csv.writer(co, delimiter="\t")
        for i, r in enumerate(pool.imap_unordered(parse, files, chunksize=200)):
            if not r: continue
            fw.writerow([x.replace("\t", " ") for x in r[0]])
            for c in r[1]: cw.writerow([x.replace("\t", " ") for x in c]); n += 1
            if i % 50000 == 0: print(f"  {i:,}", flush=True)
    print(f"{n:,} business contributor lines", flush=True)
    psql(f"""BEGIN;
CREATE TEMP TABLE t_f (ein text, object_id text, tax_year text, address1 text, city text, state text, zip text);
CREATE TEMP TABLE t_c (ein text, object_id text, tax_year text, name text, address1 text, city text, state text, zip text, amount text);
\\copy t_f FROM '{fp}' WITH (FORMAT csv, DELIMITER E'\\t', QUOTE E'\\x01')
\\copy t_c FROM '{cp}' WITH (FORMAT csv, DELIMITER E'\\t', QUOTE E'\\x01')
TRUNCATE pf_business_contributors, pf_filer_address;
INSERT INTO pf_filer_address SELECT DISTINCT ON (object_id) ein, object_id, nullif(tax_year, '')::int, nullif(address1, ''), nullif(city, ''), nullif(state, ''), nullif(zip, '')
  FROM t_f WHERE ein ~ '^[0-9]{{9}}$';
INSERT INTO pf_business_contributors (ein, object_id, tax_year, name, address1, city, state, zip, amount)
  SELECT ein, object_id, nullif(tax_year, '')::int, name, nullif(address1, ''), nullif(city, ''), nullif(state, ''), nullif(zip, ''), nullif(amount, '')::bigint
  FROM t_c WHERE ein ~ '^[0-9]{{9}}$';
COMMIT;""")


# ---------------------------------------------------------------- stage 2: names
LEGAL = r"(?:incorporated|inc|corporation|corp|company|co|companies|ltd|limited|llc|l l c|lp|llp|plc|na|n a|sa|nv|ag|se|the|usa|us|of america|holdings|holding|group|international|intl|enterprises|industries|stores|national association|trust company|fsb|delaware|del|de|ny|new|mfg)"
CHARITY = r"(?:charitable|charity|community|corporate|educational|education|scholarship|employees?|employee|giving|philanthropic|philanthropy|relief|memorial|fund|funds|foundation|foundations|fdn|fndn|fdtn|found|trust|tr|endowment|program|programs|inc|incorporated|corp|corporation|co|company|the|of|ltd|association|assn|org|dot org|gives|cares|charities|for kids|initiative|ii|iii|nfp|nonprofit|non profit|private|grant|grants|usa|us|global|social impact)"


def norm(s):
    s = unesc(s).lower().replace("&", " and ").replace("'", "").replace("’", "").replace(".", " ")
    s = re.sub(r"\bc\s*/\s*o\b.*", " ", s)            # "c/o ..." in-care-of tails
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def strip_end(s, words):
    pat = re.compile(rf"(?:\s|^)(?:{words})$")
    prev = None
    while prev != s:
        prev, s = s, pat.sub("", s).strip()
    return s


def strip_start(s):
    return re.sub(r"^(?:the)\s+", "", s)


def fstem(name):
    """Foundation name -> the part that would be the company's name."""
    s = strip_start(norm(name))
    s = re.sub(r"\b(?:charitable )?(?:foundation|fdn|fund|trust)\b.*$", "", s).strip() or s   # cut at the first foundation word
    return strip_end(strip_end(s, CHARITY), r"(?:group|inc|corp|corporation|companies|holdings)")


def cstem(name):
    """Company / contributor name -> comparable stem."""
    s = re.sub(r"\s*/[a-z]{2}/?\s*$", "", unesc(name).lower())   # EDGAR state tags: "WELLS FARGO & COMPANY/MN"
    return strip_end(strip_start(norm(s)), LEGAL[:-1] + "|and|of)") or norm(s)


CORP_WORD = re.compile(r"\b(?:company|companies|corporation|corp|corporate|bancorp|bancshares|industries|mfg|manufacturing)\b\s+(?:inc |incorporated )?(?:charitable |giving |community |educational |scholarship |employees? )*(?:foundation|fund|trust|fdn|giving)\b")
TRUSTEE = re.compile(r"\b(?:tr|trust|ua|uw|tuw|fbo|u a|u w|ttee|trustee|estate|est|dtd|agmt|agreement|testamentary|irrevocable|revocable)\b")
NOT_COMPANY = re.compile(r"\b(?:trust|trustee|ttee|estate|est|foundation|fdn|fund|family|revocable|irrevocable|living|charitable|lead|unitrust|annuity|crut|clat|crat|church|ministries|donor advised|daf|schwab|fidelity|vanguard|united way|community|memorial|society|association|club|school|university|college|lodge|chapter|ira|retirement|pension|profit sharing|401|ministry|parish|diocese|temple|synagogue|lions|rotary|kiwanis|orchestra|symphony|chamber|conference|soceity|institute|center|hospital|council|league|committee|alumni|auxiliary|guild|museum|library|assoc|benevolent|botanic|garden|zoo|cooperative extension|initiative|philanthropies|philanthropic|collective)\b")
# A contributor that reads as a business: a corporate suffix or a bank / credit union.
BUSINESS = re.compile(r"\b(?:inc|incorporated|corp|corporation|co|company|companies|llc|l l c|ltd|limited|lp|llp|plc|group|bank|bancorp|credit union|holdings|industries|enterprises|partners|products|foods|brothers|bros|na|n a|sa|nv|ag|gmbh)\b")
# A family's money vehicle when the contributor only extends the foundation's stem ("Point Reyes Funding LLC").
VEHICLE = re.compile(r"\b(?:funding|investments?|investors|assets|interests|family|properties|ventures|capital|partners|partnership)\b")
# Holding and investment vehicles: a family's money, not an operating company (checked when the stem is one word).
HOLDING = re.compile(r"\b(?:holdings?|properties|investments?|investors|capital|partners|partnership|management|ventures|equities|assets|family|interests|funding|financial services)\b")
GENERIC = {"american", "national", "united", "first", "community", "general", "global", "international", "new", "great", "best",
           "hope", "faith", "grace", "love", "peace", "life", "light", "legacy", "heritage", "future", "vision", "mission",
           "star", "sun", "sunrise", "summit", "pioneer", "liberty", "freedom", "unity", "harmony", "spirit", "promise",
           "anchor", "bridge", "beacon", "cornerstone", "eagle", "phoenix", "lighthouse", "horizon", "compass", "genesis",
           "trinity", "covenant", "guardian", "alliance", "partners", "children", "kids", "youth", "health", "education",
           "arts", "music", "animal", "animals", "water", "earth", "green", "blue", "red", "gold", "silver", "diamond",
           "home", "family", "friends", "angel", "angels", "heart", "hand", "hands", "helping hands", "good", "kind", "care",
           "open", "free", "next", "one", "the", "a", "us", "usa", "america", "texas", "california", "florida", "ohio",
           "western", "eastern", "northern", "southern", "pacific", "atlantic", "central", "midwest", "capital", "city",
           "county", "state", "valley", "river", "lake", "mountain", "ocean", "coastal", "prairie", "forest", "garden",
           "rock", "stone", "oak", "pine", "cedar", "maple", "willow", "rose", "lily", "lotus", "acorn", "seed", "harvest",
           "bright", "hawk", "lion", "bear", "wolf", "fox", "deer", "dove", "crown", "kingdom", "royal", "victory",
           "triumph", "advance", "progress", "impact", "change", "action", "access", "achieve", "aspire", "inspire", "empower",
           "elevate", "thrive", "flourish", "growth", "rise", "bloom", "spark", "ignite", "evolve", "abundance", "blessing",
           "blessings", "gift", "giving", "generosity", "charity", "service", "servant", "shepherd", "journey", "path",
           "pathway", "pathways", "gateway", "crossroads", "keystone", "landmark", "milestone", "foundation", "global health"}
# Famous namesake foundations whose stem equals an SEC company's. Evidence (people contributors, given
# names) already rules most out; this list is the backstop, checked against Schedule B by hand.
FAMILY = {"ford", "hewlett", "packard", "kellogg", "gates", "walton", "duke", "lilly", "mellon", "heinz", "hess", "ball",
          "graham", "kresge", "dell", "moody", "carnegie", "rockefeller", "mott", "kauffman", "knight", "wallace", "annenberg",
          "bloomberg", "hearst", "hilton", "marriott", "pritzker", "simons", "arnold", "bezos", "koch", "mars", "reynolds",
          "pew", "sloan", "robertson", "weinberg", "helmsley", "casey", "kenan", "danforth", "brown", "smith", "johnson",
          "jones", "williams", "davis", "miller", "wilson", "moore", "taylor", "anderson", "thomas", "jackson", "white",
          "harris", "martin", "thompson", "young", "allen", "king", "wright", "scott", "green", "baker", "adams", "nelson",
          "hill", "campbell", "mitchell", "roberts", "carter", "phillips", "evans", "turner", "parker", "collins", "edwards",
          "stewart", "morris", "murphy", "cook", "rogers", "morgan", "cooper", "peterson", "reed", "bailey", "bell", "kelly",
          "howard", "ward", "cox", "richardson", "wood", "watson", "brooks", "bennett", "gray", "james", "hughes", "price",
          "sanders", "myers", "long", "ross", "foster", "powell", "jenkins", "perry", "russell", "sullivan", "fisher",
          "henderson", "coleman", "simmons", "patterson", "jordan", "reynolds", "hamilton", "graham", "wallace", "west",
          "cole", "hayes", "gibson", "ellis", "stevens", "murray", "ford", "marshall", "owens", "mcdonald", "harrison",
          "kennedy", "wells", "chase", "lowe", "hunt", "black", "stone", "hart", "lane", "dean", "page", "hale", "rich",
          "frank", "grant", "lincoln", "washington", "franklin", "timken", "noble", "weyerhaeuser",
          "kohl", "hershey", "goodyear"}


def given_names():
    """A first-name set: SSA-style list from the DB's board members (first tokens of person names)."""
    # People only: rows naming a bank or company trustee ("PNC BANK NA", "TRUIST BANK") are left out.
    out = psql("""SELECT lower(split_part(name, ' ', 1)) FROM board_members WHERE name ~ '^[A-Za-z]{3,} [A-Za-z]'
                    AND name !~* '\\m(bank|na|n a|trust|tr|co|company|inc|corp|llc|ltd|group|fsb|associates|partners|and|of|the)\\M'
                  GROUP BY 1 HAVING count(*) >= 200""")
    words = set(out.split())
    try: words |= {w.strip().lower() for w in open("/usr/share/dict/propernames")}
    except OSError: pass
    return words


def sec_names():
    os.makedirs(SEC, exist_ok=True)
    for fn, url in (("company_tickers.json", "https://www.sec.gov/files/company_tickers.json"),
                    ("cik-lookup-data.txt", "https://www.sec.gov/Archives/edgar/cik-lookup-data.txt")):
        p = os.path.join(SEC, fn)
        if not os.path.exists(p):
            print(f"downloading {url}", flush=True)
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=300) as r, open(p, "wb") as f: f.write(r.read())
    tick = {}
    for v in json.load(open(os.path.join(SEC, "company_tickers.json"))).values():
        st = cstem(v["title"])
        if st: tick.setdefault(st, (v["title"], v["cik_str"], v["ticker"]))
        if re.fullmatch(r"[A-Z]{2,5}", v["ticker"]): tick.setdefault("$" + v["ticker"].lower(), (v["title"], v["cik_str"], v["ticker"]))
    full = {}
    suffix = re.compile(r"\b(?:inc|corp|corporation|company|co|ltd|plc|llc|holdings|bancorp|bancshares)\b\.?\s*$", re.I)
    with open(os.path.join(SEC, "cik-lookup-data.txt"), encoding="latin-1") as f:
        for line in f:
            name, _, rest = line.rstrip("\n").rpartition(":")[0].rpartition(":")
            if not name or not suffix.search(name.strip()) or re.search(r"\b(?:fund|trust|foundation|estate|lp|l p|partners|capital|investors|ventures|properties|realty|acquisition|spac)\b", name, re.I):
                continue
            st = cstem(name)
            if st: full.setdefault(st, (name.strip(), int(rest) if rest.isdigit() else None, None))
    print(f"SEC: {len(tick):,} listed company stems, {len(full):,} EDGAR company stems", flush=True)
    return tick, full


def sec_address(cik, cache):
    """Business and mailing address of an SEC filer (data.sec.gov submissions), cached in data/sec/addresses.json."""
    k = str(cik)
    if k not in cache:
        time.sleep(0.12)   # SEC fair access: under 10 requests a second
        try:
            req = urllib.request.Request(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r: d = json.load(r)
            a = d.get("addresses", {})
            cache[k] = {"name": d.get("name"), "biz": a.get("business"), "mail": a.get("mailing")}
        except Exception as e:  # noqa: BLE001  a missing filer just has no address
            cache[k] = {"name": None, "biz": None, "mail": None, "error": str(e)[:80]}
        if len(cache) % 100 == 0: json.dump(cache, open(os.path.join(SEC, "addresses.json"), "w"))
    return cache[k]


def hq_match(cik, city, state, cache):
    """"city": the foundation's city and state are the company's business or mailing address (the company
    houses it). "state": same state only. None: neither."""
    if not cik or not state: return None
    a = [x for x in (lambda d: (d.get("biz"), d.get("mail")))(sec_address(cik, cache)) if x]
    if city and any(norm(x.get("city") or "") == norm(city) and x.get("stateOrCountry") == state for x in a): return "city"
    return "state" if any(x.get("stateOrCountry") == state for x in a) else None


DICT = set()
try: DICT = {w.strip().lower() for w in open("/usr/share/dict/words")}
except OSError: pass


def coined(st, first):
    """A one-word made-up brand ("amgen", "truist", "cigna"): not a dictionary word or a person's name."""
    return " " not in st and len(st) >= 3 and st not in DICT and st not in first and st not in FAMILY


# ---------------------------------------------------------------- stage 3: score
def person_like(stem, first):
    toks = stem.split()
    if not toks: return True
    if any(t in first for t in toks): return True                  # "w k kellogg", "bill and melinda gates"
    if any(len(t) == 1 for t in toks) and len(toks) > 1: return True  # initials: "j f and w s"
    return False


def match(st, cs, first):
    """Contributor stem cs names the same company as foundation stem st."""
    if cs == st: return True
    for a, b in ((st, cs), (cs, st)):   # "jpmorgan chase" ~ "jpmorgan chase bank", "wells fargo" ~ "wells fargo and"
        if b.startswith(a + " ") and len(a) >= 5 and a not in GENERIC and not person_like(a, first) \
           and (" " in a or a not in DICT): return True   # "recovery" ~ "recovery connections" is two different orgs
    return False


def score(dry=False):
    first = given_names() - {"bank", "general", "american", "united", "first", "national", "home", "farm", "state", "union", "city", "us"}
    tick, full = sec_names()
    funders = [l.split("|") for l in psql("""SELECT f.ein, replace(f.name, '|', ' '), coalesce(f.assets, 0), replace(coalesce(o.name, ''), '|', ' '),
        replace(coalesce(fa.address1, ''), '|', ' '), coalesce(fa.zip, ''), coalesce(fa.city, f.city, ''), coalesce(fa.state, f.state, '')
      FROM funders f LEFT JOIN orgs o ON o.ein = f.ein LEFT JOIN pf_filer_address fa ON fa.object_id = f.object_id""").splitlines()]
    contrib = {}
    for l in psql("""SELECT ein, replace(name, '|', ' '), replace(coalesce(address1, ''), '|', ' '), coalesce(zip, ''), coalesce(sum(amount), 0)
                     FROM pf_business_contributors GROUP BY 1, 2, 3, 4""").splitlines():
        ein, name, a1, z, amt = l.split("|")
        contrib.setdefault(ein, []).append((name, a1, z, int(amt or 0)))
    out = []
    apath = os.path.join(SEC, "addresses.json")
    sec_addr = json.load(open(apath)) if os.path.exists(apath) else {}
    for ein, name, assets, bmf_name, a1, zip5, fcity, fstate in funders:
        best = None
        for nm in dict.fromkeys(n for n in (name, bmf_name) if n):
            n = norm(nm)
            corp = bool(CORP_WORD.search(n))
            if TRUSTEE.search(n) and not corp: continue   # "X TR UW BANK OF AMERICA NA", testamentary trusts
            st = fstem(nm)
            if re.search(r"\b(?:hospitals?|church|school|university|college|museum|clinic|botanic|garden|zoo|library|ymca|society)\b", st) \
               or re.search(r"\b(?:medical|health)\b", st) and not tick.get(st): continue   # a hospital's or school's own foundation
            if len(st) < 2 or (len(st) == 2 and not re.search(r"\d", st)) or st in GENERIC or re.search(r"\b(?:family|memorial|hospital|ymca|church|school)\b", st) or re.search(r"\bmemorial\b", n): continue
            pl, fam = person_like(st, first), st in FAMILY or st.split()[-1] in FAMILY and re.match(r"^[a-z]{1,2} ", st) is not None   # "wk kellogg"
            # Funded by the namesake's own trusts or estate ("WK KELLOGG FOUNDATION TRUST"): a family foundation
            # whose name a company happens to share. Only a matching company contributor can flag it.
            namesake = any(NOT_COMPANY.search(norm(c[0])) and norm(c[0]).startswith(st + " ") for c in contrib.get(ein, []))
            # Or mostly funded by people, trusts and foundations listed under Schedule B business names
            # ("LUKAS T WALTON", "WALTON FAMILY FOUNDATION" -> Builders Vision Foundation).
            cl = contrib.get(ein, [])
            daf = re.compile(r"\b(?:philanthropy fund|charitable|donor|giving fund|schwab|fidelity|vanguard|see|attach\w*|statement|various|anonymous)\b")
            noncorp = sum(c[3] for c in cl if not BUSINESS.search(norm(c[0])) and not daf.search(norm(c[0]))
                          and (re.search(r"\b(?:family|estate|revocable|living trust)\b", norm(c[0]))
                               or (not NOT_COMPANY.search(norm(c[0])) and person_like(cstem(c[0]), first))))
            namesake = namesake or (noncorp > 0 and noncorp >= 0.5 * sum(max(c[3], 0) for c in cl))
            listed, edgar = tick.get(st) or (tick.get("$" + st) if " " not in st else None), full.get(st)   # "$ups": ticker UPS
            ev, company = [], None
            for cname, ca1, cz, amt in sorted(contrib.get(ein, []), key=lambda c: -c[3]):
                cn = norm(cname).replace("national association", "na")
                if NOT_COMPANY.search(cn) or cn == n or not (BUSINESS.search(cn) or tick.get(cstem(cname))): continue
                if HOLDING.search(cn) and (all(t in DICT for t in st.split()) or (cstem(cname) != st and VEHICLE.search(cn))): continue   # "Point Reyes Funding LLC", "Beach Road Assets LP"; "Magnetar Capital LLC" -> "Magnetar Capital Foundation" stays
                cs = cstem(cname)
                if cs == cstem(nm): continue   # the foundation itself ("SCHOLALATINAE INC")
                if " " not in st and not re.search(r"\b(?!llc\b|l l c\b)(?:inc|incorporated|corp|corporation|co|company|companies|ltd|bank|credit union|plc|na|bros|brothers|industries|products|foods)\b", cn) \
                   and not (tick.get(cs) or full.get(cs)) and not (ca1 and a1 and norm(ca1) == norm(a1)):
                    continue   # one-word name, LLC or group only, nothing else ties it to a business ("Lumiere LLC")
                if match(st, cs, first) or (st == strip_end(cs, r"(?:bank|savings bank|national bank|na)") and st not in GENERIC) or (len(st) >= 4 and cs.startswith(st) and (tick.get(cs) or full.get(cs)) and not (pl or fam)):   # Citi <- Citigroup, Prudential <- Prudential Insurance
                    ev.append("contributor"); company = cname
                    if ca1 and a1 and norm(ca1) == norm(a1) and cz == zip5: ev.append("address")
                    break
            if corp and (NOT_COMPANY.search(st) or (" " not in st and st in DICT and not (listed or edgar))): corp = False   # "Brooklyn Botanic Garden Corp Trust" is a nonprofit's own corporation
            if corp: ev.append("corp_name")
            if listed or edgar: ev.append("sec")
            hit = listed or edgar
            if hit and (listed or " " in st) and not fam:
                h = hq_match(hit[1], fcity, fstate, sec_addr)
                if h == "city": ev.append("hq")
            ok = (corp
                  or ("contributor" in ev and (not (pl or fam) or bool(listed)))
                  or ("hq" in ev and not fam and not namesake and (not pl or bool(listed) or "contributor" in ev)))
            if not ok: continue
            cand = (len(ev), [ein, company or (hit[0] if hit else st), str(hit[1] or "") if hit else "", (listed[2] if listed else "") or "",
                              "{" + ",".join(ev) + "}", st, nm, assets])
            if not best or cand[0] > best[0]: best = cand
        if best: out.append(best[1])
    json.dump(sec_addr, open(apath, "w"))
    print(f"flagged {len(out):,} company foundations", flush=True)
    if dry:
        w = csv.writer(sys.stdout, delimiter="\t")
        for r in out: w.writerow(r)
        return
    tmp = tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False, newline="")
    csv.writer(tmp, delimiter="\t").writerows([[x.replace("\t", " ") for x in r[:6]] for r in out]); tmp.close()
    psql(f"""BEGIN;
CREATE TEMP TABLE t (ein text, company text, cik text, ticker text, evidence text, stem text);
\\copy t FROM '{tmp.name}' WITH (FORMAT csv, DELIMITER E'\\t', QUOTE E'\\x01')
TRUNCATE company_foundations;
INSERT INTO company_foundations (ein, company, cik, ticker, evidence, stem)
  SELECT ein, company, nullif(cik, '')::bigint, nullif(ticker, ''), evidence::text[], stem FROM t ON CONFLICT (ein) DO NOTHING;
COMMIT;""")
    os.unlink(tmp.name)


if __name__ == "__main__":
    if "--no-xml" not in sys.argv and "--dry" not in sys.argv: load_xml()
    score(dry="--dry" in sys.argv)
