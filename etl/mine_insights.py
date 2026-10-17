#!/usr/bin/env python3
"""Mine, verify and store the patterns behind NomBot's Insights tab (table `insights`, migration 025).

  python3 etl/mine_insights.py                  # recompute every insight, verify, upsert, drop retired slugs
  python3 etl/mine_insights.py --only a,b       # just these slugs
  python3 etl/mine_insights.py --dry            # print headlines and checks, write nothing
  python3 etl/mine_insights.py --list           # slugs by section

Each insight is one function: a SQL query (stored in `query`, reproduces the numbers), an independent
check query, a Vega-Lite chart and text written from the numbers. `verified` is true only when every
check passes (the check query agrees and sample sizes are large enough). `stats` (migration 029) holds 2-4
preformatted key numbers for NomBot's finding page, from the same values as the headline. `points` (migration 030) holds
2-3 short bullets (key numbers in **bold**, the last one "Why it matters"); `headline` is a short claim and `summary` a
one-sentence fallback. `lint()` warns when text breaks those rules. Safe to run nightly after
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
    "990i": {"label": "IRS Form 990 e-file returns, Schedule I grants to organizations (IRS releases 2023–2026)", "url": "https://www.irs.gov/charities-non-profits/form-990-series-downloads"},
    "usaspending": {"label": "USAspending.gov award data archive, federal assistance (grants and cooperative agreements), FY2022–2025", "url": "https://www.usaspending.gov/download_center/award_data_archive"},
    "acs": {"label": "U.S. Census Bureau American Community Survey 5-year estimates, 2020–2024 (B17001 poverty)", "url": "https://www.census.gov/programs-surveys/acs"},
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


def spct(x, d=0):
    """Signed percent for changes: +38%, −12%."""
    s = pct(abs(float(x)), d)
    return ("+" if float(x) >= 0 else "−") + s


def stats(*pairs):
    """Key numbers for the detail page's stat tiles: [{label, value}], same numbers as the headline."""
    return [{"label": l, "value": v} for l, v in pairs]


def bold(x):
    """Bold a key number in `points` (the only markup NomBot renders)."""
    return f"**{x}**"


WHY = "**Why it matters:** "


def cap(s):
    return s[:1].upper() + s[1:]


def frac(x):
    """Plain fraction for headlines: 0.33 -> 'a third', 0.44 -> 'under half'."""
    opts = [(1 / 10, "a tenth"), (1 / 6, "a sixth"), (1 / 5, "a fifth"), (1 / 4, "a quarter"), (1 / 3, "a third"),
            (1 / 2, "half"), (2 / 3, "two thirds"), (3 / 4, "three quarters")]
    v, w = min(opts, key=lambda o: abs(o[0] - float(x)))
    return w if abs(v - float(x)) <= 0.02 else ("under " if float(x) < v else "over ") + w


def frac_the(x):
    """'half the', 'a third of the': for 'gets ___ foundation dollars'."""
    w = frac(x)
    return f"{w} the" if w.endswith("half") else f"{w} of the"


def fold(r):
    """Growth multiple in words: 2.0 -> 'doubled', 3.0 -> 'tripled', else 'grew 3.2-fold'."""
    for k, w in ((2, "doubled"), (3, "tripled"), (4, "quadrupled")):
        if abs(float(r) - k) < 0.1:
            return w
    return f"grew {float(r):.1f}-fold"


def and_list(xs):
    xs = list(xs)
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def lint(rec):
    """Style rules for the reader-facing text (warnings only): short headline, 2-3 short bullets ending in
    'Why it matters', one-sentence summary, no semicolons or em dashes."""
    words = lambda t: len(t.replace("**", "").split())
    pts = rec.get("points") or []
    out = []
    for f, t in [("headline", rec["headline"]), ("summary", rec["summary"])] + [(f"point {i + 1}", t) for i, t in enumerate(pts)]:
        if ";" in t or "\u2014" in t:
            out.append(f"{f}: semicolon or em dash")
    if words(rec["headline"]) > 13:
        out.append(f"headline {words(rec['headline'])} words")
    if words(rec["summary"]) > 25:
        out.append(f"summary {words(rec['summary'])} words")
    if not 2 <= len(pts) <= 3 or not pts[-1].startswith(WHY):
        out.append("points: need 2-3, last 'Why it matters'")
    out += [f"point {i + 1} {words(t)} words" for i, t in enumerate(pts) if words(t) > 16]
    return out


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
        # labels sit right of their point, except in the right third of the plot, where they would run past
        # the edge and get cut off: there they sit left of the point
        xmax = max((float(v[x]) for v in values if v.get(x) is not None), default=0)
        for side, test, dx in (("left", f"datum['{x}'] <= {xmax * 0.66}", 7), ("right", f"datum['{x}'] > {xmax * 0.66}", -7)):
            layers.append({"transform": [{"filter": label_test}, {"filter": test}],
                           "mark": {"type": "text", "align": side, "dx": dx, "fontSize": 11},
                           "encoding": {"x": enc["x"], "y": enc["y"], "text": {"field": label_field, "type": "nominal"}}})
    spec["layer"] = layers
    return spec


def _robust_color(color, values, field, scale, fmt):
    """Color scale for a choropleth that one outlier (often DC) can't wash out: when the max is far above
    the 95th percentile, the domain stops there (clamped) and the legend's top label reads "≥ $X".
    An explicit clamped domain gets the same "≥" label. Tooltips keep the true values."""
    scale = dict(scale or {})
    xs = sorted(float(v[field]) for v in values if v.get(field) is not None)
    if "domain" not in scale and len(xs) >= 10:
        p95 = xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]
        if p95 > 0 and xs[-1] > 1.5 * p95:
            lo = xs[0] if scale.get("type") != "log" else min(x for x in xs if x > 0)
            scale.update({"domain": [lo, p95], "clamp": True})
    if scale.get("clamp") and scale.get("domain"):
        top = scale["domain"][-1]
        color["legend"]["values"] = [scale["domain"][0], top]  # just the two ends, the top one marked
        color["legend"]["labelExpr"] = f"datum.value >= {top * 0.999} ? '≥ ' + datum.label : datum.label"
    if scale:
        color["scale"] = scale
    return color


def state_map(values, field, title, fmt=".0%", scale=None, tooltip_extra=()):
    """Choropleth of US states; values need `id` (state FIPS number) and `state`."""
    feature = {"url": US10M, "format": {"type": "topojson", "feature": "states"}}
    color = _robust_color({"field": field, "type": "quantitative", "title": title, "legend": {"format": fmt, "orient": "bottom", "titleLimit": 400}},
                          values, field, scale, fmt)
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
    color = _robust_color({"field": field, "type": "quantitative", "title": title, "legend": {"format": fmt, "orient": "bottom", "titleLimit": 400}},
                          values, field, scale, fmt)
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
        "stats": stats(("Child care providers funded", pct(care['share_funded'])), ("All mid-size charities", pct(tot['share'])), ("Child care providers", n(care['charities']))),
        "headline": f"Child care gets {frac_the(care['share_funded'] / tot['share'])} foundation reach of other charities",
        "summary": f"Only {pct(care['share_funded'])} of mid-size child care nonprofits got a foundation grant in 2022–24, versus {pct(tot['share'])} of all mid-size charities.",
        "points": [f"{bold(pct(care['share_funded']))} of mid-size child care nonprofits got a foundation grant (2022–24)",
                   f"{bold(pct(tot['share']))} of all mid-size charities did. {cap(label(top['code']).lower())} top the list at {bold(pct(top['share_funded']))}",
                   WHY + "a first gift counts more, but few peer funders share diligence"],
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
        "stats": stats((f"{lo1['name']}, per resident", money(lo1['per_resident_year'])), (f"{hi['name']}, per resident", money(hi['per_resident_year'])), ("Gap", f"{ratio:.0f}x")),
        "headline": f"{hi['name']} gets {ratio:.0f} times {lo1['name']}'s foundation dollars per resident",
        "summary": (f"{lo1['name']} receives {money(lo1['per_resident_year'])} per resident a year in foundation grants, "
                    f"{hi['name']} {money(hi['per_resident_year'])}, {ratio:.0f} times as much."),
        "points": [(f"{bold(money(lo1['per_resident_year']))} per resident a year in {lo1['name']} and {lo2['name']}, the lowest"
                    if money(lo1['per_resident_year']) == money(lo2['per_resident_year']) else
                    f"{bold(money(lo1['per_resident_year']))} per resident a year in {lo1['name']}, {bold(money(lo2['per_resident_year']))} in {lo2['name']}, the lowest"),
                   f"{bold(money(hi['per_resident_year']))} in {hi['name']}, the highest state",
                   WHY + "foundation dollars follow wealth, not need, so low-dollar states are under-served"],
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
    small_pop = small["population"] / 1e6
    return {
        "stats": stats(("Counties of 10K–50K, per resident", money(small['median_county_per_resident'])), ("Counties of 1M+, per resident", money(big['median_county_per_resident'])), ("Gap", f"{ratio:.0f}x"), ("Small counties", n(small['counties']))),
        "headline": f"Big counties get {ratio:.0f} times the foundation dollars per resident of small ones",
        "summary": (f"The typical county of 10,000–50,000 people gets {money(small['median_county_per_resident'])} per resident a year in foundation grants, "
                    f"versus {money(big['median_county_per_resident'])} in counties of 1 million+."),
        "points": [f"{bold(money(small['median_county_per_resident']))} per resident a year in the typical county of 10K–50K people",
                   f"{bold(money(big['median_county_per_resident']))} in the typical county of 1M+. Small counties hold {bold(f'{small_pop:.0f} million')} people",
                   WHY + "rural areas lack grant-ready groups, not need"],
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
        "stats": stats(("Poorest fifth, per resident", money(hi['median_county_per_resident'])), ("Least-poor fifth, per resident", money(lo['median_county_per_resident'])), ("Gap", f"{lo['median_county_per_resident'] / hi['median_county_per_resident']:.1f}x")),
        "headline": f"The poorest counties get {frac_the(hi['median_county_per_resident'] / lo['median_county_per_resident'])} foundation dollars per resident",
        "summary": (f"The poorest fifth of US counties gets a median {money(hi['median_county_per_resident'])} per resident a year in foundation grants, "
                    f"versus {money(lo['median_county_per_resident'])} in the least-poor fifth."),
        "points": [f"{bold(money(hi['median_county_per_resident']))} per resident a year in the typical high-poverty county ({hi['poverty_rate_range']} poor)",
                   f"{bold(money(lo['median_county_per_resident']))} in the typical county of the least-poor fifth",
                   WHY + "a donor's dollar likely goes further where poverty is highest"],
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
        "stats": stats(("Appalachia, per resident", money(a)), ("Rest of US, per resident", money(b)), ("Appalachian counties", n(len(app)))),
        "headline": f"Appalachia gets {frac_the(a / b)} foundation dollars per person of other US counties",
        "summary": f"Appalachian counties receive {money(a)} per resident a year in foundation grants, {pct(a / b)} of the {money(b)} the rest of the country gets.",
        "points": [f"{bold(money(a))} per resident a year across {n(len(app))} Appalachian counties ({sum(r['population'] for r in app) / 1e6:.0f} million people)",
                   f"{bold(money(b))} per resident in the rest of the country",
                   WHY + "a gift here fills a gap most foundations leave, as across the rural South"],
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
        "stats": stats((f"{STATE_NAMES[lo['state']]}, funded", pct(lo['share_funded'])), (f"{STATE_NAMES[hi['state']]}, funded", pct(hi['share_funded']))),
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
    since = last  # first year of the current unbroken rise
    while since - 1 in by and by[since - 1] < by[since]:
        since -= 1
    chart = line(rows, "year", "new_charities", "Year the IRS recognized the charity", "Charities still on the IRS list")
    return {
        "stats": stats((f"New charities in {last}", n(by[last])), (f"Change since {last - 2}", spct(chg))),
        "headline": f"New charities are up {pct(chg)} in two years",
        "summary": f"The IRS recognized {n(by[last])} new charities in {last}, up {pct(chg)} from {last - 2}.",
        "points": [f"{bold(n(by[last]))} new charities recognized by the IRS in {last}",
                   f"{bold(n(by[last - 2]))} in {last - 2}" + (f", and the count has risen every year since {since}" if since <= last - 3 else ""),
                   WHY + "more young, unrated groups to vet without years of filings"],
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
        "stats": stats((f"Growth, {label(top['code'])}", spct(top['growth'])), ("Growth, all new charities", spct(overall)), (f"New {label(top['code']).lower()} in 2025", n(top['new_2025']))),
        "headline": f"New {label(top['code']).lower()} charities {fold(1 + top['growth'])} from 2023 to 2025",
        "summary": (f"New {label(top['code']).lower()} charities grew {pct(top['growth'])} from 2023 to 2025 ({n(top['new_2023'])} to {n(top['new_2025'])}), "
                    f"against {pct(overall)} for all new charities."),
        "points": [f"{bold(n(top['new_2023']))} new {label(top['code']).lower()} charities in 2023, {bold(n(top['new_2025']))} in 2025",
                   f"All new charities grew {bold(pct(overall))}. Most 2025 founders are in {and_list(STATE_NAMES.get(x['state'], x['state']) for x in states)}",
                   WHY + ("many local recovery groups are too new to have a 990 on file" if top["code"].startswith("M")
                          else "more young groups now compete for the same funders")],
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
    q_txt = f", international affairs {bold(spct(q_['change_capped']))}" if q_ and q_ is not bot and q_ is not top else ""
    return {
        "stats": stats((top['cause'], spct(top['change_capped'])), (bot['cause'], spct(bot['change_capped'])), ("All causes", spct(overall))),
        "headline": f"{cap(top['cause'].lower())} grants rose fastest, up {pct(top['change_capped'])} in two years",
        "summary": (f"Among foundations that filed both years, grants to {top['cause'].lower()} rose {pct(top['change_capped'])} from 2022 to 2024, "
                    f"versus {pct(overall)} for all causes."),
        "points": [f"{bold(spct(top['change_capped']))} to {top['cause'].lower()} from the same foundations, 2022 to 2024",
                   f"{bold(spct(overall))} across all causes. {cap(bot['cause'].lower())} {bold(spct(bot['change_capped']))}{q_txt}",
                   WHY + "the same funders are shifting priorities, so rising causes get crowded"],
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
        "stats": stats(("New in 2023", n(a)), ("New in 2025", n(b)), ("Growth", f"{b / a:.1f}x")),
        "headline": f"New disaster-relief charities {fold(b / a)} in two years",
        "summary": f"New disaster-relief charities grew from {n(a)} recognized in 2023 to {n(b)} in 2025, led by {and_list(STATE_NAMES[r['state']] for r in top)}.",
        "points": [f"{bold(n(a))} new disaster-relief charities in 2023, {bold(n(b))} in 2025",
                   f"{and_list(STATE_NAMES[r['state']] for r in top)} lead, after recent hurricanes, floods and fires",
                   WHY + "most are too new to have a 990, so ask for a budget"],
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
    return {
        "stats": stats(("To other grantmakers", spct(g_int)), ("To all other charities", spct(g_oth)), ("Grantmaker share, 2024", pct(b['grantmaker_share']))),
        "headline": f"Foundation grants to other grantmakers rose {pct(g_int)} in two years",
        "summary": f"Foundation grants to other grantmakers rose {pct(g_int)} from 2022 to 2024, versus {pct(g_oth)} to all other charities.",
        "points": [f"{bold(spct(g_int))} to DAF sponsors, community foundations and federated funds, 2022 to 2024",
                   f"{bold(spct(g_oth))} to all other charities. Grantmakers got {bold(pct(b['grantmaker_share']))} of 2024 dollars",
                   WHY + "parked money reaches charities later and is harder to trace"],
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
        "stats": stats(("Patient-assistance grants", money(pap, 0)), ("Share of all grant dollars", pct(pap / total)), ("Foundations", n(len(rows)))),
        "headline": f"Drug-company patient aid is {frac(pap / total)} of all foundation grant dollars",
        "summary": (f"Drug-company patient-assistance foundations reported {money(pap, 0)} in grants for 2022–24, {pct(pap / total)} of all private-foundation grant dollars, "
                    "mostly donated medicine."),
        "points": [f"{bold(money(pap, 0))} in 2022–24 grants from {bold(n(len(rows)))} patient-assistance foundations",
                   f"Mostly donated medicine to patients. Largest: {rows[0]['funder']}",
                   WHY + "strip them out before benchmarking giving to charities"],
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
        "stats": stats(*((f"{h['name']}, from out of state", pct(h['out_of_state_share'])) for h in hi[:2]), (f"{lo['name']}, from out of state", pct(lo['out_of_state_share']))),
        "headline": f"{and_list(h['name'] for h in hi)} run on out-of-state foundation money",
        "summary": (f"{and_list(h['name'] for h in hi)} charities get {pct(min(h['out_of_state_share'] for h in hi))}+ of their foundation dollars "
                    f"from out-of-state funders, versus {pct(lo['out_of_state_share'])} in {lo['name']}."),
        "points": [f"{bold(pct(min(h['out_of_state_share'] for h in hi)) + '+')} of their charities' foundation dollars come from other states",
                   f"{bold(pct(lo['out_of_state_share']))} in {lo['name']}, the most self-funded state",
                   WHY + "outside funders can shift priorities without local accountability"],
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
        "stats": stats((f"{exp['name']}, net exported", money(-exp['net'])), (f"{imp['name']}, net imported", money(imp['net']))),
        "headline": f"{exp['name']} exports the most foundation money, and {imp['name']} imports the most",
        "summary": (f"{exp['name']}'s foundations gave {money(-exp['net'])} more than its charities received in 2022–24, "
                    f"while {imp['name']} took in {money(imp['net'])} more than its funders gave."),
        "points": [f"{bold(money(-exp['net']))} more given than received in {exp['name']} (2022–24)" + (", home of the Gates Foundation" if exp["state"] == "WA" else ""),
                   f"{bold(money(imp['net']))} more received than given in {imp['name']}. {imp2['name']} is next",
                   WHY + "a few places set the agenda for philanthropy everywhere else"],
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
        "stats": stats(("Education, top 1% share", pct(b['top_1pct_share'])), ("Human services, top 1% share", pct(p['top_1pct_share'])), ("Top 1% of education grantees", n(b['top_1pct_orgs']))),
        "headline": f"The top 1% of education grantees take {frac(b['top_1pct_share'])} of foundation dollars",
        "summary": (f"In education, {n(b['top_1pct_orgs'])} organizations (the top 1% of grantees) took {pct(b['top_1pct_share'])} of foundation dollars in 2022–24, "
                    f"versus {pct(p['top_1pct_share'])} in human services."),
        "points": [f"{bold(n(b['top_1pct_orgs']))} education groups took {bold(pct(b['top_1pct_share']))} of 2022–24 foundation dollars",
                   f"The top 1% took {bold(pct(p['top_1pct_share']))} in human services. {cap(rows[-1]['cause'].lower())} is the most evenly spread",
                   WHY + "in top-heavy causes, look below the top 1% for leverage"],
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
        "stats": stats(("Foundation to foundation", money(tot)), ("Share of linked dollars", pct(tot / linked)), ("Foundations involved", n(funders))),
        "headline": f"{cap(frac(tot / linked))} of traceable foundation grant dollars go to another foundation",
        "summary": (f"{money(tot)} ({pct(tot / linked)}) of linked foundation grant dollars in 2022–24 went to another private foundation, "
                    "mostly within the same family."),
        "points": [f"{bold(money(tot))} in 2022–24 grants went from one private foundation to another",
                   f"{bold(money(top['amount']))} of it was {top['funder'].title()} paying {top['recipient'].title()}",
                   WHY + "exclude these transfers from flow totals to avoid double counting"],
        "chart": chart,
        "method": "990-PF grants for tax years 2022–24 whose linked recipient is itself a private foundation (IRS foundation codes 02–04). Share = over all grant dollars linked to any organization.",
        "caveats": ctx.link + " Unlinked transfers aren't counted, so the total is a floor.",
        "sources": [SRC["pf"], SRC["bmf"]],
        "query": PF_TRANSFER_SQL,
        "data": [{k: r[k] for k in ("funder_ein", "funder", "recipient_ein", "recipient", "amount")} for r in rows[:500]],
        "org_eins": list(dict.fromkeys(r["recipient_ein"] for r in rows[:60])),
        "checks": [("total recount", close(v["s"], tot, rel=0.001)), ("funder count", int(v["funders"]) == funders)],
    }


# ---------------------------------------------------------------- cause labels (migration 032, etl/label_causes.py)
# Causes no NTEE code captures, labeled per organization (primary = the cause is the main purpose, partial = one
# explicit program among others) and graded on fresh samples (cause_labels.grading). Method: docs/labels/<label>.md.
LABEL_DOC = "https://github.com/institute-on-generosity/generosity-data/blob/main/docs/labels/{}.md"


def label_year_sql(label):
    return f"""WITH {CLEAN},
g AS (SELECT c.tax_year, c.amount, l.tier FROM clean c
      LEFT JOIN org_labels l ON l.ein = c.recipient_ein AND l.label = '{label}' WHERE c.recipient_ein IS NOT NULL)
SELECT tax_year, round(sum(amount)) AS linked, round(sum(amount) FILTER (WHERE tier = 'primary')) AS primary_usd,
       round(sum(amount) FILTER (WHERE tier = 'partial')) AS partial_usd
FROM g GROUP BY 1 ORDER BY 1"""


def label_facts(label):
    """Shared numbers for a cause-label insight: dollars by year and tier, orgs, top funders and recipients,
    an independent recount and the stored grade."""
    rows = q(label_year_sql(label))
    lab = q1(f"SELECT name, version, grading FROM cause_labels WHERE label = '{label}'")
    t = {k: sum(r[k] or 0 for r in rows) for k in ("linked", "primary_usd", "partial_usd")}
    base = f"""WITH {CLEAN} SELECT c.funder_ein, c.recipient_ein, c.amount, l.tier FROM clean c
               JOIN org_labels l ON l.ein = c.recipient_ein AND l.label = '{label}'"""
    orgs = q1(f"SELECT count(DISTINCT recipient_ein) FILTER (WHERE tier = 'primary') AS p, count(DISTINCT recipient_ein) AS a, "
              f"count(DISTINCT funder_ein) FILTER (WHERE tier = 'primary') AS f FROM ({base}) x")
    funders = q(f"""SELECT f.ein, f.name, round(sum(x.amount)) AS amount FROM ({base}) x JOIN funders f ON f.ein = x.funder_ein
                    WHERE x.tier = 'primary' GROUP BY 1, 2 ORDER BY amount DESC LIMIT 10""")
    recips = q(f"""SELECT o.ein, o.name, x.tier, round(sum(x.amount)) AS amount FROM ({base}) x JOIN orgs o ON o.ein = x.recipient_ein
                   GROUP BY 1, 2, 3 ORDER BY amount DESC LIMIT 60""")
    v = q1(f"""SELECT sum(g.amount) AS s FROM grants g
               WHERE g.tax_year IN (2022, 2023, 2024) AND g.amount > 0 AND g.recipient_kind IS NULL
                 AND EXISTS (SELECT 1 FROM org_labels l WHERE l.ein = g.recipient_ein AND l.label = '{label}' AND l.tier = 'primary')
                 AND NOT EXISTS (SELECT 1 FROM orgs o WHERE o.ein = g.recipient_ein AND o.foundation IN ('02', '03', '04'))""")
    gr = lab.get("grading") or {}
    long = [{"year": str(r["tax_year"]), "tier": k, "millions": round((r[c] or 0) / 1e6, 1)} for r in rows
            for k, c in (("Main purpose (primary)", "primary_usd"), ("One program among others (partial)", "partial_usd"))]
    return rows, t, orgs, funders, recips, v, gr, long


def label_grade_text(gr):
    if not gr:
        return "Labels not yet graded. "
    p, a = gr["primary"], gr["partial"]
    return (f"Labels were checked on a fresh random sample by a second, stronger model: primary {pct(p['exact_weighted'])} right "
            f"({p['exact']['n']} graded), partial {pct(a['in_label_weighted'])} right ({a['in_label']['n']} graded); "
            f"estimated recall {pct(gr['recall_est'])} within likely NTEE groups. ")


def label_checks(t, v, orgs, gr, version):
    return [("primary recount", close(v["s"], t["primary_usd"], rel=0.001)), ("100+ funded primary orgs", orgs["p"] >= 100),
            ("graded current version", gr.get("version") == version),
            ("precision bars (primary 95%, partial 90%)", bool(gr) and gr["primary"]["exact_weighted"] >= 0.95 and gr["partial"]["in_label_weighted"] >= 0.90)]


LABEL_CAVEAT = (" A grant counts by its recipient's label, not its purpose: grants to partial organizations count in full in the "
                "'with partial' total, so that total is an upper bound. Individual and corporate giving outside foundations isn't visible; "
                "tax data covers itemizers only.")


@insight("flow-climate-share", "money-flows", 0.9, explore="climate change and clean energy nonprofits")
def flow_climate(ctx):
    rows, t, orgs, funders, recips, v, gr, long = label_facts("climate")
    lab = q1("SELECT version FROM cause_labels WHERE label = 'climate'")
    share, both = t["primary_usd"] / t["linked"], (t["primary_usd"] + t["partial_usd"]) / t["linked"]
    top10 = sum(f["amount"] for f in funders) / t["primary_usd"]
    chart = vbar(long, "year", "millions", "Tax year", "Foundation grants, $ millions", fmt="$,.0f", xoffset="tier")
    return {
        "stats": stats(("To climate-focused groups, 2022–24", money(t["primary_usd"])), ("Share of linked foundation dollars", pct(share, 1)),
                       ("With climate programs counted", pct(both, 1)), ("Top 10 funders' share", pct(top10))),
        "headline": f"Climate-focused groups get {pct(share, 1)} of foundation grant dollars",
        "summary": (f"Climate-focused nonprofits received {money(t['primary_usd'])} of 2022–24 foundation grants, {pct(share, 1)} of linked dollars, "
                    f"or {pct(both, 1)} counting groups with a climate program."),
        "points": [f"{bold(money(t['primary_usd']))} in 2022–24 to {bold(n(orgs['p']))} climate-focused groups, {bold(pct(share, 1))} of linked dollars",
                   f"{bold(pct(both, 1))} counting groups with a climate program. The top 10 funders gave {bold(pct(top10))}",
                   WHY + "no IRS code marks climate, so 990 data could not count it before"],
        "chart": chart,
        "method": ("990-PF grants for tax years 2022–24 linked to a public charity, by the recipient's climate label (org_labels, migration 032). "
                   "Labels come from NTEE codes, keywords and mission-embedding similarity, then Claude reads each candidate's 990 mission and programs: "
                   "primary = climate or clean energy is the main stated purpose, partial = one explicit climate program among broader work. "
                   "Share = over all linked grant dollars to public charities. Full method: docs/labels/climate.md."),
        "caveats": (ctx.link + " " + label_grade_text(gr) + "Land, wildlife and water conservation count only when the record names climate. "
                    "Foreign recipients aren't linked, so international climate grants are missing." + LABEL_CAVEAT),
        "sources": [SRC["pf"], SRC["990"], SRC["bmf"], {"label": "Cause label method: climate", "url": LABEL_DOC.format("climate")},
                    {"label": "ClimateWorks Foundation, Funding Trends (mitigation, for comparison)", "url": "https://www.climateworks.org/report/funding-trends-2026/"}],
        "query": label_year_sql("climate"),
        "data": rows,
        "org_eins": [r["ein"] for r in recips],
        "checks": label_checks(t, v, orgs, gr, lab["version"]),
    }


@insight("flow-women-girls-share", "money-flows", 0.9, explore="nonprofits serving women and girls")
def flow_women_girls(ctx):
    rows, t, orgs, funders, recips, v, gr, long = label_facts("women_girls")
    lab = q1("SELECT version FROM cause_labels WHERE label = 'women_girls'")
    share, both = t["primary_usd"] / t["linked"], (t["primary_usd"] + t["partial_usd"]) / t["linked"]
    top10 = sum(f["amount"] for f in funders) / t["primary_usd"]
    chart = vbar(long, "year", "millions", "Tax year", "Foundation grants, $ millions", fmt="$,.0f", xoffset="tier")
    return {
        "stats": stats(("To women and girls groups, 2022–24", money(t["primary_usd"])), ("Share of linked foundation dollars", pct(share, 1)),
                       ("With mixed groups counted", pct(both, 1)), ("Top 10 funders' share", pct(top10))),
        "headline": f"Groups dedicated to women and girls get {pct(share, 1)} of foundation dollars",
        "summary": (f"Nonprofits dedicated to women and girls received {money(t['primary_usd'])} of 2022–24 foundation grants, {pct(share, 1)} of linked dollars, "
                    f"or {pct(both, 1)} counting mixed groups."),
        "points": [f"{bold(money(t['primary_usd']))} in 2022–24 to {bold(n(orgs['p']))} groups dedicated to women and girls, {bold(pct(share, 1))} of linked dollars",
                   f"{bold(pct(both, 1))} counting mixed groups such as Boys & Girls Clubs. The top 10 funders gave {bold(pct(top10))}",
                   WHY + "no IRS code marks this cause, so 990 data could not count it"],
        "chart": chart,
        "method": ("990-PF grants for tax years 2022–24 linked to a public charity, by the recipient's women-and-girls label (org_labels, migration 032). "
                   "Labels come from NTEE codes, keywords and mission-embedding similarity, then Claude reads each candidate's 990 mission and programs: "
                   "primary = women and/or girls are the main intended beneficiaries, partial = an explicit focus alongside others (co-ed youth programs, "
                   "women's collectives doing general community work). Share = over all linked grant dollars to public charities. Full method: docs/labels/women_girls.md."),
        "caveats": (ctx.link + " " + label_grade_text(gr) + "The Women's Philanthropy Institute's index measures all giving these groups receive "
                    "(about 2% in 2022–23), mostly from individuals, so its share is not directly comparable." + LABEL_CAVEAT),
        "sources": [SRC["pf"], SRC["990"], SRC["bmf"], {"label": "Cause label method: women and girls", "url": LABEL_DOC.format("women_girls")},
                    {"label": "Women's Philanthropy Institute, Women & Girls Index 2025", "url": "https://philanthropy.indianapolis.iu.edu/news-events/news/_news/2025/women-and-girls-2025.html"}],
        "query": label_year_sql("women_girls"),
        "data": rows,
        "org_eins": [r["ein"] for r in recips],
        "checks": label_checks(t, v, orgs, gr, lab["version"]),
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
        "stats": stats(("Closed to applications", pct(allr['share'])), ("Their share of grant dollars", pct(allr['dollars'])), ("Closed, $1M–10M foundations", pct(mid['invite_only_share'])), ("Closed, $1B+ foundations", pct(big['invite_only_share']))),
        "headline": f"{pct(allr['share'])} of foundations won't take unsolicited applications",
        "summary": (f"{pct(allr['share'])} of US private foundations say they don't accept unsolicited applications, "
                    f"and they pay {pct(allr['dollars'])} of foundation grant dollars."),
        "points": [f"Closed foundations pay {bold(pct(allr['dollars']))} of all foundation grant dollars",
                   f"{bold(pct(mid['invite_only_share']))} of $1M–10M foundations are closed, {bold(pct(big['invite_only_share']))} of $1B+ ones",
                   WHY + "relationships, often through advisors and boards, are the way in"],
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
        "stats": stats(("General support, $1B+", pct(big['general_share_of_dollars'])), ("General support, $1M–10M", pct(mid['general_share_of_dollars']))),
        "headline": f"Billion-dollar foundations give just {pct(big['general_share_of_dollars'])} of dollars as general support",
        "summary": (f"$1B+ foundations label only {pct(big['general_share_of_dollars'])} of their grant dollars as general operating support, "
                    f"versus {pct(mid['general_share_of_dollars'])} at $1M–10M foundations."),
        "points": [f"{bold(pct(mid['general_share_of_dollars']))} of dollars at $1M–10M foundations are general support",
                   f"Big funders' unrestricted grants are small ones ({bold(pct(big['general_share_of_grants']))} of grants, few dollars)",
                   WHY + "for overhead, mid-size family foundations are the likelier source"],
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
        "stats": stats(("$1B+ paying under 5%", pct(big['share_under_5pct'])), ("Median payout, $1B+", pct(big['median_grants_to_assets'], 1)), ("Median payout, smallest", pct(small['median_grants_to_assets'], 1))),
        "headline": f"{pct(big['share_under_5pct'])} of billion-dollar foundations granted under 5% of assets",
        "summary": (f"{pct(big['share_under_5pct'])} of $1B+ foundations paid grants worth less than 5% of their assets in their latest year, "
                    f"with a median of {pct(big['median_grants_to_assets'], 1)}."),
        "points": [f"{bold(pct(big['median_grants_to_assets'], 1))} of assets granted by the median $1B+ foundation in its latest year",
                   f"{bold(pct(small['median_grants_to_assets'], 1))} at the median foundation under $1M",
                   WHY + "not a compliance test, but a benchmark for a family's own payout"],
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
        "stats": stats(("$100M–1B, half to one grantee", pct(big['share_half_to_one_grantee'])), ("Under $1M, half to one grantee", pct(small['share_half_to_one_grantee']))),
        "headline": f"{cap(frac(big['share_half_to_one_grantee']))} of $100M–1B foundations give most of their money to one group",
        "summary": f"{pct(big['share_half_to_one_grantee'])} of $100M–1B foundations gave more than half of their 2022–24 grant dollars to a single organization.",
        "points": [f"{bold(pct(big['share_half_to_one_grantee']))} of $100M–1B foundations gave over half their 2022–24 dollars to one group",
                   f"{bold(pct(small['share_half_to_one_grantee']))} of foundations under $1M did the same",
                   WHY + "assets overstate what's open to applicants, so check the grant list"],
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
        "stats": stats(("Audited, $500K–2M revenue", pct(mid['audited'])), ("Whistleblower policy", pct(mid['whistleblower_policy']))),
        "headline": f"Only {pct(mid['audited'])} of $500K–2M charities have audited financials",
        "summary": (f"Only {pct(mid['audited'])} of charities with $500K–2M in revenue report audited financial statements, "
                    f"and {pct(mid['whistleblower_policy'])} have a whistleblower policy."),
        "points": [f"{bold(pct(rows[2]['audited']))} of $2M–10M charities are audited, so the gap closes with size",
                   f"{bold(pct(mid['whistleblower_policy']))} of $500K–2M charities have a whistleblower policy, {bold(pct(mid['conflict_policy']))} a conflict policy",
                   WHY + "ask small groups for a review or compilation instead"],
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
        "stats": stats(("Funded again within 2 years", pct(allr['r'])), (hi['cause'], pct(hi['renewal_rate'])), (lo['cause'], pct(lo['renewal_rate']))),
        "headline": f"{pct(allr['r'])} of foundation grantees are funded again within two years",
        "summary": f"{pct(allr['r'])} of 2022 foundation grantees were funded again by the same foundation within two years.",
        "points": [f"{bold(pct(hi['renewal_rate']))} renewal in {hi['cause'].lower()}, the highest",
                   f"{bold(pct(lo['renewal_rate']))} in {lo['cause'].lower()}, the lowest",
                   WHY + "the first grant is the hard part, then funders tend to stay"],
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
        "stats": stats(("$1B+ paying a board-linked group", pct(big['share_with_board_linked_grantee'])), ("Board-linked grants", money(tot['amount'])), ("Foundations", n(tot['funders']))),
        "headline": f"{pct(big['share_with_board_linked_grantee'])} of billion-dollar foundations fund groups that share their board",
        "summary": (f"{pct(big['share_with_board_linked_grantee'])} of $1B+ foundations paid an organization that shares their board members in 2022–24, "
                    "excluding sister organizations and bank trustees."),
        "points": [f"{bold(money(tot['amount']))} from {bold(n(tot['funders']))} foundations to groups sharing their board members",
                   f"Small foundations with such ties sent a median {bold(pct(small['median_share_of_giving_when_linked']))} of giving there",
                   WHY + "legal, but ask whether money stays in the board's own circle"],
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
        "stats": stats((f"{top['cause']}, board-linked", pct(top['board_linked_share'], 1)), ("All causes", pct(allshare, 1)), ("Ratio", f"{top['board_linked_share'] / allshare:.1f}x")),
        "headline": f"{cap(top['cause'].lower())} charities lean most on their own board members' foundations",
        "summary": (f"{top['cause']} charities get {pct(top['board_linked_share'], 1)} of their foundation dollars from funders sharing board members, "
                    f"{top['board_linked_share'] / allshare:.1f} times the {pct(allshare, 1)} average."),
        "points": [f"{bold(pct(top['board_linked_share'], 1))} of their foundation dollars come from funders sharing board members",
                   f"{bold(pct(allshare, 1))} across all causes. {cap(rows[1]['cause'].lower())} is next at {bold(pct(rows[1]['board_linked_share'], 1))}",
                   WHY + "board-linked support is a narrower base than revenue suggests"],
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
        "stats": stats(("$100M+ with insider deals", pct(big['business_with_insiders'])), ("Under $500K", pct(small['business_with_insiders']))),
        "headline": f"{pct(big['business_with_insiders'])} of $100M+ charities do business with their own insiders",
        "summary": (f"{pct(big['business_with_insiders'])} of $100M+ charities report business deals with board members, officers or their families, "
                    f"versus {pct(small['business_with_insiders'])} of those under $500K."),
        "points": [f"{bold(pct(small['business_with_insiders']))} of charities under $500K report such deals. Disclosed, not forbidden",
                   f"{bold(pct(big['family_or_business_ties']))} of $100M+ charities also report family or business ties among officers",
                   WHY + "Schedule L names the people and amounts. Read it before a major gift"],
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
        "stats": stats((top['cause'], pct(top['family_or_business_ties'])), (bot['cause'], pct(bot['family_or_business_ties']))),
        "headline": f"{cap(frac(top['family_or_business_ties']))} of {top['cause'].lower()} charities report family ties among leaders",
        "summary": (f"{pct(top['family_or_business_ties'])} of mid-size {top['cause'].lower()} charities report family or business ties among their leaders, "
                    f"versus {pct(bot['family_or_business_ties'])} in {bot['cause'].lower()}."),
        "points": [f"{bold(pct(top['family_or_business_ties']))} of mid-size {top['cause'].lower()} charities, {bold(pct(second['family_or_business_ties']))} in {second['cause'].lower()}",
                   f"{bold(pct(bot['family_or_business_ties']))} in {bot['cause'].lower()}, the lowest",
                   WHY + "not a red flag, but check who sets pay and approves budgets"],
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
    hi_links, lo_links = hi["links_per_1000"], lo["links_per_1000"]
    v = q1(f"SELECT count(*) AS c FROM board_interlocks WHERE relation = 'link' AND confidence = 'high' AND state = '{hi['state']}'")
    chart = state_map(rows, "links_per_1000", "Hidden board links per 1,000 charities", fmt=",.0f", scale={"scheme": "purples"},
                      tooltip_extra=[("links", "quantitative", "High-confidence links", ","), ("charities", "quantitative", "Charities with board data", ",")])
    return {
        "stats": stats((f"{hi['name']}, links per 1,000", f"{hi['links_per_1000']:.0f}"), (f"{lo['name']}, links per 1,000", f"{lo['links_per_1000']:.0f}")),
        "headline": f"{hi['name']} has the most tightly linked nonprofit boards",
        "summary": f"{hi['name']} has {hi['links_per_1000']:.0f} high-confidence hidden board links per 1,000 charities, versus {lo['links_per_1000']:.0f} in {lo['name']}.",
        "points": [f"{bold(f'{hi_links:.0f}')} hidden board links per 1,000 charities in {hi['name']}",
                   f"{bold(f'{lo_links:.0f}')} in {lo['name']}, the lowest",
                   WHY + "a few leaders shape the agenda, and references are often the same people"],
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
        "stats": stats(("Half from government", pct(top['gov_majority'])), ("Also under 3 months of cash", pct(top['gov_and_thin_cash'])), ("All charities, both", pct(allr['gov_and_thin_cash'], 1))),
        "headline": f"{cap(label(top['code']).lower())} are the most exposed to a federal funding freeze",
        "summary": (f"{pct(top['gov_majority'])} of {label(top['code']).lower()} get half their revenue from government grants, "
                    f"and {pct(top['gov_and_thin_cash'])} also hold under three months of cash."),
        "points": [f"{bold(pct(top['gov_majority']))} get half or more of their revenue from government grants",
                   f"{bold(pct(top['gov_and_thin_cash']))} also hold under three months of cash, versus {bold(pct(allr['gov_and_thin_cash'], 1))} of all charities",
                   WHY + "in a grant freeze, private bridge funding can keep doors open"],
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
    months = allr["m"]
    v = q1(f"""SELECT avg((cash * 12.0 / cy_expenses < 3)::int) AS s FROM (SELECT DISTINCT ON (ein) * FROM filing_flag WHERE form = '990' ORDER BY ein, tax_year DESC, object_id DESC) x
               WHERE cy_revenue > 0 AND cy_expenses > 0 AND cash IS NOT NULL AND ein IN (SELECT ein FROM orgs WHERE subsection = '03' AND left(ntee_cd, 3) = '{top['code']}')""")
    chart = hbar(rows[:15] + rows[-3:], "cause", "under_3_months_cash", "Share with under 3 months of cash", rule=float(allr["s"]),
                 tooltip_extra=[("median_months", "quantitative", ".1f"), ("charities", "quantitative", ",")])
    return {
        "stats": stats(("Under 3 months of cash", pct(top['under_3_months_cash'])), ("Typical charity, months of cash", f"{allr['m']:.1f}")),
        "headline": f"{cap(frac(top['under_3_months_cash']))} of {label(top['code']).lower()} hold under three months of cash",
        "summary": f"{pct(top['under_3_months_cash'])} of {label(top['code']).lower()} hold under three months of cash, among the thinnest reserves of any sub-cause.",
        "points": [f"{bold(pct(top['under_3_months_cash']))} of {label(top['code']).lower()}, {bold(pct(second['under_3_months_cash']))} of {label(second['code']).lower()}",
                   f"The typical charity holds {bold(f'{months:.1f} months')} of cash",
                   WHY + "multi-year or reserve-building gifts help most here"],
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
        "stats": stats((f"{hi['name']}, half from government", pct(hi['gov_majority'])), (f"{lo['name']}, half from government", pct(lo['gov_majority']))),
        "headline": f"{hi['name']} charities lean hardest on government grants",
        "summary": f"In {hi['name']}, {pct(hi['gov_majority'])} of charities get half or more of their revenue from government grants, versus {pct(lo['gov_majority'])} in {lo['name']}.",
        "points": [f"{bold(pct(hi['gov_majority']))} of {hi['name']} charities get half their revenue from government grants",
                   f"{bold(pct(lo['gov_majority']))} in {lo['name']}, the lowest",
                   WHY + "private donors are the backstop when federal grants stall"],
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
        "stats": stats(("Ran a deficit", pct(allr['d'])), ("Under $500K, under 3 months of cash", pct(small['under_3_months_cash'])), ("$100M+ with a deficit", pct(big['deficit']))),
        "headline": f"{pct(allr['d'])} of charities spent more than they took in last year",
        "summary": f"{pct(allr['d'])} of US charities filing a full 990 spent more than they took in during their latest year.",
        "points": [f"{bold(pct(small['deficit']))} of charities under $500K ran a deficit, {bold(pct(big['deficit']))} of $100M+ ones",
                   f"{bold(pct(small['under_3_months_cash']))} of those under $500K hold under three months of cash",
                   WHY + "two deficits in a row with thin cash is the warning sign"],
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
    bot_med = bot["median_funders"]
    v = q1(f"""SELECT avg((x.n = 1)::int) AS s, count(*) AS c FROM (
                 SELECT g.recipient_ein, count(DISTINCT g.funder_ein) AS n FROM grants g
                 WHERE g.tax_year IN (2022, 2023, 2024) AND g.amount > 0 AND g.recipient_ein IN (
                   SELECT o.ein FROM orgs o JOIN financials_latest f USING (ein) WHERE o.ntee_cd LIKE '{top['major']}%' AND o.ntee_cd ~ '^[A-Z][0-9]{{2}}'
                     AND o.subsection = '03' AND o.foundation NOT IN ('02','03','04') AND f.form = '990' AND f.revenue BETWEEN 250000 AND 5000000)
                 GROUP BY 1) x""")
    chart = hbar(rows, "cause", "one_funder_share", "Share of foundation-funded charities with a single foundation funder, 2022–24",
                 highlight=("major", top["major"]), tooltip_extra=[("funded_charities", "quantitative", ","), ("median_funders", "quantitative", ",")])
    return {
        "stats": stats((f"{top['cause']}, one funder", pct(top['one_funder_share'])), (f"{bot['cause']}, one funder", pct(bot['one_funder_share']))),
        "headline": f"{cap(frac(top['one_funder_share']))} of {top['cause'].lower()} charities rely on one foundation",
        "summary": (f"{pct(top['one_funder_share'])} of mid-size {top['cause'].lower()} charities with foundation support rely on a single foundation, "
                    f"versus {pct(bot['one_funder_share'])} in {bot['cause'].lower()}."),
        "points": [f"{bold(pct(top['one_funder_share']))} of mid-size {top['cause'].lower()} charities have one foundation funder",
                   f"{bold(pct(bot['one_funder_share']))} in {bot['cause'].lower()}, with a median of {bold(f'{bot_med:.0f}')} backers",
                   WHY + "if that one funder changes course, all foundation support goes"],
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
    nonhousing = next(r for r in rows if not r["code"].startswith("L") and "housing" not in label(r["code"]).lower())
    chart = hbar(rows[:15], "cause", "liabilities_exceed_assets", "Share whose liabilities exceed assets", rule=float(allr["s"]),
                 tooltip_extra=[("charities", "quantitative", ","), ("deficit", "quantitative", ".0%")])
    return {
        "stats": stats(("Owe more than they own", pct(top['liabilities_exceed_assets'])), ("All charities", pct(allr['s'])), ("Ratio", f"{top['liabilities_exceed_assets'] / allr['s']:.0f}x")),
        "headline": f"{cap(label(top['code']).lower())} most often owe more than they own",
        "summary": (f"{pct(top['liabilities_exceed_assets'])} of {label(top['code']).lower()} owe more than they own, "
                    f"{top['liabilities_exceed_assets'] / allr['s']:.0f} times the {pct(allr['s'])} rate for all charities."),
        "points": [f"{bold(pct(top['liabilities_exceed_assets']))} owe more than they own, versus {bold(pct(allr['s']))} of all charities",
                   f"Outside housing, {label(nonhousing['code']).lower()} lead at {bold(pct(nonhousing['liabilities_exceed_assets']))}",
                   WHY + "it limits borrowing, so check the balance sheet, not just income"],
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


# ================================================================ NEW SOURCES: SCHEDULE I, FEDERAL AWARDS, ACS
# Schedule I grants (charity_grant_clean, migrations 041-042), USAspending federal awards (044), Census ACS (043).

# The EPA Greenhouse Gas Reduction Fund: $20B+ of one-time FY2024 awards to six national green banks. Left out of
# place comparisons, or Maryland and DC would lead on two awards.
GGRF = "('66.957', '66.959', '66.960')"

DAF_CAUSE_SQL = """WITH panel AS (
  SELECT funder_ein FROM charity_grant_clean WHERE funder_class = 'daf_sponsor' AND tax_year IN (2022, 2024)
  GROUP BY 1 HAVING count(DISTINCT tax_year) = 2),
g AS (SELECT c.* FROM charity_grant_clean c JOIN panel USING (funder_ein) WHERE c.tax_year IN (2022, 2024))
SELECT CASE WHEN to_grantmaker THEN 'grantmakers' WHEN recipient_ein IS NULL THEN 'unlinked'
            WHEN recipient_ntee ~ '^[A-Z]' THEN left(recipient_ntee, 1) ELSE 'no_code' END AS major,
       round(sum(amount) FILTER (WHERE tax_year = 2022)) AS usd_2022,
       round(sum(amount) FILTER (WHERE tax_year = 2024)) AS usd_2024,
       (SELECT count(*) FROM panel) AS sponsors
FROM g GROUP BY 1 ORDER BY usd_2024 DESC NULLS LAST"""


@insight("flow-daf-sponsor-causes", "money-flows", 0.88, cause="T", explore="donor-advised fund sponsors")
def flow_daf_causes(ctx):
    raw = q(DAF_CAUSE_SQL)
    tot22 = sum(float(r["usd_2022"] or 0) for r in raw)
    tot24 = sum(float(r["usd_2024"] or 0) for r in raw)
    growth = tot24 / tot22 - 1
    gm = next(r for r in raw if r["major"] == "grantmakers")
    gm_g = float(gm["usd_2024"]) / float(gm["usd_2022"]) - 1
    names = {"grantmakers": "Other grantmakers (DAF sponsors, community foundations, United Ways)"}
    rows = []
    for r in raw:
        if r["major"] in ("unlinked", "no_code") or r["major"] not in MAJOR and r["major"] != "grantmakers":
            continue
        if float(r["usd_2024"] or 0) < 3e8 or not r["usd_2022"]:
            continue
        rows.append({"major": r["major"], "cause": names.get(r["major"], MAJOR.get(r["major"])),
                     "usd_2022": r["usd_2022"], "usd_2024": r["usd_2024"],
                     "growth": round(float(r["usd_2024"]) / float(r["usd_2022"]) - 1, 3)})
    rows.sort(key=lambda r: -r["growth"])
    causes = [r for r in rows if r["major"] != "grantmakers"]
    biggest = max(causes, key=lambda r: float(r["usd_2024"]))
    v = q1("""SELECT sum(c.amount) FILTER (WHERE c.tax_year = 2024) / sum(c.amount) FILTER (WHERE c.tax_year = 2022) - 1 AS g
              FROM charity_grants c JOIN charity_funder_profile fp ON fp.ein = c.funder_ein
              WHERE fp.funder_class = 'daf_sponsor' AND c.tax_year IN (2022, 2024) AND c.amount > 0 AND c.recipient_country IS NULL
                AND c.recipient_ein IN (SELECT ein FROM charity_funder_profile WHERE funder_class IN ('daf_sponsor', 'community_foundation', 'united_way'))
                AND c.funder_ein IN (SELECT funder_ein FROM charity_grant_clean WHERE funder_class = 'daf_sponsor' AND tax_year = 2022
                                     INTERSECT SELECT funder_ein FROM charity_grant_clean WHERE funder_class = 'daf_sponsor' AND tax_year = 2024)""")
    chart = diverging(rows, "cause", "growth", "Change in DAF sponsor grant dollars, 2022 to 2024 (same sponsors)")
    layer0 = chart["layer"][0] if "layer" in chart else chart
    layer0["encoding"]["tooltip"] += [{"field": "usd_2022", "type": "quantitative", "format": "$,.0f", "title": "2022"},
                                      {"field": "usd_2024", "type": "quantitative", "format": "$,.0f", "title": "2024"}]
    layer0["encoding"]["color"] = {"condition": {"test": "datum['major'] === 'grantmakers'", "value": "#d97706"}, "value": "#4c78a8"}
    return {
        "stats": stats(("DAF sponsor grants, 2022 to 2024", spct(growth)), ("To other grantmakers", spct(gm_g)),
                       (f"To {biggest['cause'].lower()}", spct(biggest['growth'])), ("Sponsors compared", n(raw[0]["sponsors"]))),
        "headline": f"DAF sponsor grants rose {pct(growth)} in two years, led by other grantmakers",
        "summary": (f"Grants from donor-advised fund sponsors rose {pct(growth)} from 2022 to 2024. Grants to other grantmakers rose {pct(gm_g)}, "
                    f"{biggest['cause'].lower()} {pct(biggest['growth'])}."),
        "points": [f"{bold(money(tot22))} to {bold(money(tot24))} from the same {n(raw[0]['sponsors'])} sponsors, {bold(spct(growth))}",
                   f"Grants to other grantmakers {bold(spct(gm_g))}. {biggest['cause']}, the biggest cause, {bold(spct(biggest['growth']))}",
                   WHY + "more DAF money passes through a second grantmaker before reaching a charity"],
        "chart": chart,
        "method": ("Form 990 Schedule I grants from donor-advised fund sponsors (charity_funder_profile.funder_class = daf_sponsor: holds DAFs and DAF grants are half or more of its Schedule I grants) "
                   "that filed for both tax years 2022 and 2024. US recipients, no private foundations. Grouped by the recipient's NTEE major group, with grants to other DAF sponsors, community "
                   "foundations and United Ways shown apart. Chart: causes with $300M+ in 2024."),
        "caveats": ("Schedule I lists only grants over $5,000 per recipient. Recipients are linked to an EIN for most dollars, and unlinked grants count in the total but not in any cause. "
                    "Tax year 2025 is incomplete and left out. Grants between sponsors count twice across sponsors, so the total overstates what reaches charities."),
        "sources": [SRC["990i"], SRC["bmf"], SRC["ntee"]],
        "query": DAF_CAUSE_SQL,
        "data": rows,
        "org_eins": None,
        "checks": [("grantmaker growth recount", close(v["g"], gm_g, abs_=0.01)), ("sponsors >= 300", int(raw[0]["sponsors"]) >= 300),
                   ("causes >= 10", len(causes) >= 10)],
    }


LOCAL_SQL = """WITH g AS (
  SELECT CASE funder_class WHEN 'community_foundation' THEN 'Community foundations' WHEN 'daf_sponsor' THEN 'DAF sponsors'
                           WHEN 'united_way' THEN 'United Ways' ELSE 'Other charities' END AS funder_type,
         funder_ein, funder_state, recipient_state, amount
  FROM charity_grant_clean WHERE tax_year BETWEEN 2022 AND 2024 AND NOT to_grantmaker
  UNION ALL
  SELECT 'Private foundations', funder_ein, funder_state, recipient_state, amount
  FROM grant_clean WHERE tax_year BETWEEN 2022 AND 2024)
SELECT funder_type, count(DISTINCT funder_ein) AS funders, round(sum(amount)) AS usd_2022_24,
       round(sum(amount) FILTER (WHERE recipient_state = funder_state) / sum(amount), 3) AS in_state_share
FROM g WHERE funder_state IS NOT NULL GROUP BY 1 ORDER BY in_state_share DESC"""


@insight("flow-community-vs-private", "money-flows", 0.84, explore="community foundations")
def flow_community_vs_private(ctx):
    rows = q(LOCAL_SQL)
    by = {r["funder_type"]: r for r in rows}
    cf, pf, daf, uw = by["Community foundations"], by["Private foundations"], by["DAF sponsors"], by["United Ways"]
    v = q1("""SELECT sum(amount) FILTER (WHERE recipient_state = funder_state)::float8 / sum(amount) AS s FROM charity_grant_clean
              WHERE funder_class = 'community_foundation' AND tax_year IN (2022, 2023, 2024) AND NOT to_grantmaker AND funder_state IS NOT NULL""")
    chart = hbar(rows, "funder_type", "in_state_share", "Share of grant dollars to recipients in the funder's own state, 2022–24",
                 highlight=("funder_type", "Community foundations"),
                 tooltip_extra=[("funders", "quantitative", ","), ("usd_2022_24", "quantitative", "$,.0f")])
    return {
        "stats": stats(("Community foundations, in state", pct(cf["in_state_share"])), ("Private foundations, in state", pct(pf["in_state_share"])),
                       ("DAF sponsors, in state", pct(daf["in_state_share"]))),
        "headline": f"Community foundations keep {pct(cf['in_state_share'])} of grants in state, DAF sponsors {pct(daf['in_state_share'])}",
        "summary": (f"Community foundations granted {pct(cf['in_state_share'])} of their 2022–24 dollars in their own state, private foundations "
                    f"{pct(pf['in_state_share'])} and DAF sponsors {pct(daf['in_state_share'])}."),
        "points": [f"{bold(pct(cf['in_state_share']))} of community foundation dollars stay in state, {bold(money(cf['usd_2022_24']))} in 2022–24",
                   f"Private foundations {bold(pct(pf['in_state_share']))}, DAF sponsors {bold(pct(daf['in_state_share']))}, United Ways {bold(pct(uw['in_state_share']))}",
                   WHY + "local causes should look to community foundations and United Ways first"],
        "chart": chart,
        "method": ("Grants for tax years 2022–24 to US organizations (no private foundations). Public charities from Form 990 Schedule I, by funder type (charity_funder_profile.funder_class), "
                   "leaving out grants to other DAF sponsors, community foundations and United Ways. Private foundations from Form 990-PF (grant_clean). In state = the recipient's address is in the funder's state."),
        "caveats": (ctx.addr + " Schedule I lists only grants over $5,000 per recipient, while 990-PF lists every grant. Community foundations include donor-advised funds they hold, "
                    "so part of their giving is recommended by donors."),
        "sources": [SRC["990i"], SRC["pf"], SRC["bmf"]],
        "query": LOCAL_SQL,
        "data": rows,
        "org_eins": None,
        "checks": [("community recount", close(v["s"], cf["in_state_share"], abs_=0.002)), ("community funders >= 1,000", int(cf["funders"]) >= 1000),
                   ("five funder types", len(rows) == 5)],
    }


FED_STATE_SQL = f"""WITH pop AS (SELECT state, left(min(county_fips), 2)::int AS id, sum(population) AS population
             FROM county_need WHERE county_fips <> '00000' GROUP BY state),
fa AS (SELECT pop_state AS state, sum(amount) AS amount, count(DISTINCT recipient_name) AS recipients FROM federal_awards
       WHERE fiscal_year BETWEEN 2022 AND 2025 AND pop_state IS NOT NULL AND cfda_number NOT IN {GGRF} GROUP BY 1)
SELECT pop.state, pop.id, pop.population, fa.recipients, round(fa.amount) AS amount_fy2022_25,
       round(fa.amount / pop.population / 4) AS per_resident_year
FROM pop JOIN fa USING (state) ORDER BY per_resident_year"""


@insight("gap-federal-per-resident", "funding-gaps", 0.8, explore="nonprofits with federal grants")
def gap_federal_per_resident(ctx):
    rows = q(FED_STATE_SQL)
    for r in rows:
        r["name"] = STATE_NAMES.get(r["state"], r["state"])
    states = [r for r in rows if r["state"] != "DC"]
    lo, lo2, hi = states[0], states[1], states[-1]
    ratio = hi["per_resident_year"] / lo["per_resident_year"]
    nat = q1(f"""SELECT sum(amount) / (SELECT sum(population) FROM county_need WHERE county_fips <> '00000') / 4 AS pc FROM federal_awards
                 WHERE fiscal_year BETWEEN 2022 AND 2025 AND pop_state IS NOT NULL AND cfda_number NOT IN {GGRF}""")["pc"]
    v = q1(f"""SELECT sum(amount) / (SELECT sum(population) FROM county_need WHERE state = '{lo['state']}' AND county_fips <> '00000') / 4 AS pc
               FROM federal_awards WHERE pop_state = '{lo['state']}' AND fiscal_year IN (2022, 2023, 2024, 2025) AND cfda_number NOT IN {GGRF}""")["pc"]
    chart = state_map(rows, "per_resident_year", "Federal grants to nonprofits, $ per resident per year", fmt="$,.0f",
                      scale={"type": "log", "scheme": "purples"}, tooltip_extra=[("amount_fy2022_25", "quantitative", "FY2022–25", "$,.0f")])
    return {
        "stats": stats((f"{lo['name']}, per resident", money(lo['per_resident_year'])), (f"{hi['name']}, per resident", money(hi['per_resident_year'])),
                       ("US, per resident", money(nat)), ("Gap", f"{ratio:.0f}x")),
        "headline": f"{hi['name']} gets {ratio:.0f} times {lo['name']}'s federal nonprofit grants per resident",
        "summary": (f"Federal grants to nonprofits average {money(lo['per_resident_year'])} per resident a year where the work happens in {lo['name']}, "
                    f"{money(hi['per_resident_year'])} in {hi['name']}."),
        "points": [f"{bold(money(lo['per_resident_year']))} per resident a year in {lo['name']}, {bold(money(lo2['per_resident_year']))} in {lo2['name']}, the lowest",
                   f"{bold(money(hi['per_resident_year']))} in {hi['name']}, home to large research universities. US average {bold(money(nat))}",
                   WHY + "research money clusters, so per-resident federal support is uneven"],
        "chart": chart,
        "method": ("USAspending grants and cooperative agreements to nonprofits and private colleges, federal fiscal years 2022–25 (net obligations), summed by place of performance state, "
                   "divided by 2024 Census population and by 4 years. The EPA Greenhouse Gas Reduction Fund ($22B of one-time FY2024 awards to six national funds) is left out. Colors on a log scale."),
        "caveats": ("Direct (prime) federal awards only: money passed through state governments, contracts, loans and grants to public universities and governments are not counted. "
                    "Awards with no single state of performance (multi-state, abroad) are left out. Research grants follow universities and medical centers."),
        "sources": [SRC["usaspending"], SRC["pop"]],
        "query": FED_STATE_SQL,
        "data": rows,
        "org_eins": None,
        "checks": [(f"{lo['state']} recount", close(v, lo["per_resident_year"], rel=0.01, abs_=1)), ("50+ states", len(rows) >= 50)],
    }


POVERTY_BAND = """CASE WHEN a.poverty_rate < 10 THEN '1 Under 10%' WHEN a.poverty_rate < 15 THEN '2 10–15%' WHEN a.poverty_rate < 20 THEN '3 15–20%'
            WHEN a.poverty_rate < 25 THEN '4 20–25%' ELSE '5 25% or more' END"""
POVERTY_SOURCES_SQL = f"""WITH c AS (
  SELECT a.county_fips, a.people_in_poverty AS poor, {POVERTY_BAND} AS band
  FROM county_acs a WHERE a.level = 'county' AND a.people_in_poverty > 0),
pf AS (SELECT county_fips, sum(amount) AS usd FROM grant_clean WHERE tax_year BETWEEN 2022 AND 2024 GROUP BY 1),
ci AS (SELECT county_fips, sum(amount) AS usd FROM charity_grant_clean WHERE tax_year BETWEEN 2022 AND 2024 AND NOT to_grantmaker GROUP BY 1),
fe AS (SELECT pop_county_fips AS county_fips, sum(amount) AS usd FROM federal_awards
       WHERE fiscal_year BETWEEN 2022 AND 2024 AND cfda_number NOT IN {GGRF} GROUP BY 1)
SELECT c.band, count(*) AS counties, sum(c.poor) AS people_in_poverty,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY coalesce(pf.usd, 0) / c.poor / 3)::numeric, 2) AS foundations,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY coalesce(ci.usd, 0) / c.poor / 3)::numeric, 2) AS public_charities,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY coalesce(fe.usd, 0) / c.poor / 3)::numeric, 2) AS federal
FROM c LEFT JOIN pf USING (county_fips) LEFT JOIN ci USING (county_fips) LEFT JOIN fe USING (county_fips)
GROUP BY 1 ORDER BY 1"""


@insight("gap-poverty-federal-vs-foundation", "funding-gaps", 0.86, explore="anti-poverty programs in high-poverty counties")
def gap_poverty_sources(ctx):
    rows = q(POVERTY_SOURCES_SQL)
    lo, hi = rows[0], rows[-1]
    for r in rows:
        r["band"] = strip_band(r["band"])
    pf_drop = float(lo["foundations"]) / float(hi["foundations"])
    v = q1(f"""WITH c AS (SELECT county_fips, people_in_poverty AS poor FROM county_acs WHERE level = 'county' AND people_in_poverty > 0 AND poverty_rate >= 25),
               fe AS (SELECT pop_county_fips AS county_fips, sum(amount) AS usd FROM federal_awards
                      WHERE fiscal_year IN (2022, 2023, 2024) AND cfda_number NOT IN {GGRF} AND pop_county_fips IN (SELECT county_fips FROM c) GROUP BY 1),
               pf AS (SELECT county_fips, sum(amount) AS usd FROM grant_clean WHERE tax_year IN (2022, 2023, 2024) AND county_fips IN (SELECT county_fips FROM c) GROUP BY 1)
               SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY coalesce(fe.usd, 0) / c.poor / 3) AS fed,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY coalesce(pf.usd, 0) / c.poor / 3) AS pf, count(*) AS n
               FROM c LEFT JOIN fe USING (county_fips) LEFT JOIN pf USING (county_fips)""")
    long = [{"band": r["band"], "source": s, "per_person": float(r[k])} for r in rows
            for s, k in (("Private foundations (990-PF)", "foundations"), ("Public charities (Schedule I)", "public_charities"), ("Federal awards", "federal"))]
    chart = vbar(long, "band", "per_person", "County poverty rate (ACS 2020–24)", "Median county: $ per person in poverty per year",
                 fmt="$,.0f", sort=None, xoffset="source", color_title=None)
    return {
        "stats": stats(("Foundations, poorest counties", money(hi['foundations'])), ("Foundations, least-poor counties", money(lo['foundations'])),
                       ("Federal, poorest counties", money(hi['federal'])), ("Federal, least-poor counties", money(lo['federal']))),
        "headline": f"Foundation money per poor person falls {pf_drop:.0f}-fold with poverty, federal money barely moves",
        "summary": (f"In typical 25%+ poverty counties, foundations give {money(hi['foundations'])} per poor person a year, versus {money(lo['foundations'])} "
                    f"under 10%. Federal: {money(hi['federal'])} versus {money(lo['federal'])}."),
        "points": [f"Foundations: {bold(money(lo['foundations']))} per poor person a year under 10% poverty, {bold(money(hi['foundations']))} at 25%+",
                   f"Federal awards: {bold(money(lo['federal']))} versus {bold(money(hi['federal']))}, close to flat",
                   WHY + "in the poorest counties federal grants outweigh foundation dollars"],
        "chart": chart,
        "method": ("Counties grouped by ACS 2020–24 poverty rate. For each county: 2022–24 990-PF grants (grant_clean) and Schedule I grants from public charities (charity_grant_clean, "
                   "leaving out grants to other grantmakers) placed by recipient ZIP, and FY2022–24 federal grants to nonprofits placed by county of performance (Greenhouse Gas Reduction Fund left out), "
                   "each divided by people in poverty and 3 years. Bars show each group's median county."),
        "caveats": (ctx.addr + " Federal awards with no county of performance (statewide or multi-state, about $100B) are left out, so federal figures are floors. "
                    "Federal fiscal years differ from tax years. Schedule I lists only grants over $5,000 per recipient."),
        "sources": [SRC["pf"], SRC["990i"], SRC["usaspending"], SRC["acs"], SRC["zcta"]],
        "query": POVERTY_SOURCES_SQL,
        "data": rows,
        "org_eins": None,
        "checks": [("25%+ federal recount", close(v["fed"], hi["federal"], rel=0.01, abs_=0.5)),
                   ("25%+ foundation recount", close(v["pf"], hi["foundations"], rel=0.01, abs_=0.5)),
                   ("100+ counties per band", min(int(r["counties"]) for r in rows) >= 100)],
    }


FED_990_SQL = f"""WITH ff AS (
  SELECT DISTINCT ON (ein) ein, government_grants FROM filing_flag WHERE form = '990' AND tax_year = 2024 ORDER BY ein, object_id DESC),
fe AS (SELECT recipient_ein AS ein, sum(amount) AS fed FROM federal_awards
       WHERE fiscal_year = 2024 AND recipient_ein IS NOT NULL AND cfda_number NOT IN {GGRF} GROUP BY 1)
SELECT CASE WHEN o.ntee_cd ~ '^[A-Z]' THEN left(o.ntee_cd, 1) ELSE '?' END AS major, count(*) AS orgs,
       round(sum(fe.fed)) AS federal_fy2024, round(sum(coalesce(ff.government_grants, 0))) AS gov_grants_990_2024,
       round(sum(fe.fed) / nullif(sum(coalesce(ff.government_grants, 0)), 0), 3) AS federal_share,
       round(avg((fe.fed > coalesce(ff.government_grants, 0))::int), 3) AS federal_above_990
FROM fe JOIN ff USING (ein) JOIN orgs o USING (ein)
GROUP BY 1 ORDER BY gov_grants_990_2024 DESC"""


@insight("stress-federal-vs-990-government", "financial-stress", 0.78, explore="nonprofits funded by federal grants")
def stress_federal_vs_990(ctx):
    raw = q(FED_990_SQL)
    fed = sum(float(r["federal_fy2024"]) for r in raw)
    gov = sum(float(r["gov_grants_990_2024"]) for r in raw)
    orgs = sum(int(r["orgs"]) for r in raw)
    above = sum(float(r["federal_above_990"]) * int(r["orgs"]) for r in raw) / orgs
    rows = [dict(r, cause=MAJOR.get(r["major"], "No cause code")) for r in raw if int(r["orgs"]) >= 100 and r["major"] != "?"]
    rows.sort(key=lambda r: float(r["federal_share"] or 0))
    v = q1(f"""SELECT sum(fe.fed) AS fed, sum(coalesce(ff.government_grants, 0)) AS gov, count(*) AS n FROM
               (SELECT recipient_ein AS ein, sum(amount) AS fed FROM federal_awards WHERE fiscal_year = 2024 AND recipient_ein IS NOT NULL AND cfda_number NOT IN {GGRF} GROUP BY 1) fe
               JOIN LATERAL (SELECT government_grants FROM filing_flag x WHERE x.ein = fe.ein AND x.form = '990' AND x.tax_year = 2024 ORDER BY x.object_id DESC LIMIT 1) ff ON true
               JOIN orgs o ON o.ein = fe.ein""")
    chart = hbar(rows, "cause", "federal_share", "Direct federal awards (FY2024) as a share of government grants on the 990 (2024)",
                 rule=fed / gov, tooltip_extra=[("orgs", "quantitative", ","), ("federal_fy2024", "quantitative", "$,.0f"),
                                                ("gov_grants_990_2024", "quantitative", "$,.0f"), ("federal_above_990", "quantitative", ".0%")])
    low = rows[0]
    return {
        "stats": stats(("Direct federal awards, FY2024", money(fed)), ("Government grants on the 990, 2024", money(gov)), ("Federal share", pct(fed / gov)),
                       ("Recipients compared", n(orgs))),
        "headline": f"Direct federal grants are {frac(fed / gov)} of the government grants charities report",
        "summary": (f"For {n(orgs)} federal grantees with a 2024 Form 990, FY2024 direct federal awards were {money(fed)}, against {money(gov)} "
                    f"they reported as government grants."),
        "points": [f"{bold(money(fed))} in direct federal awards versus {bold(money(gov))} on line 1e, {bold(pct(fed / gov))}",
                   f"{bold(pct(above))} report less on line 1e than their FY2024 federal awards",
                   WHY + "most government money reaches charities through states and cities"],
        "chart": chart,
        "method": ("Nonprofits linked to USAspending grants and cooperative agreements in federal fiscal year 2024 (Greenhouse Gas Reduction Fund left out) that filed a full Form 990 for tax year 2024: "
                   "sum of their federal awards versus Part VIII line 1e (government grants), by NTEE major group (100+ organizations). Dashed line = all."),
        "caveats": ("Line 1e counts federal, state and local grants together, including federal money passed through states. Federal fiscal years (October to September) differ from tax years. "
                    "USAspending amounts are net obligations, not cash received. Federal awards are linked to 990 filers by name and place, so a few links are wrong."),
        "sources": [SRC["usaspending"], SRC["990"], SRC["ntee"]],
        "query": FED_990_SQL,
        "data": rows,
        "org_eins": None,
        "checks": [("federal recount", close(v["fed"], fed, rel=0.001)), ("990 recount", close(v["gov"], gov, rel=0.001)),
                   ("orgs >= 5,000", orgs >= 5000), ("lowest group n >= 100", int(low["orgs"]) >= 100)],
    }


# ---------------------------------------------------------------- write
COLS = ["slug", "section", "headline", "summary", "chart", "method", "caveats", "sources", "query", "data", "org_eins",
        "explore_query", "cause", "state", "score", "verified", "stats", "points"]


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
        for w in lint(rec):
            print(f"     text: {w}")
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
