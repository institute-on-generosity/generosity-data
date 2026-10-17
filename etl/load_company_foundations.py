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
  street       the foundation's house number and street are the SEC company's (stronger `hq`)
  brand        the stem is a made-up word naming a top-750 listed company ("Amgen", "Cigna", "Truist"),
               or the whole stem is a multi-word listed name ("American Electric Power")
  website      the foundation's website is the company's .com site ("cigna.com/...")
  addr_name    the foundation's street is named for the company ("One Amgen Center Drive"); recorded only

A foundation is flagged with `corp_name`, or `contributor`, or `sec` + `hq`, or `brand`, or `sec` + `website`. Namesake-family foundations
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


# Industry words a company stem may end with that a foundation drops ("Truist Financial" -> Truist Foundation,
# "Cigna Group", "Prudential Insurance", "Toyota Motor", "M&T Bank").
INDUSTRY = r"(?:financial services|financial group|financial|group|holdings?|bancorp|bancshares|bankshares|banc|bank|insurance|mutual|life|brands|systems|technologies|motors?|health|healthcare|services|international|intl)"


def core(st):
    """A stem without trailing industry words: "truist financial" -> "truist". Empty when nothing is left."""
    c = strip_end(st, INDUSTRY)
    return c if c != st and len(c) >= 2 else ""


def singular(st):
    return st[:-1] if len(st) >= 6 and st.endswith("s") and not st.endswith("ss") else ""   # "walgreens" -> "walgreen"


def sec_names(cache):
    """SEC company names -> {stem: [(name, cik, ticker, rank, current)]}. current: a ticker title or current EDGAR name, not a former one. rank: place in company_tickers.json (EDGAR
    orders it by market value, 0 = largest), None for unlisted EDGAR filers. Keys are each name's stem and its
    `core()`; the 1,500 largest listed companies add their current and former EDGAR names (GE Aerospace was
    General Electric, Walgreens Boots Alliance was Walgreen)."""
    os.makedirs(SEC, exist_ok=True)
    for fn, url in (("company_tickers.json", "https://www.sec.gov/files/company_tickers.json"),
                    ("cik-lookup-data.txt", "https://www.sec.gov/Archives/edgar/cik-lookup-data.txt")):
        p = os.path.join(SEC, fn)
        if not os.path.exists(p):
            print(f"downloading {url}", flush=True)
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=300) as r, open(p, "wb") as f: f.write(r.read())
    tick, full = {}, {}

    def add(d, name, e, cap=4):
        st = cstem(name)
        for k in dict.fromkeys(x for x in (st, core(st)) if x):
            lst = d.setdefault(k, [])
            if len(lst) < cap and all(x[1] != e[1] for x in lst): lst.append(e)

    rows = list(json.load(open(os.path.join(SEC, "company_tickers.json"))).values())
    for i, v in enumerate(rows):
        e = (v["title"], v["cik_str"], v["ticker"], i, True)
        add(tick, v["title"], e)
        if re.fullmatch(r"[A-Z]{2,5}", v["ticker"]): tick.setdefault("$" + v["ticker"].lower(), [e])
    big = {}
    for i, v in enumerate(rows[:1500]):
        big.setdefault(v["cik_str"], (v["ticker"], i))
    for cik, (tk, i) in big.items():
        d = sec_address(cik, cache, former=True)
        for nm in [d.get("name")] + [f.get("name") if isinstance(f, dict) else f for f in d.get("former") or []]:
            if nm: add(tick, nm, (nm, cik, tk, i, nm == d.get("name")))
    json.dump(cache, open(os.path.join(SEC, "addresses.json"), "w"))
    suffix = re.compile(r"\b(?:inc|corp|corporation|company|co|ltd|plc|llc|holdings|bancorp|bancshares|bank|national association|n a)\b\.?\s*$", re.I)
    with open(os.path.join(SEC, "cik-lookup-data.txt"), encoding="latin-1") as f:
        for line in f:
            name, _, rest = line.rstrip("\n").rpartition(":")[0].rpartition(":")
            if not name or not suffix.search(re.sub(r"\s*/[A-Z]{2}/?\s*$", "", name.strip())) or re.search(r"\b(?:fund|trust|foundation|estate|lp|l p|partners|capital|investors|ventures|properties|realty|acquisition|spac)\b", name, re.I):
                continue
            add(full, name, (name.strip(), int(rest) if rest.isdigit() else None, None, None, False), cap=3)
    print(f"SEC: {len(tick):,} listed company stems, {len(full):,} EDGAR company stems", flush=True)
    return tick, full


def sec_address(cik, cache, former=False):
    """Business and mailing address (and former names) of an SEC filer (data.sec.gov submissions), cached in
    data/sec/addresses.json."""
    k = str(cik)
    if k not in cache or (former and "former" not in cache[k] and "error" not in cache[k]):
        time.sleep(0.12)   # SEC fair access: under 10 requests a second
        try:
            req = urllib.request.Request(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r: d = json.load(r)
            a = d.get("addresses", {})
            cache[k] = {"name": d.get("name"), "biz": a.get("business"), "mail": a.get("mailing"),
                        "former": [{"name": x.get("name")} for x in d.get("formerNames") or []], "website": d.get("website") or ""}
        except Exception as e:  # noqa: BLE001  a missing filer just has no address
            cache[k] = {"name": None, "biz": None, "mail": None, "error": str(e)[:80]}
        if len(cache) % 100 == 0: json.dump(cache, open(os.path.join(SEC, "addresses.json"), "w"))
    return cache[k]


NUMWORD = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"}
STREETWORD = {"street": "st", "road": "rd", "drive": "dr", "avenue": "ave", "av": "ave", "boulevard": "blvd", "parkway": "pkwy",
              "place": "pl", "plaza": "plz", "center": "ctr", "centre": "ctr", "square": "sq", "north": "n", "south": "s",
              "east": "e", "west": "w", "lane": "ln", "court": "ct", "circle": "cir", "highway": "hwy", "way": "way"}


def street_key(a):
    """"1 NATIONWIDE PLAZA 1-LC-IRS" -> ("1", "nationwide"): house number and first street word. None for PO boxes."""
    t = [NUMWORD.get(w, STREETWORD.get(w, w)) for w in norm(a).split()]
    if len(t) < 2 or not t[0].isdigit() or t[0] == "0": return None
    rest = [w for w in t[1:] if w not in ("n", "s", "e", "w")]
    return (t[0], rest[0]) if rest else None


def hq_match(cik, city, state, a1, zip5, cache):
    """"street": the foundation's street address is the company's business or mailing address (number and street,
    same ZIP or city). "city": same city and state (the company houses it). "state": same state only. None: neither."""
    if not cik or not state: return None
    a = [x for x in (lambda d: (d.get("biz"), d.get("mail")))(sec_address(cik, cache)) if x]
    sk = street_key(a1) if a1 else None
    for x in a:
        same_city = city and norm(x.get("city") or "") == norm(city) and x.get("stateOrCountry") == state
        if sk and street_key(x.get("street1") or "") == sk and (same_city or (x.get("zipCode") or "")[:5] == zip5): return "street"
    if city and any(norm(x.get("city") or "") == norm(city) and x.get("stateOrCountry") == state for x in a): return "city"
    return "state" if any(x.get("stateOrCountry") == state for x in a) else None


# The foundation's street is named after the company: "1 Nationwide Plaza", "One Amgen Center Drive".
ADDR_NAMED = r"^(?:\d+\w*|one|two|three|four|five) {} (?:plaza|plz|center|centre|ctr|campus|tower|towers|place|pl|square|sq|park|building|bldg|headquarters|hq|circle|cir|commons|corporate|global|world|way|drive|dr|pkwy|parkway|blvd|boulevard)\b"


def web_label(w):
    """"HTTP:/WWW.CIGNA.COM/ABOUTUS/..." -> "cigna": the site's name when it is a .com site, else ""."""
    h = re.sub(r"^[a-z]+:/*", "", (w or "").strip().lower()).split("/")[0].split("?")[0]
    parts = [p for p in h.split(".") if p]
    return parts[-2] if len(parts) >= 2 and parts[-1] == "com" else ""


DICT = set()
try: DICT = {w.strip().lower() for w in open("/usr/share/dict/words")}
except OSError: pass


STATES = {"alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida", "georgia",
          "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts",
          "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
          "new mexico", "new york", "north carolina", "north dakota", "oklahoma", "oregon", "pennsylvania", "rhode island",
          "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
          "wisconsin", "wyoming"}


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


def lookup(d, st):
    """SEC entries for a foundation stem: the stem itself, then its core ("cigna group" -> "cigna"), then its
    singular ("walgreens" -> "walgreen"). Returns (entries, key)."""
    for k in dict.fromkeys(x for x in (st, core(st), singular(st)) if x and (x == st or x not in GENERIC)):
        if d.get(k):
            # "ge": GE HealthCare's core name, but the ticker GE is the closer match
            if k == st and " " not in st and d.get("$" + st) and not any(cstem(e[0]) == k for e in d[k]): return d["$" + st] + d[k], k
            return d[k], k
    if " " not in st and d.get("$" + st): return d["$" + st], "$" + st   # "$ups": ticker UPS
    return [], st


def score(dry=False):
    first = given_names() - {"bank", "general", "american", "united", "first", "national", "home", "farm", "state", "union", "city", "us"}
    # Last names of board members ("Pearson", "Eaton", "Waters") and filers' city names ("Fairfax", "Brookfield"):
    # a brand-like stem that is one of these may be a family or a place, not the listed company.
    surnames = set(psql("""SELECT lower(substring(name FROM '([A-Za-z]+)\\s*$')) FROM board_members WHERE name ~ '^[A-Za-z]+ '
                             GROUP BY 1 HAVING count(*) >= 10""").split())
    cities = set(norm(c) for c in psql("SELECT DISTINCT city FROM pf_filer_address WHERE city IS NOT NULL").splitlines())
    apath = os.path.join(SEC, "addresses.json")
    sec_addr = json.load(open(apath)) if os.path.exists(apath) else {}
    tick, full = sec_names(sec_addr)
    funders = [l.split("|") for l in psql("""SELECT f.ein, replace(f.name, '|', ' '), coalesce(f.assets, 0), replace(coalesce(o.name, ''), '|', ' '),
        replace(coalesce(fa.address1, ''), '|', ' '), coalesce(fa.zip, ''), coalesce(fa.city, f.city, ''), coalesce(fa.state, f.state, ''),
        replace(coalesce(f.website, ''), '|', ' ')
      FROM funders f LEFT JOIN orgs o ON o.ein = f.ein LEFT JOIN pf_filer_address fa ON fa.object_id = f.object_id""").splitlines()]
    contrib = {}
    for l in psql("""SELECT ein, replace(name, '|', ' '), replace(coalesce(address1, ''), '|', ' '), coalesce(zip, ''), coalesce(sum(amount), 0)
                     FROM pf_business_contributors GROUP BY 1, 2, 3, 4""").splitlines():
        ein, name, a1, z, amt = l.split("|")
        contrib.setdefault(ein, []).append((name, a1, z, int(amt or 0)))
    out = []
    corp_sfx = re.compile(r"\b(?!llc\b|l l c\b)(?:inc|incorporated|corp|corporation|co|company|companies|ltd|bank|credit union|plc|na|bros|brothers|industries|products|foods)\b")
    for ein, name, assets, bmf_name, a1, zip5, fcity, fstate, website in funders:
        best = None
        for nm in dict.fromkeys(n for n in (name, bmf_name) if n):
            # "TR UA FIFTH THIRD FOUNDATION", "TEXTRON CHARITABLE TRUST DTD 122353": a company foundation held in trust
            nm = re.sub(r"^\s*(?:tr|trust)\s+u\s?[aw]\s+", "", re.sub(r"\s+(?:dtd|u\s?a|uad)\s+\d.*$", "", nm, flags=re.I), flags=re.I)
            n = norm(nm)
            corp = bool(CORP_WORD.search(n))
            held = re.search(r" (?:charitable|foundation|giving|educational) trust$", n) and not TRUSTEE.search(n[:n.rindex(" trust")])
            if TRUSTEE.search(n) and not corp and not held: continue   # "X TR UW BANK OF AMERICA NA", testamentary trusts
            st = fstem(nm)
            listed, lkey = lookup(tick, st)
            if re.search(r"\b(?:hospitals?|church|school|university|college|museum|clinic|botanic|garden|zoo|library|ymca|society)\b", st) \
               or re.search(r"\b(?:medical|health)\b", st) and not listed: continue   # a hospital's or school's own foundation
            # A generic stem only counts with a corporate name and the listed company's headquarters ("Southern Company Charitable Foundation")
            generic = st in GENERIC
            if generic and not (corp and tick.get(st)): continue
            if len(st) < 2 or (len(st) == 2 and not re.search(r"\d", st) and not tick.get(st)) or re.search(r"\b(?:family|memorial|hospital|ymca|church|school)\b", st) or re.search(r"\bmemorial\b", n): continue
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
            edgar, ekey = lookup(full, st)
            # "Comcast NBCUniversal Foundation": no SEC name, but its first word is a listed company's. Good for
            # street and website evidence only.
            tok = st.split()[0]
            prefix = not (listed or edgar) and " " in st and len(tok) >= 4 and tok not in GENERIC and tok not in first \
                and tok not in FAMILY and tok not in DICT and bool(tick.get(tok))
            if prefix: listed, lkey = tick[tok], tok
            listed1, edgar1 = (listed[0] if listed else None), (edgar[0] if edgar else None)
            ev, company = [], None
            for cname, ca1, cz, amt in sorted(contrib.get(ein, []), key=lambda c: -c[3]):
                cn = norm(cname).replace("national association", "na")
                cs = cstem(cname)
                if NOT_COMPANY.search(cn) or cn == n or not (BUSINESS.search(cn) or tick.get(cs)): continue
                if HOLDING.search(cn) and (all(t in DICT for t in st.split()) or (cs != st and VEHICLE.search(cn))): continue   # "Point Reyes Funding LLC", "Beach Road Assets LP"; "Magnetar Capital LLC" -> "Magnetar Capital Foundation" stays
                if cs == cstem(nm): continue   # the foundation itself ("SCHOLALATINAE INC")
                if " " not in st and not corp_sfx.search(cn) and not (tick.get(cs) or full.get(cs)) and not (ca1 and a1 and norm(ca1) == norm(a1)):
                    continue   # one-word name, LLC or group only, nothing else ties it to a business ("Lumiere LLC")
                cc = core(cs) if (corp_sfx.search(cn) or tick.get(cs)) and not generic else ""   # "Prudential Insurance Company of America" -> prudential
                same = bool(ca1 and a1 and street_key(ca1) and street_key(ca1) == street_key(a1))
                if match(st, cs, first) or (cc and cc == st and len(st) >= 4) or (cs and corp_sfx.search(cn) and cs == core(st) and len(cs) >= 3) \
                   or (same and tick.get(cs) and st.endswith(" " + cs)) or (st == strip_end(cs, r"(?:bank|savings bank|national bank|na)") and st not in GENERIC) or (len(st) >= 4 and cs.startswith(st) and (tick.get(cs) or full.get(cs)) and not (pl or fam)):   # Citi <- Citigroup
                    ev.append("contributor"); company = cname
                    if ca1 and a1 and norm(ca1) == norm(a1) and cz == zip5: ev.append("address")
                    break
            if corp and (NOT_COMPANY.search(re.sub(r"\b(?:schwab|fidelity|vanguard)\b", "", st)) and not tick.get(st) or (" " not in st and st in DICT and not (listed or edgar))): corp = False   # "Brooklyn Botanic Garden Corp Trust" is a nonprofit's own corporation
            if corp: ev.append("corp_name")
            if listed or edgar: ev.append("sec")
            key = lkey if listed else ekey
            made_up = not key.startswith("$") and coined(key, first) and len(key) >= 4 and singular(key) not in DICT and key not in surnames and key not in cities
            # A foundation named for its city is a community foundation ("Hartford Foundation for Public Giving").
            city_named = norm(fcity) in (st, key) or st in STATES
            hit, h = listed1 or edgar1, None
            if (listed or edgar) and not fam:
                cands = (listed or []) + (edgar or [])
                deep = (bool(listed) or " " in key) and not prefix   # city-level match only for these; street-level for any
                for e in cands[:4]:
                    if not e[1]: continue
                    # City: the first SEC match only (or any listed one), and a short stem needs a large company ("PNC")
                    city_ok = deep and not city_named and (e is cands[0] or e[3] is not None) \
                        and (len(key) >= 4 or "contributor" in ev or corp or (e[3] is not None and e[3] < 750))
                    # Street: a family office (Form D "Gores Group LLC", "Mirabito Holdings") shares its family
                    # foundation's address, so an unlisted LLC, group or holding company doesn't count
                    office = e[3] is None and bool(re.search(r"\b(?:llc|l l c|lp|l p|group|enterprises)\b", norm(e[0])) or HOLDING.search(norm(e[0])))
                    if not city_ok and (office or not (a1 and street_key(a1))): continue
                    r = hq_match(e[1], fcity, fstate, a1, zip5, sec_addr)
                    if r == "street" and not office or r in ("street", "city") and city_ok:
                        hit, h = e, ("street" if r == "street" and not office else "city")
                        if h == "street": break
                if h: ev.append("hq")
                if h == "street": ev.append("street")
            # A large listed company (top 1,000 by market value) whose name is a made-up word: "Amgen", "Cigna", "Truist"
            big = [e for e in (listed or []) if e[3] is not None and e[3] < 750 and e[4]] if not prefix else []   # current names only
            # Or the foundation's whole name is a large listed company's, two words or more: "American Electric Power"
            if big and (made_up and key in (st, core(st)) or key == st and " " in st and not fam and any(e[4] and cstem(e[0]) == st for e in big)) and not pl and not namesake and not generic \
               and re.search(r"\b(?:foundation|fund|trust|cares|giving|charitable|charities|philanthropy|philanthropies)\b", n) and not re.search(r"\bcommunit(?:y|ies) (?:foundation|fund|trust)\b", n):
                ev.append("brand"); hit = hit if h else big[0]
            if (listed or edgar) and not generic:
                if a1 and len(key) >= 4 and re.match(ADDR_NAMED.format(re.escape(key)), " ".join(NUMWORD.get(w, w) if i == 0 else w for i, w in enumerate(norm(a1).split()))):
                    ev.append("addr_name")
                lab = web_label(website)
                if lab and (lab == key.replace(" ", "") or any(e[2] and lab == e[2].lower() for e in (listed or [])[:2])):
                    ev.append("website")
            ok = (corp and (not generic or h)
                  or ("contributor" in ev and (not (pl or fam) or bool(listed) or ("address" in ev and bool(tick.get(cstem(company)))))) 
                  or ("hq" in ev and not generic and not fam and not namesake and (not pl or bool(listed) or "contributor" in ev))
                  or ("brand" in ev)
                  or ("website" in ev and not generic and not fam and not namesake and (not pl or bool(listed))))
            if not ok: continue
            cand = (len(ev), [ein, company or (hit[0] if hit else st), str(hit[1] or "") if hit else "", (hit[2] if hit and hit[2] else (listed1[2] if listed1 else "")) or "",
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
DELETE FROM company_foundations c WHERE NOT EXISTS (SELECT 1 FROM t WHERE t.ein = c.ein);   -- incremental: no empty table mid-load
INSERT INTO company_foundations (ein, company, cik, ticker, evidence, stem)
  SELECT DISTINCT ON (ein) ein, company, nullif(cik, '')::bigint, nullif(ticker, ''), evidence::text[], stem FROM t
  ON CONFLICT (ein) DO UPDATE SET company = excluded.company, cik = excluded.cik, ticker = excluded.ticker, evidence = excluded.evidence, stem = excluded.stem
  WHERE (company_foundations.company, company_foundations.cik, company_foundations.ticker, company_foundations.evidence, company_foundations.stem)
        IS DISTINCT FROM (excluded.company, excluded.cik, excluded.ticker, excluded.evidence, excluded.stem);
COMMIT;""")
    os.unlink(tmp.name)


if __name__ == "__main__":
    if "--no-xml" not in sys.argv and "--dry" not in sys.argv: load_xml()
    score(dry="--dry" in sys.argv)
