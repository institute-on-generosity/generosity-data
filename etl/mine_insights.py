#!/usr/bin/env python3
"""Mine, verify and store the patterns behind NomBot's Insights tab (table `insights`, migration 025).

  python3 etl/mine_insights.py                  # recompute every insight, verify, upsert, drop retired slugs
  python3 etl/mine_insights.py --only a,b       # just these slugs
  python3 etl/mine_insights.py --dry            # print headlines and checks, write nothing
  python3 etl/mine_insights.py --list           # slugs by section

Each insight is one function: a SQL query (stored in `query`, reproduces the numbers), an independent
check query, a Vega-Lite chart and text written from the numbers. `verified` is true only when every
check passes (the check query agrees and sample sizes are large enough). Safe to run nightly after
`db/refresh.sql`: rows are updated in place by slug and `refreshed_at` moves.

Audience: philanthropic researchers and advisors. Every number is recomputed on each run; text never
hard-codes a figure. Grants link to a recipient organization for ~63% of rows, so any claim that needs a
recipient's cause or EIN says so in `caveats` (the live link rate is filled in).

Stdlib + psql only.
"""
import argparse
import csv
import json
import os
import secrets
import subprocess
import sys
import time

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
HERE = os.path.dirname(os.path.abspath(__file__))
US10M = "https://cdn.jsdelivr.net/npm/vega-datasets@2.8.0/data/us-10m.json"
VL = "https://vega.github.io/schema/vega-lite/v5.json"
SECTIONS = ["funding-gaps", "rising-fading", "money-flows", "track-record", "affiliations", "financial-stress"]

# ---------------------------------------------------------------- sources
SRC = {
    "pf": {"label": "IRS Form 990-PF e-file returns, Part XV grants (IRS releases 2023–2026)", "url": "https://www.irs.gov/charities-non-profits/form-990-series-downloads"},
    "990": {"label": "IRS Form 990 e-file returns (IRS releases 2023–2026)", "url": "https://www.irs.gov/charities-non-profits/form-990-series-downloads"},
    "bmf": {"label": "IRS Exempt Organizations Business Master File", "url": "https://www.irs.gov/charities-non-profits/exempt-organizations-business-master-file-extract-eo-bmf"},
    "soi": {"label": "IRS SOI annual extract of exempt-organization financial data", "url": "https://www.irs.gov/statistics/soi-tax-stats-annual-extract-of-tax-exempt-organization-financial-data"},
    "pop": {"label": "U.S. Census Bureau population estimates, 2024", "url": "https://www.census.gov/programs-surveys/popest.html"},
    "saipe": {"label": "U.S. Census Bureau SAIPE poverty estimates, 2023", "url": "https://www.census.gov/programs-surveys/saipe.html"},
    "zcta": {"label": "Census 2020 ZCTA-to-county relationship file", "url": "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/"},
    "arc": {"label": "Appalachian Regional Commission counties", "url": "https://www.arc.gov/appalachian-counties-served-by-arc/"},
    "ntee": {"label": "NTEE code definitions (NCCS, via Nonprofit Open Data Collective)", "url": "https://github.com/Nonprofit-Open-Data-Collective/mission-taxonomies"},
    "board": {"label": "Board members from 990 Part VII and 990-PF officer lists (generosity-data board_interlocks)", "url": "https://www.irs.gov/charities-non-profits/form-990-series-downloads"},
}

# ---------------------------------------------------------------- NTEE labels
MAJOR = {  # same plain-language labels as NomBot (web/lib/ntee.ts)
    "A": "Arts, Culture & Humanities", "B": "Education", "C": "Environment", "D": "Animal-Related", "E": "Health Care",
    "F": "Mental Health", "G": "Diseases & Disorders", "H": "Medical Research", "I": "Crime & Legal", "J": "Employment",
    "K": "Food, Agriculture & Nutrition", "L": "Housing & Shelter", "M": "Public Safety & Disaster", "N": "Recreation & Sports",
    "O": "Youth Development", "P": "Human Services", "Q": "International Affairs", "R": "Civil Rights & Advocacy",
    "S": "Community Improvement", "T": "Philanthropy & Grantmaking", "U": "Science & Technology", "V": "Social Science",
    "W": "Public & Societal Benefit", "X": "Religion-Related", "Y": "Mutual & Membership Benefit", "Z": "Unknown",
}
with open(os.path.join(HERE, "ntee_codes.csv"), newline="", encoding="utf-8") as fh:
    NTEE = {r["code"]: r["label"] for r in csv.DictReader(fh)}


def label(code):
    code = (code or "").strip()
    if len(code) == 1:
        return MAJOR.get(code, code)
    return NTEE.get(code[:3]) or MAJOR.get(code[:1], code)


STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico",
}

# ---------------------------------------------------------------- shared SQL
# 2022-24 grants (the three full tax years) to US organizations: no individuals, placeholders ("see statement")
# or foreign recipients, and no transfers to another private foundation (a family's trust paying its own
# foundation would otherwise swamp every total). Address fields are on every grant, linked or not.
CLEAN = """clean AS (
  SELECT g.* FROM grants g LEFT JOIN orgs o ON o.ein = g.recipient_ein
  WHERE g.tax_year BETWEEN 2022 AND 2024 AND g.amount > 0 AND g.recipient_kind IS NULL
    AND coalesce(o.foundation, '') NOT IN ('02', '03', '04'))"""
# Newest full Form 990 per charity, with usable revenue, expenses and cash.
F990 = """f AS (
  SELECT DISTINCT ON (ein) * FROM filing_flag WHERE form = '990' ORDER BY ein, tax_year DESC, object_id DESC)"""
F990_OK = "f.cy_revenue > 0 AND f.cy_expenses > 0 AND f.cash IS NOT NULL"
# Mid-size public charities: newest SOI financials on a full 990 with $250K-$5M revenue.
MIDSIZE = """u AS (
  SELECT o.ein, o.ntee_cd, o.state, f.revenue FROM orgs o JOIN financials_latest f USING (ein)
  WHERE o.subsection = '03' AND o.foundation NOT IN ('02', '03', '04') AND f.form = '990'
    AND f.revenue BETWEEN 250000 AND 5000000 AND o.ntee_cd ~ '^[A-Z][0-9]{2}')"""
BAND = """CASE WHEN {x} < 1e6 THEN '1 Under $1M' WHEN {x} < 1e7 THEN '2 $1M–10M' WHEN {x} < 1e8 THEN '3 $10M–100M'
            WHEN {x} < 1e9 THEN '4 $100M–1B' ELSE '5 $1B+' END"""
REV_BAND = """CASE WHEN {x} < 5e5 THEN '1 Under $500K' WHEN {x} < 2e6 THEN '2 $500K–2M' WHEN {x} < 1e7 THEN '3 $2M–10M'
                WHEN {x} < 1e8 THEN '4 $10M–100M' ELSE '5 $100M+' END"""
# High-confidence hidden board links where one side paid the other (funder -> grantee, both directions).
BOARD_PAIRS = """bp AS (SELECT ein_a, ein_b FROM board_interlocks WHERE relation = 'link' AND confidence = 'high' AND kind = 'funder-grantee'),
pp AS (SELECT ein_a AS f, ein_b AS r FROM bp UNION SELECT ein_b, ein_a FROM bp)"""


# ---------------------------------------------------------------- db
def psql(sql, stdin=None):
    r = subprocess.run(["psql", DB, "-X", "-At", "-v", "ON_ERROR_STOP=1", *([] if stdin else ["-c", sql])],
                       input=stdin, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"psql failed: {r.stderr.strip()[:800]}\n--- SQL ---\n{(sql or stdin)[:1500]}")
    return r.stdout


def q(sql):
    """Rows of a query as a list of dicts (numbers as float/int)."""
    sql = sql.strip().rstrip(";")
    out = psql(f"SELECT coalesce(json_agg(t), '[]') FROM ({sql}) t")
    return json.loads(out)


def q1(sql):
    rows = q(sql)
    return rows[0] if rows else {}


# ---------------------------------------------------------------- formatting
def money(x, digits=None):
    x = float(x)
    a = abs(x)
    if a >= 1e9:
        return f"${x / 1e9:.{1 if digits is None else digits}f}B"
    if a >= 1e6:
        return f"${x / 1e6:.{0 if a >= 1e8 else 1}f}M"
    if a >= 1e4:
        return f"${x / 1e3:.0f}K"
    return f"${x:,.0f}"


def pct(x, d=0):
    return f"{100 * float(x):.{d}f}%"


def n(x):
    return f"{int(round(float(x))):,}"


def close(a, b, rel=0.01, abs_=1e-9):
    a, b = float(a), float(b)
    return abs(a - b) <= max(abs_, rel * max(abs(a), abs(b)))


def strip_band(s):
    return s.split(" ", 1)[1] if s[:1].isdigit() and " " in s else s


# ---------------------------------------------------------------- charts (Vega-Lite v5, data inline)
def _base(values, height):
    return {"$schema": VL, "width": "container", "height": height, "data": {"values": values}}


def hbar(values, y, x, x_title, y_title=None, fmt=".0%", sort="-x", highlight=None, rule=None, color_field=None,
         tooltip_extra=(), height=None, color_title=None):
    """Horizontal bars, one per category. highlight: (field, value) drawn in the accent color; rule: x value line."""
    spec = _base(values, height or max(160, 24 * len(values)))
    enc = {
        "y": {"field": y, "type": "nominal", "sort": sort, "title": y_title, "axis": {"labelLimit": 260}},
        "x": {"field": x, "type": "quantitative", "title": x_title, "axis": {"format": fmt}},
        "tooltip": [{"field": y, "type": "nominal"}, {"field": x, "type": "quantitative", "format": fmt}]
                   + [{"field": f, "type": t, **({"format": ff} if ff else {})} for f, t, ff in tooltip_extra],
    }
    if color_field:
        enc["color"] = {"field": color_field, "type": "nominal", "title": color_title}
    elif highlight:
        f, v = highlight
        enc["color"] = {"condition": {"test": f"datum['{f}'] === {json.dumps(v)}", "value": "#d97706"}, "value": "#4c78a8"}
    bars = {"mark": {"type": "bar"}, "encoding": enc}
    if rule is None:
        spec.update(bars)
        return spec
    spec["layer"] = [bars, {"data": {"values": [{"v": rule}]}, "mark": {"type": "rule", "strokeDash": [4, 3], "color": "#6b7280"},
                            "encoding": {"x": {"field": "v", "type": "quantitative"}}}]
    return spec


def vbar(values, x, y, x_title, y_title, fmt=",.0f", sort=None, xoffset=None, color_title=None, height=260, label_fmt=None):
    spec = _base(values, height)
    enc = {
        "x": {"field": x, "type": "nominal", "title": x_title, "sort": sort, "axis": {"labelAngle": 0, "labelLimit": 140}},
        "y": {"field": y, "type": "quantitative", "title": y_title, "axis": {"format": fmt}},
        "tooltip": [{"field": x, "type": "nominal"}, {"field": y, "type": "quantitative", "format": fmt}],
    }
    if xoffset:
        enc["xOffset"] = {"field": xoffset}
        enc["color"] = {"field": xoffset, "type": "nominal", "title": color_title, "legend": {"orient": "top", "labelLimit": 400, "direction": "vertical"}}
        enc["tooltip"].insert(1, {"field": xoffset, "type": "nominal"})
    layers = [{"mark": {"type": "bar"}, "encoding": enc}]
    if label_fmt and not xoffset:
        layers.append({"mark": {"type": "text", "dy": -6}, "encoding": {
            "x": enc["x"], "y": enc["y"], "text": {"field": y, "type": "quantitative", "format": label_fmt}}})
    spec["layer"] = layers
    return spec


def line(values, x, y, x_title, y_title, fmt=",.0f", color=None, x_type="ordinal", height=260):
    spec = _base(values, height)
    enc = {
        "x": {"field": x, "type": x_type, "title": x_title, "axis": {"labelAngle": 0}},
        "y": {"field": y, "type": "quantitative", "title": y_title, "axis": {"format": fmt}},
        "tooltip": [{"field": x, "type": x_type}, {"field": y, "type": "quantitative", "format": fmt}],
    }
    if color:
        enc["color"] = {"field": color, "type": "nominal", "title": None, "legend": {"orient": "top"}}
        enc["tooltip"].insert(0, {"field": color, "type": "nominal"})
    spec["mark"] = {"type": "line", "point": True}
    spec["encoding"] = enc
    return spec


def diverging(values, y, x, x_title, fmt="+.0%", height=None, rule=None):
    spec = hbar(values, y, x, x_title, fmt=fmt, sort="-x", height=height, rule=rule)
    layer0 = spec["layer"][0] if "layer" in spec else spec
    layer0["encoding"]["color"] = {"condition": {"test": f"datum['{x}'] < 0", "value": "#c2410c"}, "value": "#4c78a8"}
    return spec


def scatter(values, x, y, x_title, y_title, label_field, size=None, fmt=".0%", label_test=None, tooltip_extra=()):
    spec = _base(values, 340)
    enc = {
        "x": {"field": x, "type": "quantitative", "title": x_title, "axis": {"format": fmt}},
        "y": {"field": y, "type": "quantitative", "title": y_title, "axis": {"format": fmt}},
        "tooltip": [{"field": label_field, "type": "nominal"}, {"field": x, "type": "quantitative", "format": fmt},
                    {"field": y, "type": "quantitative", "format": fmt}]
                   + [{"field": f, "type": t, **({"format": ff} if ff else {})} for f, t, ff in tooltip_extra],
    }
    if size:
        enc["size"] = {"field": size, "type": "quantitative", "title": "Charities", "legend": None}
    pts = {"mark": {"type": "circle", "opacity": 0.6}, "encoding": enc}
    layers = [pts]
    if label_test:
        layers.append({"transform": [{"filter": label_test}],
                       "mark": {"type": "text", "align": "left", "dx": 7, "fontSize": 11},
                       "encoding": {"x": enc["x"], "y": enc["y"], "text": {"field": label_field, "type": "nominal"}}})
    spec["layer"] = layers
    return spec


def state_map(values, field, title, fmt=".0%", scale=None, tooltip_extra=()):
    """Choropleth of US states; values need `id` (state FIPS number) and `state`."""
    feature = {"url": US10M, "format": {"type": "topojson", "feature": "states"}}
    color = {"field": field, "type": "quantitative", "title": title, "legend": {"format": fmt, "orient": "bottom", "titleLimit": 400}}
    if scale:
        color["scale"] = scale
    return {
        "$schema": VL, "width": "container", "height": 380, "projection": {"type": "albersUsa"},
        "layer": [
            {"data": feature, "mark": {"type": "geoshape", "fill": "#e5e7eb", "stroke": "white", "strokeWidth": 0.5}},
            {"data": {"values": values},
             "transform": [{"lookup": "id", "from": {"data": feature, "key": "id"}, "as": "geo"}],
             "mark": {"type": "geoshape", "stroke": "white", "strokeWidth": 0.5},
             "encoding": {"shape": {"field": "geo", "type": "geojson"}, "color": color,
                          "tooltip": [{"field": "name", "type": "nominal", "title": "State"},
                                      {"field": field, "type": "quantitative", "format": fmt, "title": title}]
                                     + [{"field": f, "type": t, "title": tt, **({"format": ff} if ff else {})} for f, t, tt, ff in tooltip_extra]}},
        ],
    }


def county_map(values, field, title, fmt="$,.0f", scale=None, tooltip_extra=()):
    """Choropleth of US counties; values need `id` (county FIPS number). State borders drawn on top."""
    counties = {"url": US10M, "format": {"type": "topojson", "feature": "counties"}}
    states = {"url": US10M, "format": {"type": "topojson", "mesh": "states"}}
    color = {"field": field, "type": "quantitative", "title": title, "legend": {"format": fmt, "orient": "bottom", "titleLimit": 400}}
    if scale:
        color["scale"] = scale
    return {
        "$schema": VL, "width": "container", "height": 400, "projection": {"type": "albersUsa"},
        "layer": [
            {"data": counties, "mark": {"type": "geoshape", "fill": "#e5e7eb", "stroke": None}},
            {"data": {"values": values},
             "transform": [{"lookup": "id", "from": {"data": counties, "key": "id"}, "as": "geo"}],
             "mark": {"type": "geoshape", "stroke": None},
             "encoding": {"shape": {"field": "geo", "type": "geojson"}, "color": color,
                          "tooltip": [{"field": "county", "type": "nominal", "title": "County"},
                                      {"field": field, "type": "quantitative", "format": fmt, "title": title}]
                                     + [{"field": f, "type": t, "title": tt, **({"format": ff} if ff else {})} for f, t, tt, ff in tooltip_extra]}},
            {"data": states, "mark": {"type": "geoshape", "filled": False, "stroke": "white", "strokeWidth": 0.6}},
        ],
    }


def heatmap(values, x, y, color, x_title, y_title, title, fmt=".0%", height=None):
    spec = _base(values, height or 24 * len({v[y] for v in values}) + 40)
    spec["mark"] = {"type": "rect"}
    spec["encoding"] = {
        "x": {"field": x, "type": "nominal", "title": x_title, "axis": {"labelAngle": -90, "labelFontSize": 9}},
        "y": {"field": y, "type": "nominal", "title": y_title, "axis": {"labelLimit": 220}},
        "color": {"field": color, "type": "quantitative", "title": title, "legend": {"format": fmt, "orient": "bottom", "titleLimit": 400}},
        "tooltip": [{"field": y, "type": "nominal"}, {"field": x, "type": "nominal"},
                    {"field": color, "type": "quantitative", "format": fmt}, {"field": "charities", "type": "quantitative"}],
    }
    return spec


# ---------------------------------------------------------------- registry
REG = []


def insight(slug, section, score, cause=None, state=None, explore=None):
    assert section in SECTIONS, section

    def deco(fn):
        REG.append({"slug": slug, "section": section, "score": score, "cause": cause, "state": state, "explore": explore, "fn": fn})
        return fn
    return deco


class Ctx:
    def __init__(self):
        r = q1("""SELECT avg((recipient_ein IS NOT NULL)::int) AS rate,
                         sum(amount) FILTER (WHERE recipient_ein IS NOT NULL) / sum(amount) AS dollars,
                         avg((recipient_ein IS NOT NULL)::int) FILTER (WHERE recipient_kind IS NULL) AS rate_linkable
                  FROM grants WHERE tax_year BETWEEN 2022 AND 2024""")
        self.link = (f"Recipient organizations are identified for {pct(r['rate'])} of 2022–24 grants "
                     f"({pct(r['rate_linkable'])} of grants to US organizations, {pct(r['dollars'])} of dollars), matched by name and address; "
                     "unmatched grants are missing from cause and organization totals.")
        self.addr = ("Grants are placed by the recipient's address on the 990-PF, which is often a headquarters: national groups in "
                     "New York, Washington DC and Boston collect grants for work elsewhere.")


def checks_ok(checks):
    return all(ok for _, ok in checks)


# ================================================================ FUNDING GAPS

REACH_SQL = f"""WITH {MIDSIZE},
funded AS (SELECT DISTINCT recipient_ein AS ein FROM grants
           WHERE tax_year BETWEEN 2022 AND 2024 AND recipient_ein IS NOT NULL AND amount > 0)
SELECT left(u.ntee_cd, 3) AS code, count(*) AS charities, count(funded.ein) AS funded,
       round(count(funded.ein)::numeric / count(*), 3) AS share_funded
FROM u LEFT JOIN funded USING (ein)
GROUP BY 1 HAVING count(*) >= 300 ORDER BY share_funded"""


@insight("gap-subcause-reach", "funding-gaps", 0.82, cause="P", explore="child day care centers")
def gap_subcause_reach(ctx):
    rows = q(REACH_SQL)
    tot = q1(f"""WITH {MIDSIZE} SELECT count(*) AS charities,
        avg((EXISTS (SELECT 1 FROM grants g WHERE g.recipient_ein = u.ein AND g.tax_year BETWEEN 2022 AND 2024 AND g.amount > 0))::int) AS share
        FROM u""")
    for r in rows:
        r["cause"] = f"{r['code']} {label(r['code'])}"
    by = {r["code"]: r for r in rows}
    care = by["P33"]
    top = rows[-1]
    # independent recount for child care with EXISTS instead of a join
    v = q1(f"""SELECT count(*) AS charities, avg((EXISTS (SELECT 1 FROM grants g WHERE g.recipient_ein = o.ein
              AND g.tax_year BETWEEN 2022 AND 2024 AND g.amount > 0))::int) AS share
              FROM orgs o JOIN financials_latest f ON f.ein = o.ein
              WHERE o.ntee_cd LIKE 'P33%' AND o.subsection = '03' AND o.foundation NOT IN ('02','03','04') AND f.form = '990'
                AND f.revenue BETWEEN 250000 AND 5000000""")
    low = rows[:15]
    chart = hbar(low + rows[-5:], "cause", "share_funded", "Share with any identified foundation grant, 2022–24",
                 highlight=("code", "P33"), rule=float(tot["share"]),
                 tooltip_extra=[("charities", "quantitative", ","), ("funded", "quantitative", ",")])
    orgs = q("""SELECT o.ein FROM orgs o JOIN financials_latest f USING (ein)
                WHERE o.ntee_cd LIKE 'P33%' AND o.subsection = '03' AND f.form = '990' AND f.revenue BETWEEN 250000 AND 5000000
                  AND NOT EXISTS (SELECT 1 FROM grants g WHERE g.recipient_ein = o.ein AND g.tax_year BETWEEN 2022 AND 2024)
                ORDER BY f.revenue DESC LIMIT 60""")
    return {
        "headline": f"Only {pct(care['share_funded'])} of mid-size child care nonprofits got a foundation grant in 2022–24, versus {pct(tot['share'])} of all mid-size charities",
        "summary": (f"Of {n(care['charities'])} child day care providers with $250K–$5M in revenue, {n(care['funded'])} received an identifiable private-foundation grant. "
                    f"Fee-funded fields (senior housing, fire departments, charter schools) sit even lower; {label(top['code']).lower()} tops the list at {pct(top['share_funded'])}. "
                    "For a donor, low reach means a first gift can matter more, and it also means few peer funders to share diligence with."),
        "chart": chart,
        "method": ("Public charities (501(c)(3), not private foundations) filing a full Form 990 with $250K–$5M revenue in their newest IRS SOI record, grouped by 3-character NTEE code (300+ charities per code). "
                   "A charity counts as funded if any 990-PF grant from tax years 2022–24 is linked to its EIN. Chart: the 15 lowest and 5 highest codes; dashed line = all mid-size charities."),
        "caveats": ctx.link + " Unlinked grants make every share a floor, but the gap between codes is far larger than the matching error. Fee-for-service fields are expected to rely less on foundations.",
        "sources": [SRC["pf"], SRC["soi"], SRC["bmf"], SRC["ntee"]],
        "query": REACH_SQL,
        "data": rows,
        "org_eins": [r["ein"] for r in orgs],
        "checks": [("child care recount", close(v["share"], care["share_funded"], abs_=0.002) and int(v["charities"]) == int(care["charities"])),
                   ("child care n >= 300", int(care["charities"]) >= 300), ("codes >= 20", len(rows) >= 20)],
    }


STATE_PC_SQL = f"""WITH {CLEAN},
pop AS (SELECT state, left(min(county_fips), 2)::int AS id, sum(population) AS population
        FROM county_need WHERE county_fips <> '00000' GROUP BY state),
st AS (SELECT recipient_state AS state, sum(amount) AS amount, count(*) AS grants FROM clean GROUP BY 1)
SELECT pop.state, pop.id, pop.population, st.grants, round(st.amount) AS amount_2022_24,
       round(st.amount / pop.population / 3) AS per_resident_year
FROM pop JOIN st USING (state) ORDER BY per_resident_year"""


@insight("gap-state-per-capita", "funding-gaps", 0.78)
def gap_state_per_capita(ctx):
    rows = q(STATE_PC_SQL)
    for r in rows:
        r["name"] = STATE_NAMES.get(r["state"], r["state"])
    lo1, lo2 = rows[0], rows[1]
    states = [r for r in rows if r["state"] != "DC"]
    hi = states[-1]
    v = {}
    for s in (lo1["state"], hi["state"]):
        v[s] = q1(f"""SELECT sum(g.amount) / (SELECT sum(population) FROM county_need WHERE state = '{s}' AND county_fips <> '00000') / 3 AS pc
                      FROM grants g WHERE g.recipient_state = '{s}' AND g.tax_year IN (2022, 2023, 2024) AND g.amount > 0
                        AND g.recipient_kind IS NULL
                        AND NOT EXISTS (SELECT 1 FROM orgs o WHERE o.ein = g.recipient_ein AND o.foundation IN ('02','03','04'))""")["pc"]
    chart = state_map(rows, "per_resident_year", "Foundation $ per resident per year", fmt="$,.0f",
                      scale={"type": "log", "scheme": "blues"},
                      tooltip_extra=[("amount_2022_24", "quantitative", "Grants 2022–24", "$,.0f")])
    ratio = hi["per_resident_year"] / lo1["per_resident_year"]
    return {
        "headline": (f"{lo1['name']} and {lo2['name']} receive about {money(lo1['per_resident_year'])} per resident a year in foundation grants; "
                     f"{hi['name']} receives {money(hi['per_resident_year'])}, {ratio:.0f} times as much"),
        "summary": (f"Private-foundation dollars follow wealth and institutions, not need: {', '.join(r['name'] for r in rows[:5])} draw the least per person. "
                    "Advisors weighing place-based giving can treat low-dollar states as under-served by institutional philanthropy."),
        "chart": chart,
        "method": ("All 990-PF grants for tax years 2022–24 to US organizations (excluding grants to individuals, unitemized lines, foreign recipients and transfers to other private foundations), "
                   "summed by the recipient's state and divided by 2024 Census population and by 3 years. Colors on a log scale; DC is far off the scale."),
        "caveats": ctx.addr + " Only private foundations are counted (no community-foundation, DAF or corporate-giving-program grants). Unlinked grants are included here because the address is always present.",
        "sources": [SRC["pf"], SRC["pop"]],
        "query": STATE_PC_SQL,
        "data": rows,
        "org_eins": None,
        "checks": [(f"{s} recount", close(v[s], r["per_resident_year"], rel=0.01, abs_=1)) for s, r in ((lo1["state"], lo1), (hi["state"], hi))]
                  + [("51 states", len(rows) >= 50)],
    }


COUNTY_SQL = f"""WITH {CLEAN},
c AS (SELECT county_fips, name, state, population, poverty_rate FROM county_need WHERE county_fips <> '00000' AND population > 0),
app AS (SELECT county_fips, bool_or(appalachia) AS appalachia FROM zip_regions GROUP BY 1),
amt AS (SELECT z.county_fips, sum(g.amount) AS amount FROM clean g JOIN zip_regions z ON z.zip5 = left(g.recipient_zip, 5) GROUP BY 1)
SELECT c.county_fips, c.name, c.state, c.population, c.poverty_rate, coalesce(app.appalachia, false) AS appalachia,
       coalesce(amt.amount, 0) AS amount_2022_24, round(coalesce(amt.amount, 0) / c.population / 3, 2) AS per_resident_year
FROM c LEFT JOIN app USING (county_fips) LEFT JOIN amt USING (county_fips)"""


def _county_rows():
    rows = q(COUNTY_SQL)
    return rows


def _median(xs):
    xs = sorted(float(x) for x in xs)
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


@insight("gap-county-size", "funding-gaps", 0.74, explore="rural community foundations")
def gap_county_size(ctx):
    rows = _county_rows()
    bands = [("Under 10K", 0, 1e4), ("10K–50K", 1e4, 5e4), ("50K–250K", 5e4, 2.5e5), ("250K–1M", 2.5e5, 1e6), ("1M+", 1e6, 1e12)]
    out = []
    for name, lo, hi in bands:
        rs = [r for r in rows if lo <= r["population"] < hi]
        pop = sum(r["population"] for r in rs)
        out.append({"band": name, "counties": len(rs), "population": pop,
                    "median_county_per_resident": round(_median([r["per_resident_year"] for r in rs]), 2),
                    "pooled_per_resident": round(sum(r["amount_2022_24"] for r in rs) / pop / 3, 2)})
    small, big = out[1], out[4]
    sql = COUNTY_SQL + """\n-- then: per band of county population, the median county's per_resident_year and sum(amount)/sum(population)/3"""
    v = q1(f"""WITH {CLEAN}
        SELECT sum(g.amount) / (SELECT sum(population) FROM county_need WHERE county_fips <> '00000' AND population >= 1000000) / 3 AS pc
        FROM clean g JOIN zip_regions z ON z.zip5 = left(g.recipient_zip, 5)
        WHERE z.county_fips IN (SELECT county_fips FROM county_need WHERE county_fips <> '00000' AND population >= 1000000)""")
    chart = vbar(out, "band", "median_county_per_resident", "County population", "Median county: $ per resident per year",
                 fmt="$,.0f", sort=None, label_fmt="$,.0f")
    ratio = big["median_county_per_resident"] / small["median_county_per_resident"]
    return {
        "headline": (f"The typical county of 10,000–50,000 people receives {money(small['median_county_per_resident'])} per resident a year in foundation grants; "
                     f"the typical county of 1 million+ receives {money(big['median_county_per_resident'])}"),
        "summary": (f"Across {n(small['counties'])} small counties home to {small['population'] / 1e6:.0f} million people, foundation grants per person run about "
                    f"{ratio:.0f} times lower than in big metros. Rural capacity, not rural need, is the binding constraint: there are fewer grant-ready organizations to fund."),
        "chart": chart,
        "method": ("2022–24 990-PF grants to US organizations (no individuals, unitemized lines, foreign or private-foundation recipients), placed in a county by recipient ZIP (Census ZCTA crosswalk), "
                   "divided by 2024 county population and by 3 years. Bars show the median county in each population band, so a few rich counties don't drive the result."),
        "caveats": ctx.addr + " About 15% of grant dollars have a ZIP that doesn't map to a county and are left out. County population size is a proxy for rural; it is not the USDA rural-urban code.",
        "sources": [SRC["pf"], SRC["pop"], SRC["zcta"]],
        "query": sql,
        "data": out,
        "org_eins": None,
        "checks": [("1M+ pooled recount", close(v["pc"], big["pooled_per_resident"], rel=0.01)),
                   ("counties >= 3,000", len(rows) >= 3000), ("small band >= 500 counties", small["counties"] >= 500)],
    }


@insight("gap-county-poverty", "funding-gaps", 0.8, explore="anti-poverty programs in high-poverty rural counties")
def gap_county_poverty(ctx):
    rows = [r for r in _county_rows() if r["poverty_rate"] is not None]
    rows.sort(key=lambda r: float(r["poverty_rate"]))
    k = len(rows)
    out = []
    for i in range(5):
        rs = rows[i * k // 5:(i + 1) * k // 5]
        out.append({"quintile": ["Lowest poverty", "Low", "Middle", "High", "Highest poverty"][i],
                    "poverty_rate_range": f"{rs[0]['poverty_rate']}–{rs[-1]['poverty_rate']}%", "counties": len(rs),
                    "population": sum(r["population"] for r in rs),
                    "median_county_per_resident": round(_median([r["per_resident_year"] for r in rs]), 2)})
    lo, hi = out[0], out[4]
    v = q1(f"""WITH {CLEAN},
        c AS (SELECT county_fips, population, ntile(5) OVER (ORDER BY poverty_rate, county_fips) AS q FROM county_need
              WHERE county_fips <> '00000' AND poverty_rate IS NOT NULL AND population > 0),
        a AS (SELECT z.county_fips, sum(amount) AS amt FROM clean g JOIN zip_regions z ON z.zip5 = left(g.recipient_zip, 5) GROUP BY 1)
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY coalesce(a.amt, 0) / c.population / 3) FILTER (WHERE q = 1) AS lo,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY coalesce(a.amt, 0) / c.population / 3) FILTER (WHERE q = 5) AS hi
        FROM c LEFT JOIN a USING (county_fips)""")
    chart = vbar(out, "quintile", "median_county_per_resident", "Counties by poverty rate (fifths)", "Median county: $ per resident per year",
                 fmt="$,.0f", sort=None, label_fmt="$,.0f")
    sql = COUNTY_SQL + "\n-- then: counties split into fifths by poverty_rate; median per_resident_year in each fifth"
    return {
        "headline": (f"The poorest fifth of US counties gets a median {money(hi['median_county_per_resident'])} per resident a year in foundation grants, "
                     f"about a third of the {money(lo['median_county_per_resident'])} in the least-poor fifth"),
        "summary": (f"In counties where {hi['poverty_rate_range']} of people live in poverty, the typical county draws "
                    f"{lo['median_county_per_resident'] / hi['median_county_per_resident']:.1f} times less foundation money per person than affluent counties. "
                    "Need and institutional dollars run in opposite directions; a donor's marginal dollar likely goes further in the high-poverty fifth."),
        "chart": chart,
        "method": ("Counties ranked by 2023 Census SAIPE poverty rate and split into five equal groups. 2022–24 990-PF grants to US organizations (no individuals, unitemized lines, foreign or "
                   "private-foundation recipients) placed by recipient ZIP, divided by 2024 population and 3 years; bars show each group's median county."),
        "caveats": ctx.addr + " About 15% of grant dollars have a ZIP that doesn't map to a county. Grants to a city charity can serve surrounding counties.",
        "sources": [SRC["pf"], SRC["saipe"], SRC["pop"], SRC["zcta"]],
        "query": sql,
        "data": out,
        "org_eins": None,
        "checks": [("lowest fifth recount", close(v["lo"], lo["median_county_per_resident"], rel=0.03, abs_=0.5)),
                   ("highest fifth recount", close(v["hi"], hi["median_county_per_resident"], rel=0.03, abs_=0.5)),
                   ("600+ counties per fifth", min(o["counties"] for o in out) >= 600)],
    }


@insight("gap-appalachia-map", "funding-gaps", 0.76, explore="Appalachian community development")
def gap_appalachia_map(ctx):
    rows = _county_rows()
    app = [r for r in rows if r["appalachia"]]
    rest = [r for r in rows if not r["appalachia"]]

    def pooled(rs):
        return sum(r["amount_2022_24"] for r in rs) / sum(r["population"] for r in rs) / 3
    a, b = pooled(app), pooled(rest)
    v = q1(f"""WITH {CLEAN},
        ac AS (SELECT DISTINCT county_fips FROM zip_regions WHERE appalachia)
        SELECT sum(g.amount) / (SELECT sum(population) FROM county_need WHERE county_fips IN (SELECT county_fips FROM ac)) / 3 AS pc
        FROM clean g JOIN zip_regions z ON z.zip5 = left(g.recipient_zip, 5) WHERE z.county_fips IN (SELECT county_fips FROM ac)""")
    values = [{"id": int(r["county_fips"]), "county": f"{r['name']}, {r['state']}", "per_resident_year": max(float(r["per_resident_year"]), 0.5),
               "appalachia": r["appalachia"]} for r in rows]
    chart = county_map(values, "per_resident_year", "Foundation $ per resident per year", fmt="$,.0f",
                       scale={"type": "log", "scheme": "blues", "domain": [1, 1000], "clamp": True})
    data = sorted(({"county_fips": r["county_fips"], "county": r["name"], "state": r["state"], "appalachia": r["appalachia"],
                    "population": r["population"], "poverty_rate": r["poverty_rate"], "per_resident_year": r["per_resident_year"]}
                   for r in app), key=lambda r: float(r["per_resident_year"]))[:500]
    return {
        "headline": f"Appalachian counties receive {money(a)} per resident a year in foundation grants, {pct(a / b)} of the {money(b)} the rest of the country gets",
        "summary": (f"The {n(len(app))} counties of the Appalachian Regional Commission's footprint ({sum(r['population'] for r in app) / 1e6:.0f} million people) draw well under half "
                    "the per-person foundation support of other US counties. The map shows the same pattern across the rural South and Plains."),
        "chart": chart,
        "method": ("2022–24 990-PF grants to US organizations (no individuals, unitemized lines, foreign or private-foundation recipients), placed by recipient ZIP via the Census ZCTA-county file, "
                   "summed per county and divided by 2024 population and 3 years. Appalachia = ARC counties. Map colors on a log scale, capped at $1,000. CSV: the 500 lowest-funded Appalachian counties."),
        "caveats": ctx.addr + " About 15% of grant dollars have a ZIP that doesn't map to a county.",
        "sources": [SRC["pf"], SRC["pop"], SRC["zcta"], SRC["arc"]],
        "query": COUNTY_SQL + "\n-- then: sum(amount_2022_24)/sum(population)/3 for appalachia = true vs false",
        "data": data,
        "org_eins": None,
        "checks": [("Appalachia recount", close(v["pc"], a, rel=0.01)), ("400+ ARC counties", len(app) >= 400)],
    }


HEAT_SQL = f"""WITH {MIDSIZE},
funded AS (SELECT DISTINCT recipient_ein AS ein FROM grants WHERE tax_year BETWEEN 2022 AND 2024 AND recipient_ein IS NOT NULL AND amount > 0)
SELECT u.state, left(u.ntee_cd, 1) AS major, count(*) AS charities, round(count(funded.ein)::numeric / count(*), 3) AS share_funded
FROM u LEFT JOIN funded USING (ein)
WHERE left(u.ntee_cd, 1) IN ('A', 'B', 'C', 'D', 'E', 'K', 'L', 'N', 'O', 'P', 'S')
GROUP BY 1, 2 HAVING count(*) >= 40"""


# Dropped: headline driven by charter-school mix (Arizona education), not a real gap.
# @insight("gap-cause-state-grid", "funding-gaps", 0.62, explore="human services nonprofits")
def gap_cause_state_grid(ctx):
    rows = q(HEAT_SQL)
    for r in rows:
        r["cause"] = MAJOR[r["major"]]
    states = {r["state"] for r in rows}
    rows = [r for r in rows if r["state"] in STATE_NAMES]
    # the cause with the widest spread between states (n >= 100 per cell)
    big = [r for r in rows if r["charities"] >= 100]
    by = {}
    for r in big:
        by.setdefault(r["major"], []).append(r)
    spread = sorted(((max(x["share_funded"] for x in rs) - min(x["share_funded"] for x in rs), m) for m, rs in by.items() if len(rs) >= 20), reverse=True)
    m = spread[0][1]
    lo = min(by[m], key=lambda r: r["share_funded"])
    hi = max(by[m], key=lambda r: r["share_funded"])
    v = q1(f"""SELECT count(*) AS charities, avg((EXISTS (SELECT 1 FROM grants g WHERE g.recipient_ein = o.ein AND g.tax_year BETWEEN 2022 AND 2024 AND g.amount > 0))::int) AS share
               FROM orgs o JOIN financials_latest f USING (ein)
               WHERE o.state = '{lo['state']}' AND o.ntee_cd LIKE '{m}%' AND o.ntee_cd ~ '^[A-Z][0-9]{{2}}' AND o.subsection = '03'
                 AND o.foundation NOT IN ('02','03','04') AND f.form = '990' AND f.revenue BETWEEN 250000 AND 5000000""")
    chart = heatmap(rows, "state", "cause", "share_funded", "State", None, "Share with a foundation grant")
    return {
        "headline": (f"{MAJOR[m]} charities in {STATE_NAMES[lo['state']]}: {pct(lo['share_funded'])} got a foundation grant in 2022–24; "
                     f"in {STATE_NAMES[hi['state']]}, {pct(hi['share_funded'])}"),
        "summary": (f"Foundation reach for the same kind of mid-size charity varies widely by state; {MAJOR[m].lower()} shows the biggest spread among states with 100+ such charities. "
                    "The grid shows where a cause is thinly funded locally, a starting point for place-based gap analyses."),
        "chart": chart,
        "method": ("Public charities filing a full 990 with $250K–$5M revenue (newest IRS SOI record), by state and NTEE major group (11 large groups, cells with 40+ charities). "
                   "Funded = any 990-PF grant from tax years 2022–24 linked to the charity's EIN. Headline compares states with 100+ charities in the cause."),
        "caveats": ctx.link + " Link rates differ somewhat by state, which can widen gaps.",
        "sources": [SRC["pf"], SRC["soi"], SRC["bmf"]],
        "query": HEAT_SQL,
        "data": rows[:500],
        "org_eins": None,
        "cause": m,
        "explore": f"{MAJOR[m].lower()} nonprofits in {STATE_NAMES[lo['state']]}",
        "checks": [("low cell recount", close(v["share"], lo["share_funded"], abs_=0.002) and int(v["charities"]) == int(lo["charities"])),
                   ("low cell n >= 100", lo["charities"] >= 100), ("states >= 45", len(states) >= 45)],
    }


# ================================================================ RISING / FADING

RULING_SQL = """SELECT left(ruling, 4)::int AS year, count(*) AS new_charities
FROM orgs WHERE subsection = '03' AND ruling ~ '^20(1[5-9]|2[0-5])'
GROUP BY 1 ORDER BY 1"""


@insight("rise-new-charities", "rising-fading", 0.72, explore="newly formed nonprofits")
def rise_new_charities(ctx):
    rows = q(RULING_SQL)
    by = {r["year"]: r["new_charities"] for r in rows}
    last = max(by)
    v = q1(f"SELECT count(*) AS c FROM orgs WHERE subsection = '03' AND ruling BETWEEN '{last}01' AND '{last}12'")
    chg = by[last] / by[last - 2] - 1
    chart = line(rows, "year", "new_charities", "Year the IRS recognized the charity", "Charities still on the IRS list")
    return {
        "headline": f"The IRS recognized {n(by[last])} new charities in {last}, up {pct(chg)} from {last - 2}",
        "summary": ("New 501(c)(3) formation has climbed every year since 2021. For advisors this means more young, unrated organizations in every cause, "
                    "and more need for diligence tools that don't depend on years of filings."),
        "chart": chart,
        "method": "501(c)(3) organizations on the current IRS Business Master File, counted by the year of their IRS ruling (recognition) date, 2015–" + str(last) + ".",
        "caveats": ("Only organizations still on the IRS list are counted: charities that dissolved or lost exemption drop out, so older years are undercounted "
                    "(the IRS revokes status after three years without a filing). Comparisons between the last three years are least affected."),
        "sources": [SRC["bmf"]],
        "query": RULING_SQL,
        "data": rows,
        "org_eins": None,
        "checks": [("last-year recount", int(v["c"]) == int(by[last])), ("n >= 10,000", by[last] >= 10000)],
    }


NEW_SUB_SQL = """WITH r AS (
  SELECT left(ntee_cd, 3) AS code, left(ruling, 4)::int AS year FROM orgs
  WHERE subsection = '03' AND ntee_cd ~ '^[A-Z][0-9]{2}' AND left(ruling, 4) IN ('2023', '2025'))
SELECT code, count(*) FILTER (WHERE year = 2023) AS new_2023, count(*) FILTER (WHERE year = 2025) AS new_2025,
       round(count(*) FILTER (WHERE year = 2025)::numeric / nullif(count(*) FILTER (WHERE year = 2023), 0) - 1, 3) AS growth
FROM r GROUP BY 1
HAVING count(*) FILTER (WHERE year = 2023) >= 100 AND count(*) FILTER (WHERE year = 2025) >= 100
ORDER BY growth DESC"""


@insight("rise-new-charities-by-cause", "rising-fading", 0.72)
def rise_new_by_cause(ctx):
    rows = q(NEW_SUB_SQL)
    tot = q1("""SELECT count(*) FILTER (WHERE left(ruling, 4) = '2023') AS a, count(*) FILTER (WHERE left(ruling, 4) = '2025') AS b
                FROM orgs WHERE subsection = '03' AND ntee_cd ~ '^[A-Z][0-9]{2}'""")
    overall = tot["b"] / tot["a"] - 1
    for r in rows:
        r["cause"] = f"{r['code']} {label(r['code'])}"
    # disaster relief has its own insight (rise-disaster-charities), so headline the next fastest
    top = next(r for r in rows if not r["code"].startswith("M2"))
    shown = rows[:12] + rows[-6:]
    v = q1(f"""SELECT count(*) FILTER (WHERE ruling LIKE '2023%') AS a, count(*) FILTER (WHERE ruling LIKE '2025%') AS b
               FROM orgs WHERE subsection = '03' AND ntee_cd LIKE '{top['code']}%'""")
    states = q(f"""SELECT state, count(*) AS new_2025 FROM orgs WHERE subsection = '03' AND ntee_cd LIKE '{top['code']}%' AND ruling LIKE '2025%'
                   GROUP BY 1 ORDER BY 2 DESC LIMIT 3""")
    chart = diverging(shown, "cause", "growth", "Change in new charities, 2025 vs 2023", rule=overall)
    orgs = q(f"""SELECT ein FROM orgs WHERE subsection = '03' AND ntee_cd LIKE '{top['code']}%' AND ruling LIKE '2025%'
                 ORDER BY coalesce(revenue_amt, 0) DESC LIMIT 60""")
    return {
        "headline": (f"New {label(top['code']).lower()} charities grew {pct(top['growth'])} from 2023 to 2025 ({n(top['new_2023'])} to {n(top['new_2025'])}), "
                     f"against {pct(overall)} for all new charities"),
        "summary": (f"The 2025 cohort leans toward {', '.join(s['state'] for s in states)}, where recent floods, hurricanes and fires hit; most of these groups are small and new. "
                    "For donors giving after a disaster, many local recovery charities are too new to have a 990 on file."
                    if top["code"].startswith("M") else
                    f"The 2025 cohort is concentrated in {', '.join(s['state'] for s in states)}. Fast-growing sub-causes mean more young organizations competing for the same funders."),
        "chart": chart,
        "method": ("501(c)(3) organizations on the IRS Business Master File by NTEE code and IRS ruling year, 2025 vs 2023 cohorts (codes with 100+ new charities in each year). "
                   "Chart: 12 fastest-growing and 6 slowest codes; dashed line = all codes."),
        "caveats": "Ruling year counts only organizations still listed; both cohorts are young, so dissolutions affect them little. NTEE codes are assigned by the IRS from the application and can be imprecise.",
        "sources": [SRC["bmf"], SRC["ntee"]],
        "query": NEW_SUB_SQL,
        "data": rows,
        "org_eins": [r["ein"] for r in orgs],
        "cause": top["code"][0],
        "explore": f"new {label(top['code']).lower()} nonprofits",
        "checks": [("top code recount", int(v["a"]) == int(top["new_2023"]) and int(v["b"]) == int(top["new_2025"])),
                   ("top code n >= 100", top["new_2023"] >= 100)],
    }


CAUSE_TREND_SQL = """WITH panel AS (  -- foundations with grants in both 2022 and 2024
  SELECT funder_ein FROM grants WHERE tax_year IN (2022, 2024) GROUP BY 1 HAVING count(DISTINCT tax_year) = 2),
g AS (
  SELECT left(o.ntee_cd, 1) AS major, g.tax_year, sum(least(g.amount, 5000000)) AS capped, sum(g.amount) AS raw
  FROM grants g JOIN panel USING (funder_ein) JOIN orgs o ON o.ein = g.recipient_ein
  WHERE g.tax_year IN (2022, 2024) AND g.amount > 0 AND o.foundation NOT IN ('02', '03', '04') AND o.ntee_cd ~ '^[A-Z]'
  GROUP BY 1, 2)
SELECT major, round(sum(capped) FILTER (WHERE tax_year = 2022)) AS capped_2022, round(sum(capped) FILTER (WHERE tax_year = 2024)) AS capped_2024,
       round(sum(capped) FILTER (WHERE tax_year = 2024) / sum(capped) FILTER (WHERE tax_year = 2022) - 1, 3) AS change_capped,
       round(sum(raw) FILTER (WHERE tax_year = 2024) / sum(raw) FILTER (WHERE tax_year = 2022) - 1, 3) AS change_raw
FROM g GROUP BY 1 HAVING sum(capped) FILTER (WHERE tax_year = 2022) >= 50000000 ORDER BY change_capped DESC"""


@insight("rise-cause-dollars", "rising-fading", 0.84, cause="R", explore="civil rights and democracy advocacy")
def rise_cause_dollars(ctx):
    rows = q(CAUSE_TREND_SQL)
    for r in rows:
        r["cause"] = MAJOR[r["major"]]
    tot22 = sum(r["capped_2022"] for r in rows)
    tot24 = sum(r["capped_2024"] for r in rows)
    overall = tot24 / tot22 - 1
    top, bot = rows[0], rows[-1]
    d = {r["major"]: r for r in rows}
    q_ = d.get("Q")
    # recount top and bottom with EXISTS on the panel, independent of the grouped query
    v = {}
    for m in (top["major"], bot["major"]):
        v[m] = q1(f"""SELECT sum(least(amount, 5000000)) FILTER (WHERE tax_year = 2024) / sum(least(amount, 5000000)) FILTER (WHERE tax_year = 2022) - 1 AS c
                      FROM grants g WHERE g.tax_year IN (2022, 2024) AND g.amount > 0
                        AND EXISTS (SELECT 1 FROM orgs o WHERE o.ein = g.recipient_ein AND o.ntee_cd LIKE '{m}%' AND o.foundation NOT IN ('02','03','04'))
                        AND EXISTS (SELECT 1 FROM grants a WHERE a.funder_ein = g.funder_ein AND a.tax_year = 2022)
                        AND EXISTS (SELECT 1 FROM grants b WHERE b.funder_ein = g.funder_ein AND b.tax_year = 2024)""")["c"]
    chart = diverging(rows, "cause", "change_capped", "Change in grant dollars, 2024 vs 2022 (same foundations)", rule=overall)
    q_txt = f" and international affairs {pct(q_['change_capped'])}" if q_ and q_ is not bot and q_ is not top else ""
    return {
        "headline": (f"Same foundations, two years apart: grants to {top['cause'].lower()} rose {pct(top['change_capped'])} from 2022 to 2024, "
                     f"while {bot['cause'].lower()} {'fell' if bot['change_capped'] < 0 else 'rose only'} {pct(abs(bot['change_capped']))}{q_txt}"),
        "summary": (f"Across foundations that filed for both years, giving to linked grantees rose {pct(overall)} overall. "
                    "Shifts this large inside a fixed set of funders signal changing priorities, not just new money; advisors can expect more competition in fast-rising causes."),
        "chart": chart,
        "method": ("Foundations with 990-PF grants in both tax years 2022 and 2024; their grants to linked public charities, grouped by the recipient's NTEE major group. "
                   "Each grant capped at $5M so one mega-gift can't drive a cause (uncapped change in the CSV). Causes with $50M+ capped dollars in 2022."),
        "caveats": ctx.link + " Some 2024 returns were still arriving in IRS releases; using the same foundations in both years limits that effect.",
        "sources": [SRC["pf"], SRC["bmf"]],
        "query": CAUSE_TREND_SQL,
        "data": rows,
        "org_eins": None,
        "cause": top["major"],
        "checks": [(f"{m} recount", close(v[m], r["change_capped"], abs_=0.002)) for m, r in ((top["major"], top), (bot["major"], bot))]
                  + [("15+ causes", len(rows) >= 15)],
    }


DISASTER_SQL = """SELECT state, count(*) FILTER (WHERE left(ruling, 4) = '2023') AS new_2023, count(*) FILTER (WHERE left(ruling, 4) = '2025') AS new_2025
FROM orgs WHERE subsection = '03' AND left(ntee_cd, 3) = 'M20' AND left(ruling, 4) IN ('2023', '2025')
GROUP BY 1 ORDER BY new_2025 DESC"""


@insight("rise-disaster-charities", "rising-fading", 0.7, cause="M", explore="disaster recovery nonprofits")
def rise_disaster(ctx):
    rows = q(DISASTER_SQL)
    a = sum(r["new_2023"] for r in rows)
    b = sum(r["new_2025"] for r in rows)
    top = rows[:3]
    v = q1("SELECT count(*) FILTER (WHERE ruling LIKE '2025%') AS b, count(*) FILTER (WHERE ruling LIKE '2023%') AS a FROM orgs WHERE subsection = '03' AND ntee_cd LIKE 'M20%'")
    long = [{"state": r["state"], "year": str(y), "new": r[f"new_{y}"]} for r in rows[:10] for y in (2023, 2025)]
    chart = vbar(long, "state", "new", "State", "New disaster-relief charities", sort=[r["state"] for r in rows[:10]],
                 xoffset="year", color_title="IRS ruling year")
    orgs = q("SELECT ein FROM orgs WHERE subsection = '03' AND ntee_cd LIKE 'M20%' AND ruling LIKE '2025%' ORDER BY coalesce(revenue_amt, 0) DESC LIMIT 60")
    return {
        "headline": f"New disaster-relief charities grew {b / a:.1f}-fold: {n(a)} recognized in 2023, {n(b)} in 2025, led by {', '.join(STATE_NAMES[r['state']] for r in top)}",
        "summary": ("Each major disaster now spins up dozens of new local charities (recent hurricanes, floods and wildfires). "
                    "Most are too new to have filed a 990, so donors should check the IRS list and ask for a budget before giving."),
        "chart": chart,
        "method": "501(c)(3) organizations with NTEE code M20 (disaster preparedness and relief services) on the IRS Business Master File, by state and IRS ruling year 2023 vs 2025. Chart: the 10 states with the most 2025 rulings.",
        "caveats": "Ruling year counts only organizations still listed. NTEE codes are assigned from the IRS application; some relief groups are coded elsewhere (human services, community improvement).",
        "sources": [SRC["bmf"]],
        "query": DISASTER_SQL,
        "data": rows,
        "org_eins": [r["ein"] for r in orgs],
        "checks": [("total recount", int(v["a"]) == a and int(v["b"]) == b), ("n >= 100", a >= 100)],
    }


INTERMEDIARY_SQL = """WITH panel AS (
  SELECT funder_ein FROM grants WHERE tax_year IN (2022, 2024) GROUP BY 1 HAVING count(DISTINCT tax_year) = 2),
g AS (
  SELECT g.tax_year, (o.ntee_cd LIKE 'T%') AS intermediary, sum(g.amount) AS amount
  FROM grants g JOIN panel USING (funder_ein) JOIN orgs o ON o.ein = g.recipient_ein
  WHERE g.tax_year IN (2022, 2024) AND g.amount > 0 AND o.foundation NOT IN ('02', '03', '04')
  GROUP BY 1, 2)
SELECT tax_year, round(sum(amount) FILTER (WHERE intermediary)) AS to_grantmakers, round(sum(amount) FILTER (WHERE NOT intermediary)) AS to_other_charities,
       round(sum(amount) FILTER (WHERE intermediary) / sum(amount), 3) AS grantmaker_share
FROM g GROUP BY 1 ORDER BY 1"""


@insight("rise-grantmaker-intermediaries", "rising-fading", 0.8, cause="T", explore="donor-advised fund sponsors and community foundations")
def rise_intermediaries(ctx):
    rows = q(INTERMEDIARY_SQL)
    a, b = rows[0], rows[-1]
    g_int = b["to_grantmakers"] / a["to_grantmakers"] - 1
    g_oth = b["to_other_charities"] / a["to_other_charities"] - 1
    top = q("""WITH panel AS (SELECT funder_ein FROM grants WHERE tax_year IN (2022, 2024) GROUP BY 1 HAVING count(DISTINCT tax_year) = 2)
               SELECT o.ein, o.name, o.ntee_cd, round(sum(g.amount) FILTER (WHERE g.tax_year = 2024) - coalesce(sum(g.amount) FILTER (WHERE g.tax_year = 2022), 0)) AS change
               FROM grants g JOIN panel USING (funder_ein) JOIN orgs o ON o.ein = g.recipient_ein
               WHERE g.tax_year IN (2022, 2024) AND o.ntee_cd LIKE 'T%' AND o.foundation NOT IN ('02','03','04')
               GROUP BY 1, 2, 3 ORDER BY change DESC LIMIT 60""")
    v = q1("""SELECT sum(g.amount) FILTER (WHERE g.tax_year = 2024) / sum(g.amount) FILTER (WHERE g.tax_year = 2022) - 1 AS c
              FROM grants g WHERE g.tax_year IN (2022, 2024) AND g.amount > 0
                AND g.recipient_ein IN (SELECT ein FROM orgs WHERE ntee_cd LIKE 'T%' AND foundation NOT IN ('02','03','04'))
                AND g.funder_ein IN (SELECT funder_ein FROM grants WHERE tax_year = 2022 INTERSECT SELECT funder_ein FROM grants WHERE tax_year = 2024)""")
    long = [{"year": str(r["tax_year"]), "recipient": k, "amount": r[c]} for r in rows
            for k, c in (("Grantmakers (DAF sponsors, community and public foundations, federations)", "to_grantmakers"), ("All other charities", "to_other_charities"))]
    for x in long:
        x["billions"] = round(x["amount"] / 1e9, 2)
    chart = vbar(long, "year", "billions", "Tax year", "Grant dollars, $ billions (same foundations)", fmt="$,.1f", xoffset="recipient", color_title=None)
    names = ", ".join(t["name"].title() for t in top[:3])
    return {
        "headline": f"Foundation grants to other grantmakers rose {pct(g_int)} from 2022 to 2024, versus {pct(g_oth)} to all other charities",
        "summary": (f"Payments to donor-advised fund sponsors, community and public foundations and federated funds made up {pct(b['grantmaker_share'])} of linked 2024 grant dollars from the same foundations. "
                    f"Largest increases: {names}. Money parked in intermediaries reaches charities later and is harder to trace, which matters for any 'where did the money go' analysis."),
        "chart": chart,
        "method": ("Foundations with 990-PF grants in both tax years 2022 and 2024; their grants to linked public charities (not private foundations), split by whether the recipient's NTEE code is T "
                   "(philanthropy, voluntarism and grantmaking: DAF sponsors, community foundations, public foundations, federated giving)."),
        "caveats": ctx.link + " Some DAF sponsors carry non-T codes (e.g. Fidelity Charitable is coded S), so the intermediary total is a floor.",
        "sources": [SRC["pf"], SRC["bmf"]],
        "query": INTERMEDIARY_SQL,
        "data": rows,
        "org_eins": [t["ein"] for t in top],
        "checks": [("growth recount", close(v["c"], g_int, abs_=0.002)), ("two years", len(rows) == 2)],
    }


# ================================================================ MONEY FLOWS

PAP_SQL = """WITH f AS (
  SELECT funder_ein, sum(amount) AS total, sum(amount) FILTER (WHERE recipient_kind IN ('individual', 'placeholder')) AS to_people
  FROM grants WHERE tax_year BETWEEN 2022 AND 2024 AND amount > 0 GROUP BY 1)
SELECT fu.ein, fu.name, fu.state, round(f.total) AS grants_2022_24
FROM f JOIN funders fu ON fu.ein = f.funder_ein
WHERE fu.name ~* '(PATIENT (ASSISTANCE|FOUNDATION|ACCESS)|\\mCARES (FOUNDATION|NORTH AMERICA)|ACCESS FOUNDATION)'
  AND f.to_people >= 0.8 * f.total AND f.total >= 10000000
ORDER BY f.total DESC"""


@insight("flow-pharma-patient-assistance", "money-flows", 0.93, cause="E", explore="patient assistance programs for prescription drugs")
def flow_pharma(ctx):
    rows = q(PAP_SQL)
    total = q1("SELECT sum(amount) AS s FROM grants WHERE tax_year BETWEEN 2022 AND 2024 AND amount > 0")["s"]
    pap = sum(r["grants_2022_24"] for r in rows)
    eins = ",".join(f"'{r['ein']}'" for r in rows)
    v = q1(f"SELECT sum(amount) AS s FROM grants WHERE funder_ein IN ({eins}) AND tax_year IN (2022, 2023, 2024) AND amount > 0")
    for r in rows:
        r["funder"] = r["name"].title()
    for r in rows:
        r["billions"] = round(r["grants_2022_24"] / 1e9, 2)
    chart = hbar(rows[:15], "funder", "billions", "Reported grants, tax years 2022–24 ($ billions)", fmt="$,.1f")
    return {
        "headline": f"Drug-company patient-assistance foundations reported {money(pap, 0)} in grants for 2022–24, {pct(pap / total)} of all private-foundation grant dollars",
        "summary": (f"{len(rows)} corporate patient-assistance foundations ({', '.join(r['funder'] for r in rows[:4])} and others) give mostly donated medicine to individual patients. "
                    "Any 'total foundation giving' figure that includes them overstates grants to charities by roughly a fifth; strip them out before benchmarking."),
        "chart": chart,
        "method": ("990-PF Part XV grants for tax years 2022–24. Patient-assistance foundations: name says patient assistance, patient foundation or access, or '… Cares' foundation, "
                   "80%+ of grant dollars go to individuals or unitemized lines, and $10M+ in grants. Share = their total over all 990-PF grant dollars in the same years."),
        "caveats": ("Values are as reported by each foundation (donated drugs are usually valued at wholesale prices). Name matching misses some programs "
                    "(e.g. Genzyme's), so the total is a floor."),
        "sources": [SRC["pf"]],
        "query": PAP_SQL,
        "data": [{k: r[k] for k in ("ein", "name", "state", "grants_2022_24")} for r in rows],
        "org_eins": None,
        "checks": [("sum recount", close(v["s"], pap, rel=0.001)), ("8+ programs", len(rows) >= 8)],
    }


OUT_STATE_SQL = f"""WITH {CLEAN},
pop AS (SELECT state, left(min(county_fips), 2)::int AS id FROM county_need WHERE county_fips <> '00000' GROUP BY state)
SELECT g.recipient_state AS state, pop.id, round(sum(g.amount)) AS received,
       round(sum(g.amount) FILTER (WHERE f.state IS DISTINCT FROM g.recipient_state)) AS from_out_of_state,
       round(sum(g.amount) FILTER (WHERE f.state IS DISTINCT FROM g.recipient_state) / sum(g.amount), 3) AS out_of_state_share
FROM clean g JOIN funders f ON f.ein = g.funder_ein JOIN pop ON pop.state = g.recipient_state
GROUP BY 1, 2 ORDER BY out_of_state_share DESC"""


@insight("flow-out-of-state", "money-flows", 0.8, explore="community foundations")
def flow_out_of_state(ctx):
    rows = q(OUT_STATE_SQL)
    for r in rows:
        r["name"] = STATE_NAMES.get(r["state"], r["state"])
    states = [r for r in rows if r["state"] != "DC"]
    hi = states[:3]
    lo = states[-1]
    v = {}
    for r in (hi[0], lo):
        s = r["state"]
        v[s] = q1(f"""SELECT sum(g.amount) FILTER (WHERE f.state <> '{s}' OR f.state IS NULL) / sum(g.amount) AS sh
                      FROM grants g JOIN funders f ON f.ein = g.funder_ein
                      WHERE g.recipient_state = '{s}' AND g.tax_year IN (2022, 2023, 2024) AND g.amount > 0 AND g.recipient_kind IS NULL
                        AND NOT EXISTS (SELECT 1 FROM orgs o WHERE o.ein = g.recipient_ein AND o.foundation IN ('02','03','04'))""")["sh"]
    chart = state_map(rows, "out_of_state_share", "Share of foundation $ from out-of-state funders", fmt=".0%",
                      scale={"scheme": "oranges", "domain": [0.2, 1]},
                      tooltip_extra=[("received", "quantitative", "Received 2022–24", "$,.0f")])
    return {
        "headline": (f"{hi[0]['name']}, {hi[1]['name']} and {hi[2]['name']} charities get {pct(min(h['out_of_state_share'] for h in hi))}+ of their foundation dollars from out-of-state funders; "
                     f"{lo['name']} charities get {pct(lo['out_of_state_share'])}"),
        "summary": ("Small-population states depend on foundations based elsewhere, whose priorities can shift without local accountability. "
                    f"States with big home-grown foundations ({', '.join(r['name'] for r in states[-4:])}) fund their own. Advisors in import-dependent states should map who funds locally before advising a gift."),
        "chart": chart,
        "method": ("2022–24 990-PF grants to US organizations (no individuals, unitemized lines, foreign or private-foundation recipients), by recipient state; "
                   "out-of-state = the foundation's filing address is in another state. DC (97%) is off the color scale."),
        "caveats": ctx.addr + " A foundation's filing address can be its bank trustee's or family office's, not where the family lives.",
        "sources": [SRC["pf"]],
        "query": OUT_STATE_SQL,
        "data": rows,
        "org_eins": None,
        "checks": [(f"{s} recount", close(v[s], r["out_of_state_share"], abs_=0.002)) for s, r in ((hi[0]["state"], hi[0]), (lo["state"], lo))]
                  + [("50 states", len(rows) >= 50)],
    }


NET_SQL = f"""WITH {CLEAN},
g AS (SELECT f.state AS funder_state, c.recipient_state, c.amount FROM clean c JOIN funders f ON f.ein = c.funder_ein),
o AS (SELECT funder_state AS state, sum(amount) AS given FROM g GROUP BY 1),
i AS (SELECT recipient_state AS state, sum(amount) AS received FROM g GROUP BY 1)
SELECT state, round(o.given) AS given, round(i.received) AS received, round(i.received - o.given) AS net
FROM o JOIN i USING (state) WHERE state IN (SELECT state FROM county_need) ORDER BY net"""


@insight("flow-net-exporters", "money-flows", 0.6, explore="national foundations")
def flow_net(ctx):
    rows = q(NET_SQL)
    for r in rows:
        r["name"] = STATE_NAMES.get(r["state"], r["state"])
    exp, imp = rows[0], rows[-1]
    imp2 = [r for r in rows if r["state"] != "DC"][-1]
    v = q1(f"""WITH {CLEAN} SELECT (SELECT sum(amount) FROM clean WHERE recipient_state = '{exp['state']}')
                 - (SELECT sum(c.amount) FROM clean c WHERE c.funder_ein IN (SELECT ein FROM funders WHERE state = '{exp['state']}')) AS net""")
    shown = rows[:8] + rows[-8:]
    for r in rows:
        r["net_billions"] = round(r["net"] / 1e9, 2)
    chart = diverging(shown, "name", "net_billions", "Received minus given by the state's foundations, 2022–24 ($ billions)", fmt="$,.1f")
    return {
        "headline": f"{exp['name']}'s foundations gave {money(-exp['net'])} more than its charities received in 2022–24; {imp['name']} took in {money(imp['net'])} more than its funders gave",
        "summary": (f"{exp['name']} is the biggest net exporter of foundation money (the Gates Foundation is there); {imp['name']} and {imp2['name']} are the biggest importers, "
                    "as headquarters of national and policy groups. Net flows show which places set the agenda for philanthropy elsewhere."),
        "chart": chart,
        "method": ("2022–24 990-PF grants to US organizations (no individuals, unitemized lines, foreign or private-foundation recipients). Given = grants by foundations filing from the state; "
                   "received = grants to recipients addressed in the state. Chart: 8 biggest exporters and importers."),
        "caveats": ctx.addr,
        "sources": [SRC["pf"]],
        "query": NET_SQL,
        "data": rows,
        "org_eins": None,
        "checks": [("exporter recount", close(v["net"], exp["net"], rel=0.005))],
    }


CONC_SQL = """WITH r AS (
  SELECT g.recipient_ein, left(o.ntee_cd, 1) AS major, sum(g.amount) AS amount
  FROM grants g JOIN orgs o ON o.ein = g.recipient_ein
  WHERE g.tax_year BETWEEN 2022 AND 2024 AND g.amount > 0 AND o.foundation NOT IN ('02', '03', '04') AND o.ntee_cd ~ '^[A-Z]'
  GROUP BY 1, 2),
k AS (SELECT *, row_number() OVER (PARTITION BY major ORDER BY amount DESC) AS rk, count(*) OVER (PARTITION BY major) AS grantees FROM r)
SELECT major, grantees, ceil(grantees * 0.01)::int AS top_1pct_orgs, round(sum(amount)) AS amount,
       round(sum(amount) FILTER (WHERE rk <= ceil(grantees * 0.01)) / sum(amount), 3) AS top_1pct_share,
       round(sum(amount) FILTER (WHERE rk <= ceil(grantees * 0.10)) / sum(amount), 3) AS top_10pct_share
FROM k GROUP BY 1, 2 HAVING grantees >= 1000 ORDER BY top_1pct_share DESC"""


@insight("flow-concentration", "money-flows", 0.78, cause="B", explore="universities")
def flow_concentration(ctx):
    rows = q(CONC_SQL)
    for r in rows:
        r["cause"] = MAJOR[r["major"]]
    d = {r["major"]: r for r in rows}
    b, p = d["B"], d["P"]
    v = q1("""WITH r AS (SELECT g.recipient_ein, sum(g.amount) AS a FROM grants g JOIN orgs o ON o.ein = g.recipient_ein
                         WHERE g.tax_year IN (2022, 2023, 2024) AND g.amount > 0 AND o.ntee_cd LIKE 'B%' AND o.foundation NOT IN ('02','03','04') GROUP BY 1),
                  t AS (SELECT a, ntile(100) OVER (ORDER BY a DESC) AS pctile FROM r)
              SELECT sum(a) FILTER (WHERE pctile = 1) / sum(a) AS sh FROM t""")
    top = q("""SELECT g.recipient_ein AS ein FROM grants g JOIN orgs o ON o.ein = g.recipient_ein
               WHERE g.tax_year BETWEEN 2022 AND 2024 AND o.ntee_cd LIKE 'B%' AND o.foundation NOT IN ('02','03','04')
               GROUP BY 1 ORDER BY sum(g.amount) DESC LIMIT 60""")
    chart = hbar(rows, "cause", "top_1pct_share", "Share of the cause's foundation dollars going to its top 1% of grantees",
                 highlight=("major", "B"), tooltip_extra=[("grantees", "quantitative", ","), ("top_1pct_orgs", "quantitative", ",")])
    return {
        "headline": (f"In education, {n(b['top_1pct_orgs'])} organizations (the top 1% of grantees) took {pct(b['top_1pct_share'])} of foundation dollars in 2022–24; "
                     f"in human services the top 1% took {pct(p['top_1pct_share'])}"),
        "summary": (f"Foundation money in {', '.join(r['cause'].lower() for r in rows[:3])} pools at a few large institutions; "
                    f"{', '.join(r['cause'].lower() for r in rows[-3:])} are far more evenly spread. A donor seeking leverage in a top-heavy cause should look below the top 1%."),
        "chart": chart,
        "method": ("990-PF grants for tax years 2022–24 linked to a public charity, summed per recipient and grouped by NTEE major group (causes with 1,000+ grantees). "
                   "Top 1% = the highest-funded 1% of grantees in that cause (rounded up)."),
        "caveats": ctx.link + " Large universities and hospitals are more likely to be matched than small groups, which pushes top shares up somewhat.",
        "sources": [SRC["pf"], SRC["bmf"]],
        "query": CONC_SQL,
        "data": rows,
        "org_eins": [t["ein"] for t in top],
        "checks": [("education recount (ntile)", close(v["sh"], b["top_1pct_share"], abs_=0.01)), ("education n >= 1,000", b["grantees"] >= 1000)],
    }


PF_TRANSFER_SQL = """SELECT f.ein AS funder_ein, f.name AS funder, o.ein AS recipient_ein, o.name AS recipient, round(sum(g.amount)) AS amount
FROM grants g JOIN orgs o ON o.ein = g.recipient_ein JOIN funders f ON f.ein = g.funder_ein
WHERE g.tax_year BETWEEN 2022 AND 2024 AND g.amount > 0 AND o.foundation IN ('02', '03', '04')
GROUP BY 1, 2, 3, 4 ORDER BY amount DESC"""


@insight("flow-foundation-to-foundation", "money-flows", 0.82, cause="T", explore="private family foundations")
def flow_pf_transfers(ctx):
    rows = q(PF_TRANSFER_SQL)
    tot = sum(r["amount"] for r in rows)
    linked = q1("SELECT sum(amount) AS s FROM grants WHERE tax_year BETWEEN 2022 AND 2024 AND amount > 0 AND recipient_ein IS NOT NULL")["s"]
    top = rows[0]
    v = q1("""SELECT sum(g.amount) AS s, count(DISTINCT g.funder_ein) AS funders FROM grants g
              WHERE g.tax_year IN (2022, 2023, 2024) AND g.amount > 0
                AND g.recipient_ein IN (SELECT ein FROM orgs WHERE foundation IN ('02','03','04'))""")
    funders = len({r["funder_ein"] for r in rows})
    for r in rows:
        r["pair"] = f"{r['funder'].title()} → {r['recipient'].title()}"
    for r in rows[:12]:
        r["billions"] = round(r["amount"] / 1e9, 2)
    chart = hbar(rows[:12], "pair", "billions", "Grants 2022–24 ($ billions)", fmt="$,.1f")
    return {
        "headline": (f"{money(tot)} ({pct(tot / linked)}) of linked foundation grant dollars in 2022–24 went to another private foundation; "
                     f"{money(top['amount'])} was {top['funder'].title()} paying {top['recipient'].title()}"),
        "summary": (f"{n(funders)} foundations made grants to other private foundations, mostly within the same family (a trust funding its operating foundation). "
                    "These transfers count as giving on paper but don't reach charities until the second foundation pays out; exclude them from flow totals to avoid double counting."),
        "chart": chart,
        "method": "990-PF grants for tax years 2022–24 whose linked recipient is itself a private foundation (IRS foundation codes 02–04). Share = over all grant dollars linked to any organization.",
        "caveats": ctx.link + " Unlinked transfers aren't counted, so the total is a floor.",
        "sources": [SRC["pf"], SRC["bmf"]],
        "query": PF_TRANSFER_SQL,
        "data": [{k: r[k] for k in ("funder_ein", "funder", "recipient_ein", "recipient", "amount")} for r in rows[:500]],
        "org_eins": list(dict.fromkeys(r["recipient_ein"] for r in rows[:60])),
        "checks": [("total recount", close(v["s"], tot, rel=0.001)), ("funder count", int(v["funders"]) == funders)],
    }


# ================================================================ TRACK RECORD

INVITE_SQL = f"""SELECT {BAND.format(x='assets')} AS band, count(*) AS foundations,
       round(avg(invite_only::int), 3) AS invite_only_share,
       round(sum(grants_paid) FILTER (WHERE invite_only) / nullif(sum(grants_paid), 0), 3) AS invite_only_dollar_share
FROM funders WHERE assets > 0 AND invite_only IS NOT NULL GROUP BY 1 ORDER BY 1"""


@insight("track-invite-only", "track-record", 0.72, explore="foundations that accept unsolicited grant applications")
def track_invite(ctx):
    rows = q(INVITE_SQL)
    for r in rows:
        r["band"] = strip_band(r["band"])
    allr = q1("""SELECT count(*) AS n, avg(invite_only::int) AS share, sum(grants_paid) FILTER (WHERE invite_only) / sum(grants_paid) AS dollars
                 FROM funders WHERE invite_only IS NOT NULL""")
    v = q1("SELECT count(*) FILTER (WHERE invite_only)::float / count(*) AS share FROM funders WHERE invite_only IS NOT NULL")
    big = rows[-1]
    mid = rows[1]
    chart = vbar(rows, "band", "invite_only_share", "Foundation assets (fair market value)", "Share that won't take unsolicited applications",
                 fmt=".0%", label_fmt=".0%")
    return {
        "headline": f"{pct(allr['share'])} of US private foundations say they don't accept unsolicited applications; they pay {pct(allr['dollars'])} of foundation grant dollars",
        "summary": (f"Closed doors are most common at $1M–10M family foundations ({pct(mid['invite_only_share'])}); among $1B+ foundations it is {pct(big['invite_only_share'])}. "
                    "For nonprofits, relationships (often through advisors and board members) are the main way in to most foundation money."),
        "chart": chart,
        "method": ("Newest 990-PF per foundation (mostly tax years 2023–24): Part XV line 2 box 'only makes contributions to preselected charitable organizations and does not accept unsolicited requests'. "
                   "Dollar share = grants paid in that return."),
        "caveats": "Self-reported checkbox; some invite-only foundations still read letters of inquiry, and some open ones fund mostly the same grantees.",
        "sources": [SRC["pf"]],
        "query": INVITE_SQL,
        "data": rows,
        "org_eins": None,
        "checks": [("overall recount", close(v["share"], allr["share"], abs_=0.001)), ("100K+ foundations", allr["n"] >= 100000)],
    }


GOS_SQL = f"""WITH g AS (
  SELECT g.funder_ein, g.amount, g.purpose ~* '(general|operating|unrestricted|annual fund|core support)' AS general
  FROM grants g WHERE g.tax_year BETWEEN 2022 AND 2024 AND g.amount > 0 AND g.recipient_kind IS NULL)
SELECT {BAND.format(x='f.assets')} AS band, count(DISTINCT g.funder_ein) AS foundations, count(*) AS grants,
       round(avg(general::int), 3) AS general_share_of_grants,
       round(sum(amount) FILTER (WHERE general) / sum(amount), 3) AS general_share_of_dollars
FROM g JOIN funders f ON f.ein = g.funder_ein WHERE f.assets > 0 GROUP BY 1 ORDER BY 1"""


@insight("track-general-support", "track-record", 0.8, explore="general operating support grants")
def track_gos(ctx):
    rows = q(GOS_SQL)
    for r in rows:
        r["band"] = strip_band(r["band"])
    big, mid = rows[-1], rows[1]
    v = q1("""SELECT sum(g.amount) FILTER (WHERE g.purpose ~* '(general|operating|unrestricted|annual fund|core support)') / sum(g.amount) AS sh
              FROM grants g WHERE g.funder_ein IN (SELECT ein FROM funders WHERE assets >= 1e9)
                AND g.tax_year IN (2022, 2023, 2024) AND g.amount > 0 AND g.recipient_kind IS NULL""")
    long = [{"band": r["band"], "measure": m, "share": r[k]} for r in rows
            for m, k in (("Share of grants", "general_share_of_grants"), ("Share of dollars", "general_share_of_dollars"))]
    chart = vbar(long, "band", "share", "Foundation assets", "Labeled general or operating support", fmt=".0%", xoffset="measure", color_title=None)
    return {
        "headline": f"$1B+ foundations label only {pct(big['general_share_of_dollars'])} of their grant dollars as general operating support, versus {pct(mid['general_share_of_dollars'])} at $1M–10M foundations",
        "summary": (f"Big foundations make plenty of small unrestricted grants ({pct(big['general_share_of_grants'])} of their grants), but their large grants are restricted to projects. "
                    "Unrestricted money is what lets nonprofits cover overhead, so for operating needs, mid-size family foundations are the more likely source."),
        "chart": chart,
        "method": ("990-PF Part XV grants for tax years 2022–24 to organizations (no individuals, unitemized lines or foreign recipients). General support = the purpose text mentions general, "
                   "operating, unrestricted, annual fund or core support. Foundation size = fair market value of assets on the newest return."),
        "caveats": "Purpose lines are free text; grants described as 'charitable' or left vague may also be unrestricted, so all shares are floors.",
        "sources": [SRC["pf"]],
        "query": GOS_SQL,
        "data": rows,
        "org_eins": None,
        "checks": [("$1B+ recount", close(v["sh"], big["general_share_of_dollars"], abs_=0.002)), ("$1B+ has 100+ foundations", big["foundations"] >= 100)],
    }


PAYOUT_SQL = f"""SELECT {BAND.format(x='assets')} AS band, count(*) AS foundations,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY coalesce(grants_paid, 0)::float / assets))::numeric, 4) AS median_grants_to_assets,
       round(avg((coalesce(grants_paid, 0)::float / assets < 0.05)::int), 3) AS share_under_5pct,
       round(avg((coalesce(grants_paid, 0) = 0)::int), 3) AS share_paid_nothing
FROM funders WHERE assets >= 100000 GROUP BY 1 ORDER BY 1"""


@insight("track-payout", "track-record", 0.66, explore="private foundations")
def track_payout(ctx):
    rows = q(PAYOUT_SQL)
    for r in rows:
        r["band"] = strip_band(r["band"])
    big, small = rows[-1], rows[0]
    v = q1("SELECT avg((coalesce(grants_paid, 0)::float / assets < 0.05)::int) AS sh, count(*) AS n FROM funders WHERE assets >= 1e9")
    chart = vbar(rows, "band", "share_under_5pct", "Foundation assets", "Share whose grants were under 5% of assets", fmt=".0%", label_fmt=".0%")
    return {
        "headline": f"{pct(big['share_under_5pct'])} of $1B+ foundations paid grants worth less than 5% of their assets in their latest year; the median paid {pct(big['median_grants_to_assets'], 1)}",
        "summary": (f"The legal 5% payout rule counts administrative costs and uses prior-year average assets, so this isn't a compliance test. But it shows that the largest foundations "
                    f"grant a smaller slice of their wealth (median {pct(big['median_grants_to_assets'], 1)}) than small ones ({pct(small['median_grants_to_assets'], 1)}), "
                    "a useful benchmark when advising a family on its own payout policy."),
        "chart": chart,
        "method": "Newest 990-PF per foundation with $100K+ assets: total grants paid (Part XV) divided by end-of-year fair market value of assets. Grouped by asset size.",
        "caveats": "Qualifying distributions also include administrative and program costs, and the 5% minimum applies to prior-year average net investment assets; a strong market year raises end-of-year assets.",
        "sources": [SRC["pf"]],
        "query": PAYOUT_SQL,
        "data": rows,
        "org_eins": None,
        "checks": [("$1B+ recount", close(v["sh"], big["share_under_5pct"], abs_=0.002) and int(v["n"]) == int(big["foundations"])), ("$1B+ n >= 100", big["foundations"] >= 100)],
    }


ONE_GRANTEE_SQL = f"""WITH fr AS (
  SELECT funder_ein, coalesce(recipient_ein, upper(recipient_name) || '|' || coalesce(recipient_state, '')) AS recipient, sum(amount) AS amount
  FROM grants WHERE tax_year BETWEEN 2022 AND 2024 AND amount > 0 AND recipient_kind IS NULL GROUP BY 1, 2),
fs AS (SELECT funder_ein, sum(amount) AS total, max(amount) AS largest, count(*) AS recipients FROM fr GROUP BY 1)
SELECT {BAND.format(x='f.assets')} AS band, count(*) AS foundations,
       round(avg((largest > 0.5 * total)::int), 3) AS share_half_to_one_grantee,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY recipients) AS median_grantees
FROM fs JOIN funders f ON f.ein = fs.funder_ein WHERE f.assets > 0 GROUP BY 1 ORDER BY 1"""


@insight("track-one-grantee", "track-record", 0.7, explore="supporting foundations")
def track_one_grantee(ctx):
    rows = q(ONE_GRANTEE_SQL)
    for r in rows:
        r["band"] = strip_band(r["band"])
    big = rows[3]
    small = rows[0]
    v = q1("""WITH fr AS (SELECT g.funder_ein, coalesce(g.recipient_ein, upper(g.recipient_name) || '|' || coalesce(g.recipient_state, '')) AS r, sum(g.amount) AS a
                         FROM grants g JOIN funders f ON f.ein = g.funder_ein
                         WHERE f.assets >= 1e8 AND f.assets < 1e9 AND g.tax_year IN (2022, 2023, 2024) AND g.amount > 0 AND g.recipient_kind IS NULL GROUP BY 1, 2),
                  rk AS (SELECT funder_ein, a, sum(a) OVER (PARTITION BY funder_ein) AS t, rank() OVER (PARTITION BY funder_ein ORDER BY a DESC, r) AS k FROM fr)
              SELECT avg((a > 0.5 * t)::int) AS sh FROM rk WHERE k = 1""")
    chart = vbar(rows, "band", "share_half_to_one_grantee", "Foundation assets", "Share giving over half its dollars to one grantee", fmt=".0%", label_fmt=".0%")
    return {
        "headline": f"{pct(big['share_half_to_one_grantee'])} of $100M–1B foundations gave more than half of their 2022–24 grant dollars to a single organization",
        "summary": (f"Many sizable foundations are effectively endowments for one university, hospital or family charity; among foundations under $1M it is {pct(small['share_half_to_one_grantee'])}. "
                    "Their asset size overstates how much is open to other applicants, so check a funder's grant list, not just its assets."),
        "chart": chart,
        "method": ("990-PF grants for tax years 2022–24 to organizations (no individuals, unitemized lines or foreign recipients), summed per foundation and recipient "
                   "(recipient = linked EIN, else name + state). Size = assets on the newest return."),
        "caveats": "Unlinked recipients spelled differently across years count as separate grantees, which can only lower the share.",
        "sources": [SRC["pf"]],
        "query": ONE_GRANTEE_SQL,
        "data": rows,
        "org_eins": None,
        "checks": [("$100M–1B recount", close(v["sh"], big["share_half_to_one_grantee"], abs_=0.003)), ("n >= 1,000", big["foundations"] >= 1000)],
    }


AUDIT_SQL = f"""WITH {F990}
SELECT {REV_BAND.format(x='f.cy_revenue')} AS band, count(*) AS charities,
       round(avg(f.audited::int), 3) AS audited, round(avg(f.conflict_policy::int), 3) AS conflict_policy,
       round(avg(f.whistleblower_policy::int), 3) AS whistleblower_policy
FROM f JOIN orgs o USING (ein) WHERE f.cy_revenue > 0 AND o.subsection = '03' GROUP BY 1 ORDER BY 1"""


@insight("track-audit-gap", "track-record", 0.64, explore="nonprofits with audited financial statements")
def track_audit(ctx):
    rows = q(AUDIT_SQL)
    for r in rows:
        r["band"] = strip_band(r["band"])
    mid = rows[1]
    v = q1("""SELECT avg(audited::int) AS a FROM (SELECT DISTINCT ON (ein) ein, audited, cy_revenue FROM filing_flag WHERE form = '990' ORDER BY ein, tax_year DESC, object_id DESC) x
              WHERE cy_revenue >= 5e5 AND cy_revenue < 2e6 AND ein IN (SELECT ein FROM orgs WHERE subsection = '03')""")
    long = [{"band": r["band"], "practice": p, "share": r[k]} for r in rows
            for p, k in (("Audited financials", "audited"), ("Conflict-of-interest policy", "conflict_policy"), ("Whistleblower policy", "whistleblower_policy"))]
    chart = vbar(long, "band", "share", "Charity revenue (newest 990)", "Share of charities", fmt=".0%", xoffset="practice", color_title=None)
    return {
        "headline": f"Only {pct(mid['audited'])} of charities with $500K–2M in revenue report audited financial statements; {pct(mid['whistleblower_policy'])} have a whistleblower policy",
        "summary": ("Below about $2M in revenue, most charities have no independent audit, so donors rely on the 990 and their own diligence. "
                    "Ask for a review or compilation instead of an audit rather than ruling small groups out."),
        "chart": chart,
        "method": "Newest full Form 990 per 501(c)(3): Part XII line 2b (financial statements audited) and Part VI lines 12a and 13 (policies), grouped by current-year revenue.",
        "caveats": "990-EZ filers (under $200K revenue) don't answer these questions and aren't included. State law requires audits above thresholds that vary by state.",
        "sources": [SRC["990"]],
        "query": AUDIT_SQL,
        "data": rows,
        "org_eins": None,
        "checks": [("$500K–2M recount", close(v["a"], mid["audited"], abs_=0.002)), ("n >= 10,000", mid["charities"] >= 10000)],
    }


RENEW_SQL = """WITH p AS (SELECT DISTINCT funder_ein, recipient_ein, tax_year FROM grants
           WHERE recipient_ein IS NOT NULL AND tax_year BETWEEN 2022 AND 2024 AND amount > 0),
active AS (SELECT funder_ein FROM p WHERE tax_year = 2024 GROUP BY 1),
a AS (SELECT p.funder_ein, p.recipient_ein,
             EXISTS (SELECT 1 FROM p q WHERE q.funder_ein = p.funder_ein AND q.recipient_ein = p.recipient_ein AND q.tax_year IN (2023, 2024)) AS renewed
      FROM p JOIN active USING (funder_ein) WHERE p.tax_year = 2022)
SELECT left(o.ntee_cd, 1) AS major, count(*) AS pairs, round(avg(renewed::int), 3) AS renewal_rate
FROM a JOIN orgs o ON o.ein = a.recipient_ein WHERE o.ntee_cd ~ '^[A-Z]'
GROUP BY 1 HAVING count(*) >= 2000 ORDER BY renewal_rate DESC"""


@insight("track-renewal", "track-record", 0.55, explore="environmental conservation nonprofits")
def track_renewal(ctx):
    rows = q(RENEW_SQL)
    for r in rows:
        r["cause"] = MAJOR[r["major"]]
    # independent recount: one row per foundation-grantee pair with flags for the years it appears
    allr = q1("""WITH pr AS (SELECT funder_ein, recipient_ein, bool_or(tax_year = 2022) AS y22, bool_or(tax_year IN (2023, 2024)) AS later
                             FROM grants WHERE recipient_ein IS NOT NULL AND tax_year BETWEEN 2022 AND 2024 AND amount > 0 GROUP BY 1, 2),
                      act AS (SELECT DISTINCT funder_ein FROM grants WHERE tax_year = 2024 AND recipient_ein IS NOT NULL AND amount > 0)
                 SELECT avg(later::int) AS r FROM pr JOIN act USING (funder_ein) WHERE y22""")
    hi, lo = rows[0], rows[-1]
    tot_pairs = sum(r["pairs"] for r in rows)
    weighted = sum(r["pairs"] * r["renewal_rate"] for r in rows) / tot_pairs
    chart = hbar(rows, "cause", "renewal_rate", "Share of 2022 grantees funded again by the same foundation in 2023–24",
                 rule=float(allr["r"]), tooltip_extra=[("pairs", "quantitative", ",")])
    return {
        "headline": f"{pct(allr['r'])} of 2022 foundation grantees were funded again by the same foundation within two years; {hi['cause'].lower()} {pct(hi['renewal_rate'])}, {lo['cause'].lower()} {pct(lo['renewal_rate'])}",
        "summary": ("Foundation relationships are sticky: most grantees are repeat bets. "
                    "A first grant is the hard part, and fields with lower renewal see more one-off, event or capital gifts."),
        "chart": chart,
        "method": ("Pairs of foundation and linked grantee with a grant in tax year 2022, among foundations that also filed grants for 2024; renewed = another grant to the same EIN in 2023 or 2024. "
                   "Grouped by the grantee's NTEE major group (2,000+ pairs)."),
        "caveats": ctx.link + " Multi-year grants paid up front in 2022 show as not renewed.",
        "sources": [SRC["pf"], SRC["bmf"]],
        "query": RENEW_SQL,
        "data": rows,
        "org_eins": None,
        "checks": [("weighted mean matches overall (±3 pts)", close(weighted, allr["r"], abs_=0.03)), ("pairs >= 100K", tot_pairs >= 100000)],
    }


# ================================================================ AFFILIATIONS

BOARD_SIZE_SQL = f"""WITH {BOARD_PAIRS},
g AS (SELECT g.funder_ein, g.amount, (pp.f IS NOT NULL) AS board_linked FROM grants g
      LEFT JOIN pp ON pp.f = g.funder_ein AND pp.r = g.recipient_ein
      WHERE g.tax_year BETWEEN 2022 AND 2024 AND g.amount > 0 AND g.recipient_ein IS NOT NULL),
fs AS (SELECT funder_ein, sum(amount) AS total, coalesce(sum(amount) FILTER (WHERE board_linked), 0) AS linked FROM g GROUP BY 1)
SELECT {BAND.format(x='f.assets')} AS band, count(*) AS foundations, count(*) FILTER (WHERE linked > 0) AS with_board_linked_grantee,
       round(avg((linked > 0)::int), 3) AS share_with_board_linked_grantee,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY linked / total) FILTER (WHERE linked > 0))::numeric, 3) AS median_share_of_giving_when_linked,
       round(sum(linked) / sum(total), 3) AS dollar_share
FROM fs JOIN funders f ON f.ein = fs.funder_ein WHERE f.assets > 0 GROUP BY 1 ORDER BY 1"""


@insight("aff-board-linked-giving", "affiliations", 0.86, explore="family foundation board members")
def aff_board_size(ctx):
    rows = q(BOARD_SIZE_SQL)
    for r in rows:
        r["band"] = strip_band(r["band"])
    big, small = rows[-1], rows[0]
    big2 = rows[3]
    tot = q1(f"""WITH {BOARD_PAIRS}
        SELECT count(DISTINCT g.funder_ein) AS funders, count(DISTINCT (g.funder_ein, g.recipient_ein)) AS pairs, sum(g.amount) AS amount
        FROM grants g JOIN pp ON pp.f = g.funder_ein AND pp.r = g.recipient_ein WHERE g.tax_year BETWEEN 2022 AND 2024 AND g.amount > 0""")
    v = q1("""SELECT count(DISTINCT funder_ein) AS funders FROM (
                SELECT g.funder_ein FROM grants g JOIN board_interlocks b ON b.ein_a = g.funder_ein AND b.ein_b = g.recipient_ein
                WHERE b.relation = 'link' AND b.confidence = 'high' AND b.kind = 'funder-grantee' AND g.tax_year IN (2022, 2023, 2024) AND g.amount > 0
                UNION ALL
                SELECT g.funder_ein FROM grants g JOIN board_interlocks b ON b.ein_b = g.funder_ein AND b.ein_a = g.recipient_ein
                WHERE b.relation = 'link' AND b.confidence = 'high' AND b.kind = 'funder-grantee' AND g.tax_year IN (2022, 2023, 2024) AND g.amount > 0) x""")
    long = [{"band": r["band"], "measure": m, "share": r[k]} for r in rows
            for m, k in (("Foundations paying a board-linked org", "share_with_board_linked_grantee"),
                         ("Median share of their giving to such orgs", "median_share_of_giving_when_linked"))]
    chart = vbar(long, "band", "share", "Foundation assets", "Share", fmt=".0%", xoffset="measure", color_title=None)
    return {
        "headline": (f"{pct(big['share_with_board_linked_grantee'])} of $1B+ foundations paid an organization that shares their board members in 2022–24; "
                     f"small foundations with such ties sent a median {pct(small['median_share_of_giving_when_linked'])} of their giving to those organizations"),
        "summary": (f"{n(tot['funders'])} foundations made {money(tot['amount'])} in grants to {n(tot['pairs'])} organizations whose boards they share, excluding sister organizations and bank trustees. "
                    "That is legal and often intended, but for a small foundation it can mean most money stays within the board's own circle; worth asking about in diligence."),
        "chart": chart,
        "method": ("High-confidence hidden board links (generosity-data board_interlocks: relation = 'link', confidence = 'high', kind = 'funder-grantee'): a foundation and a grantee in the same state "
                   "list the same person by first and last name, outside sister organizations and bank trusts. Grants for tax years 2022–24; size = foundation assets on the newest return."),
        "caveats": ("Names are matched within a state, so a common name can create a false link (graded ~100% correct at high confidence). "
                    "Board lists come from each organization's newest return; large nonprofit boards are cut at 25 people. " + ctx.link),
        "sources": [SRC["pf"], SRC["board"]],
        "query": BOARD_SIZE_SQL,
        "data": rows,
        "org_eins": None,
        "checks": [("funder count recount", int(v["funders"]) == int(tot["funders"])),
                   ("band sum = total", sum(r["with_board_linked_grantee"] for r in rows) <= int(tot["funders"])),
                   ("$1B+ n >= 100", big["foundations"] >= 100)],
    }


BOARD_CAUSE_SQL = f"""WITH {BOARD_PAIRS}
SELECT left(o.ntee_cd, 1) AS major, round(sum(g.amount)) AS foundation_dollars,
       round(coalesce(sum(g.amount) FILTER (WHERE pp.f IS NOT NULL), 0)) AS board_linked_dollars,
       count(DISTINCT g.recipient_ein) FILTER (WHERE pp.f IS NOT NULL) AS board_linked_grantees,
       round(coalesce(sum(g.amount) FILTER (WHERE pp.f IS NOT NULL), 0) / sum(g.amount), 4) AS board_linked_share
FROM grants g JOIN orgs o ON o.ein = g.recipient_ein LEFT JOIN pp ON pp.f = g.funder_ein AND pp.r = g.recipient_ein
WHERE g.tax_year BETWEEN 2022 AND 2024 AND g.amount > 0 AND o.ntee_cd ~ '^[A-Z]' AND o.foundation NOT IN ('02', '03', '04')
GROUP BY 1 HAVING sum(g.amount) >= 500000000 ORDER BY board_linked_share DESC"""


@insight("aff-board-linked-causes", "affiliations", 0.74, cause="N", explore="youth sports and recreation programs")
def aff_board_cause(ctx):
    rows = q(BOARD_CAUSE_SQL)
    for r in rows:
        r["cause"] = MAJOR[r["major"]]
    allshare = sum(r["board_linked_dollars"] for r in rows) / sum(r["foundation_dollars"] for r in rows)
    top = rows[0]
    v = q1(f"""WITH g AS (SELECT g.* FROM grants g JOIN orgs o ON o.ein = g.recipient_ein
                          WHERE g.tax_year IN (2022, 2023, 2024) AND g.amount > 0 AND o.ntee_cd LIKE '{top['major']}%' AND o.foundation NOT IN ('02','03','04')),
                    b AS (SELECT * FROM board_interlocks WHERE relation = 'link' AND confidence = 'high' AND kind = 'funder-grantee')
               SELECT ((SELECT coalesce(sum(g.amount), 0) FROM g JOIN b ON b.ein_a = g.funder_ein AND b.ein_b = g.recipient_ein)
                     + (SELECT coalesce(sum(g.amount), 0) FROM g JOIN b ON b.ein_b = g.funder_ein AND b.ein_a = g.recipient_ein))
                     / (SELECT sum(amount) FROM g) AS sh""")
    orgs = q(f"""WITH {BOARD_PAIRS} SELECT g.recipient_ein AS ein FROM grants g JOIN pp ON pp.f = g.funder_ein AND pp.r = g.recipient_ein
                 JOIN orgs o ON o.ein = g.recipient_ein WHERE g.tax_year BETWEEN 2022 AND 2024 AND o.ntee_cd LIKE '{top['major']}%'
                 GROUP BY 1 ORDER BY sum(g.amount) DESC LIMIT 60""")
    chart = hbar(rows, "cause", "board_linked_share", "Share of foundation $ from funders sharing board members with the grantee",
                 fmt=".1%", highlight=("major", top["major"]), rule=allshare,
                 tooltip_extra=[("board_linked_dollars", "quantitative", "$,.0f"), ("board_linked_grantees", "quantitative", ",")])
    return {
        "headline": f"{top['cause']} charities get {pct(top['board_linked_share'], 1)} of their foundation dollars from funders that share board members with them, {top['board_linked_share'] / allshare:.1f} times the {pct(allshare, 1)} average",
        "summary": (f"{top['cause']} and {rows[1]['cause'].lower()} charities are disproportionately funded by their own board members' foundations. "
                    "For an advisor, a grantee whose foundation support comes mostly from board-linked funders has a narrower, more fragile base than its revenue suggests."),
        "chart": chart,
        "method": ("990-PF grants for tax years 2022–24 linked to public charities, by the recipient's NTEE major group (causes with $500M+). Board-linked = a high-confidence hidden board link "
                   "between funder and grantee in board_interlocks (relation 'link', confidence 'high'; sister organizations and bank trustees excluded). Dashed line = all causes."),
        "caveats": "Same-state, name-based matching of board members on each organization's newest return. " + ctx.link,
        "sources": [SRC["pf"], SRC["board"]],
        "query": BOARD_CAUSE_SQL,
        "data": rows,
        "org_eins": [o["ein"] for o in orgs],
        "cause": top["major"],
        "checks": [("top cause recount", close(v["sh"], top["board_linked_share"], abs_=0.0005)), ("20+ grantees", top["board_linked_grantees"] >= 20)],
    }


INSIDER_SQL = f"""WITH {F990}
SELECT {REV_BAND.format(x='f.cy_revenue')} AS band, count(*) AS charities,
       round(avg(f.business_with_insiders::int), 3) AS business_with_insiders,
       round(avg(f.family_business_ties::int), 3) AS family_or_business_ties,
       round(avg(f.insider_loan::int), 3) AS loans_with_insiders
FROM f JOIN orgs o USING (ein) WHERE f.cy_revenue > 0 AND o.subsection = '03' GROUP BY 1 ORDER BY 1"""


@insight("aff-insider-transactions", "affiliations", 0.68, explore="large nonprofit hospitals and universities")
def aff_insider(ctx):
    rows = q(INSIDER_SQL)
    for r in rows:
        r["band"] = strip_band(r["band"])
    big, small = rows[-1], rows[0]
    v = q1("""SELECT avg(business_with_insiders::int) AS b, count(*) AS n FROM (SELECT DISTINCT ON (ein) ein, business_with_insiders, cy_revenue FROM filing_flag WHERE form = '990' ORDER BY ein, tax_year DESC, object_id DESC) x
              WHERE cy_revenue >= 1e8 AND ein IN (SELECT ein FROM orgs WHERE subsection = '03')""")
    long = [{"band": r["band"], "flag": p, "share": r[k]} for r in rows
            for p, k in (("Business deals with insiders (Sch. L Part IV)", "business_with_insiders"),
                         ("Family or business ties among officers", "family_or_business_ties"),
                         ("Loans to or from insiders", "loans_with_insiders"))]
    chart = vbar(long, "band", "share", "Charity revenue (newest 990)", "Share of charities", fmt=".0%", xoffset="flag", color_title=None)
    orgs = q("""SELECT ein FROM (SELECT DISTINCT ON (ein) ein, business_with_insiders, cy_revenue FROM filing_flag WHERE form = '990' ORDER BY ein, tax_year DESC, object_id DESC) x
                WHERE business_with_insiders AND cy_revenue >= 1e8 ORDER BY cy_revenue DESC LIMIT 60""")
    return {
        "headline": f"{pct(big['business_with_insiders'])} of $100M+ charities report business deals with board members, officers or their families, versus {pct(small['business_with_insiders'])} of those under $500K",
        "summary": (f"Insider business is disclosed, not forbidden, and big organizations have more vendors to overlap with. {pct(big['family_or_business_ties'])} of the largest also report family or business ties among officers. "
                    "Schedule L names the people and amounts, so read it before a major gift."),
        "chart": chart,
        "method": "Newest full Form 990 per 501(c)(3): Part IV line 28 (business transactions with interested persons), Part VI line 2 (family or business relationships among officers and directors) and Part IV line 26 (loans).",
        "caveats": "Self-reported; reporting thresholds (e.g. $100K+ per transaction) make small deals invisible, which lowers the rate for small charities. 990-EZ filers aren't included.",
        "sources": [SRC["990"]],
        "query": INSIDER_SQL,
        "data": rows,
        "org_eins": [o["ein"] for o in orgs],
        "checks": [("$100M+ recount", close(v["b"], big["business_with_insiders"], abs_=0.002) and int(v["n"]) == int(big["charities"])), ("$100M+ n >= 1,000", big["charities"] >= 1000)],
    }


FAMILY_SQL = f"""WITH {F990}
SELECT left(o.ntee_cd, 1) AS major, count(*) AS charities, round(avg(f.family_business_ties::int), 3) AS family_or_business_ties,
       round(avg((f.independent_members::float / nullif(f.voting_members, 0) < 0.5)::int), 3) AS minority_independent_board
FROM f JOIN orgs o USING (ein)
WHERE f.cy_revenue BETWEEN 500000 AND 10000000 AND o.subsection = '03' AND o.ntee_cd ~ '^[A-Z]'
GROUP BY 1 HAVING count(*) >= 1000 ORDER BY family_or_business_ties DESC"""


@insight("aff-family-ties-by-cause", "affiliations", 0.64, cause="Q", explore="international relief and development nonprofits")
def aff_family(ctx):
    rows = q(FAMILY_SQL)
    for r in rows:
        r["cause"] = MAJOR[r["major"]]
    top, bot = rows[0], rows[-1]
    allr = q1(f"""WITH {F990} SELECT avg(f.family_business_ties::int) AS a FROM f JOIN orgs o USING (ein)
                 WHERE f.cy_revenue BETWEEN 500000 AND 10000000 AND o.subsection = '03' AND o.ntee_cd ~ '^[A-Z]'""")
    v = q1(f"""SELECT avg(family_business_ties::int) AS a FROM (SELECT DISTINCT ON (ein) ein, family_business_ties, cy_revenue FROM filing_flag WHERE form = '990' ORDER BY ein, tax_year DESC, object_id DESC) x
               JOIN orgs o USING (ein) WHERE x.cy_revenue BETWEEN 500000 AND 10000000 AND o.subsection = '03' AND o.ntee_cd LIKE '{top['major']}%'""")
    second = rows[1]
    chart = hbar(rows, "cause", "family_or_business_ties", "Share reporting family or business ties among officers and directors",
                 rule=float(allr["a"]), tooltip_extra=[("charities", "quantitative", ",")])
    return {
        "headline": f"{pct(top['family_or_business_ties'])} of mid-size {top['cause'].lower()} charities report family or business ties among their leaders, versus {pct(bot['family_or_business_ties'])} in {bot['cause'].lower()}",
        "summary": (f"{top['cause']} and {second['cause'].lower()} groups are often founded and run by one family or diaspora network. "
                    "Family-led boards aren't a red flag on their own, but they raise the bar for independent oversight; check who sets pay and approves budgets."),
        "chart": chart,
        "method": "Newest full Form 990 per 501(c)(3) with $500K–$10M revenue: Part VI line 2 (any officer, director, trustee or key employee has a family or business relationship with another). By NTEE major group (1,000+ charities); dashed line = all.",
        "caveats": "Self-reported yes/no; doesn't say how many people are related.",
        "sources": [SRC["990"], SRC["bmf"]],
        "query": FAMILY_SQL,
        "data": rows,
        "org_eins": None,
        "cause": top["major"],
        "checks": [("top cause recount", close(v["a"], top["family_or_business_ties"], abs_=0.002)), ("n >= 1,000", top["charities"] >= 1000)],
    }


NPNP_SQL = """WITH s AS (SELECT state, count(*) AS charities FROM orgs WHERE subsection = '03' AND foundation NOT IN ('02', '03', '04')
           AND ein IN (SELECT ein FROM board_members) GROUP BY 1),
l AS (SELECT state, count(*) AS links FROM board_interlocks WHERE relation = 'link' AND confidence = 'high' GROUP BY 1),
pop AS (SELECT state, left(min(county_fips), 2)::int AS id FROM county_need WHERE county_fips <> '00000' GROUP BY state)
SELECT s.state, pop.id, s.charities, coalesce(l.links, 0) AS links, round(coalesce(l.links, 0) * 1000.0 / s.charities, 1) AS links_per_1000
FROM s JOIN pop USING (state) LEFT JOIN l USING (state) WHERE s.charities >= 1000 ORDER BY links_per_1000 DESC"""


@insight("aff-board-network-density", "affiliations", 0.58, explore="nonprofit board leadership")
def aff_density(ctx):
    rows = q(NPNP_SQL)
    for r in rows:
        r["name"] = STATE_NAMES.get(r["state"], r["state"])
    hi, lo = rows[0], rows[-1]
    v = q1(f"SELECT count(*) AS c FROM board_interlocks WHERE relation = 'link' AND confidence = 'high' AND state = '{hi['state']}'")
    chart = state_map(rows, "links_per_1000", "Hidden board links per 1,000 charities", fmt=",.0f", scale={"scheme": "purples"},
                      tooltip_extra=[("links", "quantitative", "High-confidence links", ","), ("charities", "quantitative", "Charities with board data", ",")])
    return {
        "headline": f"{hi['name']} has {hi['links_per_1000']:.0f} high-confidence hidden board links per 1,000 charities; {lo['name']} has {lo['links_per_1000']:.0f}",
        "summary": ("Where the same people sit on many boards, a few leaders shape a state's nonprofit agenda, and a single departure ripples further. "
                    "Dense networks also make independent diligence harder: references and co-funders are often the same people."),
        "chart": chart,
        "method": ("High-confidence hidden board links (board_interlocks relation 'link', confidence 'high': two organizations in the same state sharing 2+ board members, or one plus a grant between them; "
                   "sister organizations and bank trustees excluded), per 1,000 501(c)(3)s with board data in the state."),
        "caveats": "Name-based matching within a state on each organization's newest return; boards of very large charities are cut at 25 people. States with more 990 e-filers have more board data.",
        "sources": [SRC["board"], SRC["990"]],
        "query": NPNP_SQL,
        "data": rows,
        "org_eins": None,
        "checks": [("link recount", int(v["c"]) == int(hi["links"])), ("40+ states", len(rows) >= 40)],
    }


# ================================================================ FINANCIAL STRESS

GOV_CASH_SQL = f"""WITH {F990}
SELECT left(o.ntee_cd, 3) AS code, count(*) AS charities,
       round(avg((coalesce(f.government_grants, 0) >= 0.5 * f.cy_revenue)::int), 3) AS gov_majority,
       round(avg((f.cash * 12.0 / f.cy_expenses < 3)::int), 3) AS under_3_months_cash,
       round(avg((coalesce(f.government_grants, 0) >= 0.5 * f.cy_revenue AND f.cash * 12.0 / f.cy_expenses < 3)::int), 3) AS gov_and_thin_cash
FROM f JOIN orgs o USING (ein)
WHERE {F990_OK} AND o.subsection = '03' AND o.ntee_cd ~ '^[A-Z][0-9]{{2}}'
GROUP BY 1 HAVING count(*) >= 400 ORDER BY gov_and_thin_cash DESC"""


@insight("stress-government-thin-cash", "financial-stress", 0.9, cause="P", explore="domestic violence shelters")
def stress_gov_cash(ctx):
    rows = q(GOV_CASH_SQL)
    for r in rows:
        r["cause"] = f"{r['code']} {label(r['code'])}"
    top = rows[0]
    allr = q1(f"""WITH {F990} SELECT avg((coalesce(f.government_grants, 0) >= 0.5 * f.cy_revenue AND f.cash * 12.0 / f.cy_expenses < 3)::int) AS gov_and_thin_cash,
                  avg((coalesce(f.government_grants, 0) >= 0.5 * f.cy_revenue)::int) AS gov
                  FROM f JOIN orgs o USING (ein) WHERE {F990_OK} AND o.subsection = '03'""")
    v = q1(f"""SELECT count(*) AS n, avg((coalesce(government_grants, 0) >= 0.5 * cy_revenue)::int) AS gov, avg((cash * 12.0 / cy_expenses < 3)::int) AS cash
               FROM (SELECT DISTINCT ON (ein) * FROM filing_flag WHERE form = '990' ORDER BY ein, tax_year DESC, object_id DESC) x
               WHERE cy_revenue > 0 AND cy_expenses > 0 AND cash IS NOT NULL
                 AND ein IN (SELECT ein FROM orgs WHERE subsection = '03' AND left(ntee_cd, 3) = '{top['code']}')""")
    shown = {r["code"] for r in rows[:4]}
    for r in rows:
        r["label"] = r["cause"] if r["code"] in shown else ""
    chart = scatter(rows, "gov_majority", "under_3_months_cash", "Share getting half or more of revenue from government grants",
                    "Share with under 3 months of cash", "cause", size="charities",
                    label_test="datum.label != ''", tooltip_extra=[("charities", "quantitative", ",")])
    chart["layer"][1]["encoding"]["text"] = {"field": "label", "type": "nominal"}
    orgs = q(f"""WITH {F990} SELECT f.ein FROM f JOIN orgs o USING (ein) WHERE left(o.ntee_cd, 3) = '{top['code']}' AND {F990_OK}
                 AND coalesce(f.government_grants, 0) >= 0.5 * f.cy_revenue AND f.cash * 12.0 / f.cy_expenses < 3 ORDER BY f.cy_revenue DESC LIMIT 60""")
    return {
        "headline": (f"{pct(top['gov_majority'])} of {label(top['code']).lower()} get half their revenue from government grants, and {pct(top['gov_and_thin_cash'])} also have under three months of cash, "
                     f"the most exposed sub-cause to a federal funding freeze"),
        "summary": (f"Across all charities only {pct(allr['gov_and_thin_cash'], 1)} combine government dependence with thin reserves. "
                    "When federal or state grants pause (as in 2025), these organizations have weeks, not months, of runway; bridge funding from private donors can keep doors open."),
        "chart": chart,
        "method": ("Newest full Form 990 per 501(c)(3) (mostly tax years 2023–24): government grants (Part VIII line 1e) / total revenue; months of cash = (cash + savings, Part X lines 1–2) / (total expenses / 12). "
                   "By 3-character NTEE code, 400+ charities each."),
        "caveats": "Government fee-for-service payments (e.g. Medicaid) are program revenue, not grants, so dependence on public money is understated. Cash excludes investments and lines of credit.",
        "sources": [SRC["990"], SRC["bmf"], SRC["ntee"]],
        "query": GOV_CASH_SQL,
        "data": [{k: r[k] for k in ("code", "cause", "charities", "gov_majority", "under_3_months_cash", "gov_and_thin_cash")} for r in rows],
        "org_eins": [o["ein"] for o in orgs],
        "checks": [("top code recount", int(v["n"]) == int(top["charities"]) and close(v["gov"], top["gov_majority"], abs_=0.002) and close(v["cash"], top["under_3_months_cash"], abs_=0.002)),
                   ("n >= 400", top["charities"] >= 400)],
    }


CASH_SUB_SQL = f"""WITH {F990}
SELECT left(o.ntee_cd, 3) AS code, count(*) AS charities,
       round(avg((f.cash * 12.0 / f.cy_expenses < 3)::int), 3) AS under_3_months_cash,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY f.cash * 12.0 / f.cy_expenses))::numeric, 1) AS median_months
FROM f JOIN orgs o USING (ein)
WHERE {F990_OK} AND o.subsection = '03' AND o.ntee_cd ~ '^[A-Z][0-9]{{2}}'
GROUP BY 1 HAVING count(*) >= 400 ORDER BY under_3_months_cash DESC"""


@insight("stress-thin-cash-by-cause", "financial-stress", 0.76, cause="L", explore="senior housing and assisted living nonprofits")
def stress_cash(ctx):
    rows = q(CASH_SUB_SQL)
    for r in rows:
        r["cause"] = f"{r['code']} {label(r['code'])}"
    allr = q1(f"""WITH {F990} SELECT avg((f.cash * 12.0 / f.cy_expenses < 3)::int) AS s, percentile_cont(0.5) WITHIN GROUP (ORDER BY f.cash * 12.0 / f.cy_expenses) AS m
                  FROM f JOIN orgs o USING (ein) WHERE {F990_OK} AND o.subsection = '03'""")
    # hospitals and housing hold reserves in investments and buildings, not cash: shown, but not headlined
    cand = [r for r in rows if not r["code"].startswith(("E2", "L2"))]
    top, second, bot = cand[0], cand[1], rows[-1]
    v = q1(f"""SELECT avg((cash * 12.0 / cy_expenses < 3)::int) AS s FROM (SELECT DISTINCT ON (ein) * FROM filing_flag WHERE form = '990' ORDER BY ein, tax_year DESC, object_id DESC) x
               WHERE cy_revenue > 0 AND cy_expenses > 0 AND cash IS NOT NULL AND ein IN (SELECT ein FROM orgs WHERE subsection = '03' AND left(ntee_cd, 3) = '{top['code']}')""")
    chart = hbar(rows[:15] + rows[-3:], "cause", "under_3_months_cash", "Share with under 3 months of cash", rule=float(allr["s"]),
                 tooltip_extra=[("median_months", "quantitative", ".1f"), ("charities", "quantitative", ",")])
    return {
        "headline": (f"{pct(top['under_3_months_cash'])} of {label(top['code']).lower()} hold under three months of cash, among the thinnest reserves of any sub-cause; "
                     f"the typical charity holds {allr['m']:.1f} months"),
        "summary": (f"{label(second['code'])} ({pct(second['under_3_months_cash'])}) and other direct-service fields run close to empty, while {label(bot['code']).lower()} sit on the most. "
                    "Thin cash plus a payroll-heavy budget is the classic setup for a sudden closure; multi-year or reserve-building gifts help most here."),
        "chart": chart,
        "method": ("Newest full Form 990 per 501(c)(3): cash + savings (Part X lines 1–2, end of year) divided by monthly total expenses. By 3-character NTEE code with 400+ charities; "
                   "chart: 15 thinnest and 3 thickest; dashed line = all charities. Headline skips hospitals (E2x) and housing (L2x), which hold reserves as investments and buildings."),
        "caveats": "Excludes investments, receivables and credit lines; capital-heavy fields (housing, hospitals) hold wealth in buildings rather than cash. 990-EZ filers aren't included.",
        "sources": [SRC["990"], SRC["bmf"], SRC["ntee"]],
        "query": CASH_SUB_SQL,
        "data": rows,
        "org_eins": None,
        "cause": top["code"][0],
        "explore": f"{label(top['code']).lower()} nonprofits",
        "checks": [("top code recount", close(v["s"], top["under_3_months_cash"], abs_=0.002)), ("n >= 400", top["charities"] >= 400)],
    }


GOV_MAP_SQL = f"""WITH {F990},
pop AS (SELECT state, left(min(county_fips), 2)::int AS id FROM county_need WHERE county_fips <> '00000' GROUP BY state)
SELECT o.state, pop.id, count(*) AS charities,
       round(avg((coalesce(f.government_grants, 0) >= 0.5 * f.cy_revenue)::int), 3) AS gov_majority,
       round(avg((f.cash * 12.0 / f.cy_expenses < 3)::int), 3) AS under_3_months_cash
FROM f JOIN orgs o USING (ein) JOIN pop ON pop.state = o.state
WHERE {F990_OK} AND o.subsection = '03'
GROUP BY 1, 2 ORDER BY gov_majority DESC"""


@insight("stress-government-dependence-map", "financial-stress", 0.74, explore="nonprofits funded by government grants")
def stress_gov_map(ctx):
    rows = q(GOV_MAP_SQL)
    for r in rows:
        r["name"] = STATE_NAMES.get(r["state"], r["state"])
    hi, lo = rows[0], rows[-1]
    v = q1(f"""SELECT avg((coalesce(government_grants, 0) >= 0.5 * cy_revenue)::int) AS g FROM (SELECT DISTINCT ON (ein) * FROM filing_flag WHERE form = '990' ORDER BY ein, tax_year DESC, object_id DESC) x
               WHERE cy_revenue > 0 AND cy_expenses > 0 AND cash IS NOT NULL AND ein IN (SELECT ein FROM orgs WHERE subsection = '03' AND state = '{hi['state']}')""")
    chart = state_map(rows, "gov_majority", "Share of charities with 50%+ revenue from government grants", fmt=".0%",
                      scale={"scheme": "reds"}, tooltip_extra=[("charities", "quantitative", "Charities", ",")])
    return {
        "headline": f"In {hi['name']}, {pct(hi['gov_majority'])} of charities get half or more of their revenue from government grants; in {lo['name']}, {pct(lo['gov_majority'])}",
        "summary": ("States where charities lean on public grants face the sharpest local fallout from federal cuts and payment delays. "
                    "Private donors there are the backstop; a map like this helps target emergency or bridge funding."),
        "chart": chart,
        "method": "Newest full Form 990 per 501(c)(3) with usable revenue, expenses and cash: government grants (Part VIII line 1e) / total revenue ≥ 50%, by the charity's state.",
        "caveats": "Medicaid and other fee-for-service payments are program revenue, not grants. Statewide charities headquartered in a capital city count in that state only.",
        "sources": [SRC["990"]],
        "query": GOV_MAP_SQL,
        "data": rows,
        "org_eins": None,
        "checks": [("top state recount", close(v["g"], hi["gov_majority"], abs_=0.002)), ("n >= 500", hi["charities"] >= 500)],
    }


SIZE_CASH_SQL = f"""WITH {F990}
SELECT {REV_BAND.format(x='f.cy_revenue')} AS band, count(*) AS charities,
       round(avg((f.cash * 12.0 / f.cy_expenses < 3)::int), 3) AS under_3_months_cash,
       round(avg((f.cy_expenses > f.cy_revenue)::int), 3) AS deficit,
       round(avg((f.total_liabilities > f.total_assets)::int), 3) AS liabilities_exceed_assets
FROM f JOIN orgs o USING (ein) WHERE {F990_OK} AND o.subsection = '03' GROUP BY 1 ORDER BY 1"""


@insight("stress-deficits-by-size", "financial-stress", 0.6, explore="small community nonprofits")
def stress_size(ctx):
    rows = q(SIZE_CASH_SQL)
    for r in rows:
        r["band"] = strip_band(r["band"])
    allr = q1(f"""WITH {F990} SELECT avg((f.cy_expenses > f.cy_revenue)::int) AS d, count(*) AS n FROM f JOIN orgs o USING (ein) WHERE {F990_OK} AND o.subsection = '03'""")
    v = q1("""SELECT avg((cy_expenses > cy_revenue)::int) AS d FROM (SELECT DISTINCT ON (ein) * FROM filing_flag WHERE form = '990' ORDER BY ein, tax_year DESC, object_id DESC) x
              WHERE cy_revenue > 0 AND cy_expenses > 0 AND cash IS NOT NULL AND ein IN (SELECT ein FROM orgs WHERE subsection = '03')""")
    long = [{"band": r["band"], "measure": m, "share": r[k]} for r in rows
            for m, k in (("Spent more than it took in", "deficit"), ("Under 3 months of cash", "under_3_months_cash"), ("Liabilities exceed assets", "liabilities_exceed_assets"))]
    chart = vbar(long, "band", "share", "Charity revenue (newest 990)", "Share of charities", fmt=".0%", xoffset="measure", color_title=None)
    small, big = rows[0], rows[-1]
    return {
        "headline": f"{pct(allr['d'])} of US charities filing a full 990 spent more than they took in during their latest year; {pct(small['under_3_months_cash'])} of those under $500K hold under three months of cash",
        "summary": (f"Deficits are common at every size ({pct(small['deficit'])} of small, {pct(big['deficit'])} of $100M+), so one deficit year isn't a warning sign by itself; "
                    "two in a row with thin cash is. Small charities' thin cash makes them the most sensitive to a lost grant."),
        "chart": chart,
        "method": "Newest full Form 990 per 501(c)(3) (mostly tax years 2023–24): deficit = total expenses > total revenue (Part I lines 18–12); cash = Part X lines 1–2; liabilities and assets from Part X.",
        "caveats": "Deficits include non-cash depreciation and planned spending of prior-year restricted gifts. 990-EZ filers (under $200K) aren't included.",
        "sources": [SRC["990"]],
        "query": SIZE_CASH_SQL,
        "data": rows,
        "org_eins": None,
        "checks": [("deficit recount", close(v["d"], allr["d"], abs_=0.001)), ("n >= 100K", allr["n"] >= 100000)],
    }


SINGLE_SQL = f"""WITH {MIDSIZE},
r AS (SELECT recipient_ein AS ein, count(DISTINCT funder_ein) AS funders FROM grants
      WHERE tax_year BETWEEN 2022 AND 2024 AND recipient_ein IS NOT NULL AND amount > 0 GROUP BY 1)
SELECT left(u.ntee_cd, 1) AS major, count(*) AS funded_charities,
       round(avg((r.funders = 1)::int), 3) AS one_funder_share,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r.funders) AS median_funders
FROM u JOIN r USING (ein) GROUP BY 1 HAVING count(*) >= 500 ORDER BY one_funder_share DESC"""


@insight("stress-single-funder", "financial-stress", 0.7, cause="M", explore="volunteer fire departments")
def stress_single(ctx):
    rows = q(SINGLE_SQL)
    for r in rows:
        r["cause"] = MAJOR[r["major"]]
    top, bot = rows[0], rows[-1]
    v = q1(f"""SELECT avg((x.n = 1)::int) AS s, count(*) AS c FROM (
                 SELECT g.recipient_ein, count(DISTINCT g.funder_ein) AS n FROM grants g
                 WHERE g.tax_year IN (2022, 2023, 2024) AND g.amount > 0 AND g.recipient_ein IN (
                   SELECT o.ein FROM orgs o JOIN financials_latest f USING (ein) WHERE o.ntee_cd LIKE '{top['major']}%' AND o.ntee_cd ~ '^[A-Z][0-9]{{2}}'
                     AND o.subsection = '03' AND o.foundation NOT IN ('02','03','04') AND f.form = '990' AND f.revenue BETWEEN 250000 AND 5000000)
                 GROUP BY 1) x""")
    chart = hbar(rows, "cause", "one_funder_share", "Share of foundation-funded charities with a single foundation funder, 2022–24",
                 highlight=("major", top["major"]), tooltip_extra=[("funded_charities", "quantitative", ","), ("median_funders", "quantitative", ",")])
    return {
        "headline": f"{pct(top['one_funder_share'])} of mid-size {top['cause'].lower()} charities with foundation support rely on a single foundation, versus {pct(bot['one_funder_share'])} in {bot['cause'].lower()}",
        "summary": ("One-funder charities lose all foundation support when that funder changes course. "
                    f"{bot['cause']} and {rows[-2]['cause'].lower()} groups have a median of {bot['median_funders']:.0f} and {rows[-2]['median_funders']:.0f} foundation backers; "
                    f"{top['cause'].lower()} groups have {top['median_funders']:.0f}, often a local family or company foundation."),
        "chart": chart,
        "method": ("Public charities filing a full 990 with $250K–$5M revenue that received at least one linked 990-PF grant in tax years 2022–24; count of distinct foundations paying them, "
                   "by NTEE major group (500+ charities)."),
        "caveats": ctx.link + " Unlinked grants mean some charities have more funders than shown, so single-funder shares are ceilings.",
        "sources": [SRC["pf"], SRC["soi"], SRC["bmf"]],
        "query": SINGLE_SQL,
        "data": rows,
        "org_eins": None,
        "cause": top["major"],
        "checks": [("top cause recount", close(v["s"], top["one_funder_share"], abs_=0.002) and int(v["c"]) == int(top["funded_charities"])),
                   ("n >= 500", top["funded_charities"] >= 500)],
    }


INSOLVENT_SQL = f"""WITH {F990}
SELECT left(o.ntee_cd, 3) AS code, count(*) AS charities,
       round(avg((f.total_liabilities > f.total_assets)::int), 3) AS liabilities_exceed_assets,
       round(avg((f.cy_expenses > f.cy_revenue)::int), 3) AS deficit
FROM f JOIN orgs o USING (ein)
WHERE {F990_OK} AND f.total_assets > 0 AND o.subsection = '03' AND o.ntee_cd ~ '^[A-Z][0-9]{{2}}'
GROUP BY 1 HAVING count(*) >= 400 ORDER BY liabilities_exceed_assets DESC"""


@insight("stress-negative-net-assets", "financial-stress", 0.62, cause="L", explore="affordable housing nonprofits")
def stress_insolvent(ctx):
    rows = q(INSOLVENT_SQL)
    for r in rows:
        r["cause"] = f"{r['code']} {label(r['code'])}"
    top = rows[0]
    allr = q1(f"""WITH {F990} SELECT avg((f.total_liabilities > f.total_assets)::int) AS s FROM f JOIN orgs o USING (ein)
                  WHERE {F990_OK} AND f.total_assets > 0 AND o.subsection = '03' AND o.ntee_cd ~ '^[A-Z][0-9]{{2}}'""")
    v = q1(f"""SELECT avg((total_liabilities > total_assets)::int) AS s FROM (SELECT DISTINCT ON (ein) * FROM filing_flag WHERE form = '990' ORDER BY ein, tax_year DESC, object_id DESC) x
               WHERE cy_revenue > 0 AND cy_expenses > 0 AND cash IS NOT NULL AND total_assets > 0 AND ein IN (SELECT ein FROM orgs WHERE subsection = '03' AND left(ntee_cd, 3) = '{top['code']}')""")
    nonhousing = next(r for r in rows if not r["code"].startswith("L"))
    chart = hbar(rows[:15], "cause", "liabilities_exceed_assets", "Share whose liabilities exceed assets", rule=float(allr["s"]),
                 tooltip_extra=[("charities", "quantitative", ","), ("deficit", "quantitative", ".0%")])
    return {
        "headline": f"{pct(top['liabilities_exceed_assets'])} of {label(top['code']).lower()} owe more than they own, {top['liabilities_exceed_assets'] / allr['s']:.0f} times the {pct(allr['s'])} rate for all charities",
        "summary": (f"Debt-financed housing dominates the list, which is expected for mortgage-backed properties. Outside housing, {label(nonhousing['code']).lower()} lead at {pct(nonhousing['liabilities_exceed_assets'])}. "
                    "Negative net assets limit borrowing and can trigger covenant problems, so check the balance sheet, not just the income statement."),
        "chart": chart,
        "method": "Newest full Form 990 per 501(c)(3) with assets: total liabilities > total assets at end of year (Part X lines 26 and 16). 3-character NTEE codes with 400+ charities; chart: top 15, dashed line = all.",
        "caveats": "Book values: buildings are carried at depreciated cost, so property-rich organizations can look insolvent while holding valuable real estate.",
        "sources": [SRC["990"], SRC["bmf"], SRC["ntee"]],
        "query": INSOLVENT_SQL,
        "data": rows,
        "org_eins": None,
        "cause": top["code"][0],
        "checks": [("top code recount", close(v["s"], top["liabilities_exceed_assets"], abs_=0.002)), ("n >= 400", top["charities"] >= 400)],
    }


# ---------------------------------------------------------------- write
COLS = ["slug", "section", "headline", "summary", "chart", "method", "caveats", "sources", "query", "data", "org_eins",
        "explore_query", "cause", "state", "score", "verified"]


def upsert(rec):
    payload = json.dumps(rec, ensure_ascii=False, default=str)
    tag = "$j" + secrets.token_hex(4) + "$"
    assert tag not in payload
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLS if c != "slug")
    sql = (f"INSERT INTO insights ({', '.join(COLS)}, refreshed_at)\n"
           f"SELECT {', '.join(COLS)}, now() FROM jsonb_populate_record(NULL::insights, {tag}{payload}{tag}::jsonb)\n"
           f"ON CONFLICT (slug) DO UPDATE SET {sets}, refreshed_at = now();\n")
    psql(None, stdin=sql)


def run(item, ctx):
    out = item["fn"](ctx)
    checks = out.pop("checks")
    rec = {
        "slug": item["slug"], "section": item["section"], "score": item["score"],
        "cause": out.pop("cause", item["cause"]), "state": out.pop("state", item["state"]),
        "explore_query": out.pop("explore", item["explore"]),
        **out,
    }
    rec["verified"] = checks_ok(checks)
    assert len(rec["data"]) <= 500, (item["slug"], len(rec["data"]))
    if rec.get("org_eins"):
        rec["org_eins"] = [e.strip() for e in rec["org_eins"]][:60]
    return rec, checks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated slugs")
    ap.add_argument("--dry", action="store_true", help="print, don't write")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--json", help="also write every record to this JSON file (for chart checks)")
    a = ap.parse_args()
    if a.list:
        for s in SECTIONS:
            print(s, ", ".join(i["slug"] for i in REG if i["section"] == s))
        return
    only = set(a.only.split(",")) if a.only else None
    items = [i for i in REG if not only or i["slug"] in only]
    if only and len(items) != len(only):
        sys.exit(f"unknown slug(s): {only - {i['slug'] for i in items}}")
    ctx = Ctx()
    recs, failed = [], []
    for item in items:
        t0 = time.time()
        try:
            rec, checks = run(item, ctx)
        except Exception as e:  # keep the previous row; report
            failed.append(item["slug"])
            print(f"FAIL  {item['slug']}: {e}", file=sys.stderr)
            continue
        recs.append(rec)
        mark = "ok " if rec["verified"] else "UNVERIFIED"
        print(f"{mark} [{rec['section']}] {rec['slug']} ({time.time() - t0:.0f}s)\n     {rec['headline']}", flush=True)
        for name, ok in checks:
            if not ok:
                print(f"     check failed: {name}")
        if not a.dry:
            upsert(rec)
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(recs, fh, default=str)
    if not a.dry and not only and not failed:
        slugs = ",".join("'" + i["slug"] + "'" for i in REG)
        psql(f"DELETE FROM insights WHERE slug NOT IN ({slugs})")
    print(f"\n{len(recs)} insights, {sum(r['verified'] for r in recs)} verified, {len(failed)} failed" + (" (dry run)" if a.dry else ""))
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
