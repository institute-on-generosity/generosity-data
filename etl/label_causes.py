#!/usr/bin/env python3
"""Cause labels: tag organizations with causes no IRS/NTEE code captures (migration 032, docs/labels/).

  python3 etl/label_causes.py --label climate all             # define, candidates, classify, verify, load
  python3 etl/label_causes.py --label climate verify          # climate only: Sonnet re-reads Haiku's positives
  python3 etl/label_causes.py --label climate candidates      # (re)build candidates only
  python3 etl/label_causes.py --label climate classify        # Claude on candidates not yet classified this version
  python3 etl/label_causes.py --label climate load            # write org_labels from the classifier output
  python3 etl/label_causes.py --label climate sample --tag g2 # fresh stratified grading sample (never reuses graded EINs)
  python3 etl/label_causes.py --label climate judge --tag g2  # grade it (Claude Opus, independent prompt), print precision/recall
  python3 etl/label_causes.py --label climate summarize --tag g5 --record   # store the grade in cause_labels.grading
  python3 etl/label_causes.py --label climate report          # headline dollar answers (data/labels/<label>/report.json)
  python3 etl/label_causes.py --label climate usage           # Claude tokens and cost so far

Labels: climate, women_girls. Pipeline per label:
  1. candidates (table label_candidates): NTEE codes, keyword rules on name + mission + programs, and embedding
     similarity (nomic 512-dim, filing_latest) to a label query and to the centroid of seed exemplar orgs.
  2. classify: Claude Haiku 5.5 (high effort, Batch API, 15 orgs a call, structured output) reads each candidate's
     name, NTEE code, mission and programs and answers primary / partial / not with a verbatim quote, a confidence and
     a <= 25-word evidence string. Cached in data/labels/<label>/llm-<llm_version>.jsonl, so reruns only pay for new
     candidates. verify (labels with verify_model, i.e. climate): Claude Sonnet 5.5 re-reads every org Haiku put in
     the label and its answer decides.
  3. load: results() applies the quote guard, the per-label confidence thresholds and the co-ed rule, then the
     hand-checked overrides in etl/labels/overrides-<label>.json (top-100 recipients by dollars, re-graded by the
     judge); org_labels gets primary and partial rows, replacing the label's old rows.
  4. sample + judge: fresh random samples stratified by tier and candidate source, plus rejected candidates and
     non-candidates from likely NTEE groups for recall, graded by Claude Opus 5.5 with its own prompt. Every graded EIN
     is recorded in data/labels/<label>/graded.txt and never sampled again, so no precision is reported on a tuning
     sample. summarize --record stores the grade in cause_labels.grading. sample-top + judge + dollar-check re-grade
     the top 100 recipients by dollars.
Version = LABELS[label]['version'] (methodology) and ['llm_version'] (the classifier run whose output is used): bump
llm_version when the definition or prompt changes (forces re-classification), version for any method change.

Orgs in scope: 501(c)(3) and (c)(4) on the IRS master file. Orgs without an e-filed 990 (no mission text) are
judged on name + NTEE only. Stdlib + psql + node (etl/labels/claude.mjs, embed/embedder.mjs).
"""
import argparse
import json
import os
import random
import re
import subprocess
import sys
from collections import Counter, defaultdict

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAUDE = os.path.join(ROOT, "etl", "labels", "claude.mjs")
CLASSIFY_MODEL = "claude-haiku-5-5"
# Tier thresholds on the classifier's confidence are per label (LABELS[...]['primary_min'], ['partial_min']): primary
# below primary_min becomes partial (the org is in the label, the tier is uncertain); partial below partial_min becomes
# not. Set on grading samples g2 + g3 (version .8), checked on fresh g4.
COED = re.compile(r"\b(boys?\s*(and|&)\s*girls?|girls?\s*(and|&)\s*boys?)\b", re.I)
CLASSIFY_EFFORT = "high"  # was "low" through version 2026-10-16.5: tiers too generous on grading sample g1
JUDGE_MODEL = "claude-opus-5-5"

# ---------------------------------------------------------------- label definitions
# Long text lives in docs/labels/<label>.md; these fields are what the classifier and judge read, and what
# cause_labels stores. Keep them in sync with the doc.
LABELS = {
    "climate": {
        "name": "Climate",
        "version": "2026-10-16.9",          # methodology (.7 confidence thresholds, .8 per label + co-ed rule)
        "llm_version": "2026-10-16.6",      # classifier prompt + model run whose output is used
        "primary_min": 0.8, "partial_min": 0.65,   # used only where no verify answer exists
        # Second stage (.9): Haiku's partial tier for climate graded 87-89% on fresh samples g3/g4, under the 90% bar,
        # so a stronger model re-reads every org Haiku put in the label (any confidence) and its answer decides.
        "verify_model": "claude-sonnet-5-5",
        "definition": ("Organizations whose stated work addresses climate change: cutting greenhouse gas emissions "
                       "(clean and renewable energy, energy efficiency, electrification, clean transportation framed "
                       "around emissions, carbon removal), adapting to or building resilience against climate impacts, "
                       "climate science, and climate policy, advocacy, education, finance or justice."),
        "includes": ("Climate advocacy and policy groups (Clean Air Task Force, 350.org, Citizens' Climate), clean-energy "
                     "nonprofits (RMI, GRID Alternatives, community solar), climate research and communication (Climate "
                     "Central), climate-resilience and adaptation programs, climate funds and intermediaries "
                     "(ClimateWorks), energy-efficiency and electrification programs."),
        "excludes": ("Land, wildlife, ocean or water conservation, parks, gardens, recycling and waste, pollution and "
                     "environmental health, and general environmental education when climate or energy is not named as "
                     "a goal; disaster relief without a climate framing; orgs that only use solar panels or 'green' "
                     "buildings themselves; utility bill assistance; places named Carbon or Solar."),
        "mixed_rule": ("primary = climate or clean energy is the main stated purpose (most of the mission/programs). "
                       "partial = climate or clean energy is one explicitly named goal or program among broader work "
                       "(e.g. a broad environmental group with a climate program, a land trust that names carbon "
                       "storage or climate resilience as a goal, a community action agency with a weatherization program). "
                       "not = climate is absent, incidental or only implied."),
        "explicit": ("climate change, global warming, greenhouse gases or carbon emissions, clean or renewable energy "
                     "(solar, wind, geothermal), energy efficiency or weatherization, electrification or electric vehicles, "
                     "carbon removal or sequestration, or climate resilience/adaptation. A name word like 'Solar' or "
                     "'Energy' counts only when it clearly means clean energy work"),
        "extra": ("- 'Energy' alone is not clean energy: energy policy, energy research or economics, grid operations, "
                  "fossil-fuel or nuclear-neutral energy work, and HVAC or building science count only when the record "
                  "says clean, renewable, efficient, low-carbon or climate. Advocacy for nuclear power as clean energy counts.\n"
                  "- Energy-efficient homes built by a housing group, solar panels or EVs bought as equipment, and "
                  "'resilience' or 'sustainability' without the word climate are not.\n"),
        "strong_ntee": [],                       # codes that settle a name-only record (quote guard)
        "ntee": ["C35", "C20", "Q38"],          # any org with these codes
        "ntee_text": ["C"],                      # orgs with these code prefixes, if they have 990 text
        "name_re": r"(\mclimat|\mcarbon\M|\mrenewabl|\msolar\M|\mwind (energy|power)|clean energ|green energ|sustainable energ|"
                   r"energy (efficien|transition|futures?\M|justice|democracy|access)|decarboni|\mnet[- ]zero|zero[- ]emission|"
                   r"\melectrif|geotherm|global warming|\mcool (the )?planet|\m350\M|sunrise movement|drawdown|\mcleantech)",
        "text_re": r"(\mclimat|global warming|greenhouse gas|\mGHG\M|carbon (emission|pollution|footprint|neutral|sequest|dioxide|free|capture|removal|offset|reduction)|"
                   r"decarboni|\mnet[- ]zero|zero[- ]emission|clean energy|renewable energy|renewables\M|solar (energy|power|panel|array|install)|"
                   r"wind (energy|power|turbine)|fossil fuel|energy efficien|electric vehicle|heat pump|\melectrif|sea[- ]level rise|"
                   r"weatheriz|energy transition|clean power|clean transportation)",
        "query": ("Nonprofit organization working on climate change: reducing greenhouse gas emissions, clean and "
                  "renewable energy, energy efficiency, climate policy and advocacy, climate science, and helping "
                  "communities adapt to and build resilience against climate impacts."),
        "seeds": ["043512550", "742244146", "262303250", "223053747", "261797336", "261150699", "262948811",
                  "464773036", "260043353", "383705448", "832935439", "030359346"],
        "emb_top": 6000,
        "recall_ntee": r"^(C|U|Q|S|W|B9|K)",     # likely groups for the recall sample of non-candidates
    },
    "women_girls": {
        "name": "Women and girls",
        "version": "2026-10-16.8",          # methodology (.7 confidence thresholds, .8 per label + co-ed rule)
        "llm_version": "2026-10-16.6",      # classifier prompt + model run whose output is used
        "primary_min": 0.8, "partial_min": 0.5,
        "coed_rule": True,                  # co-ed programs that only say they serve "boys and girls" are not
        "definition": ("Organizations whose stated work is dedicated to women and/or girls as the people served or "
                       "advanced: women's and girls' rights and gender equity, women's funds, girls' development "
                       "programs, women's, maternal and reproductive health (family planning, pregnancy care), services designed for women (women's shelters, "
                       "domestic-violence services for women, pregnancy and new-mother support), women's economic "
                       "security, leadership and education (women's colleges, girls' schools, scholarships for women)."),
        "includes": ("Women's funds and foundations for women (Ms. Foundation, Global Fund for Women), Girls Inc., "
                     "Girl Scouts, YWCAs, Dress for Success, women's shelters, maternal-health programs, women's "
                     "colleges and girls' schools, family-planning and women's health clinics (Planned Parenthood), women's business centers, scholarship funds for women, women's "
                     "history and women-in-the-arts organizations."),
        "excludes": ("Orgs that list women among many populations, orgs founded or led by women without a women focus, "
                     "Catholic sisters' orders serving the general public, ladies' auxiliaries and women's clubs whose "
                     "work supports another cause (a fire company, a veterans post, a hospital, a garden), and general "
                     "health, family or youth services without a gender focus."),
        "mixed_rule": ("primary = women and/or girls are the main intended beneficiaries (most of the mission/programs). "
                       "partial = women or girls are an explicitly named focus alongside others: co-ed youth programs "
                       "serving boys and girls (Boys & Girls Clubs), family services or domestic-violence programs for "
                       "'survivors and families' without naming women, and women's collectives doing general community work "
                       "(Junior Leagues, sororities, women's service clubs). not = women/girls are absent, one of "
                       "a long list, or only implied."),
        "explicit": ("women, girls, mothers, maternal or pregnancy care, women's or reproductive health, gender equity, "
                     "or a women's institution (YWCA, women's college, sorority, women's club). Domestic-violence services "
                     "count as primary when they say they serve women, else partial"),
        "name_only": " (except R24 women's rights, P83 women's centers, O22 girls clubs, O42 Girl Scouts)",
        "extra": ("- League of Women Voters chapters and YMCAs are not. Women's professional and business associations "
                  "(women in tech, women engineers, AAUW), all-female clubs and teams, girls' sports teams and boosters "
                  "are primary.\n"
                  "- Reproductive health, family planning, abortion access and pregnancy centers are primary; partial "
                  "when the record says the service is for both women and men or for parents generally.\n"
                  "- A general health center, clinic or job program that lists women's health or women among several "
                  "services or populations is not.\n"
                  "- With no 990 text, do not use partial to express doubt: answer not unless the name itself shows "
                  "the women/girls focus (a women's club, sisterhood or sorority name is partial; a name for mothers, "
                  "widows, girls or women's empowerment is primary).\n"),
        "strong_ntee": ["R24", "P83", "O22", "O42"],
        "ntee": ["R24", "P83", "O22", "O23", "O42", "P27", "S81", "V32", "E40", "E42", "P43", "P47"],
        "ntee_text": [],
        "name_re": r"(\mwom[ae]n|\mgirls?\M|\mgirlhood|\mfemale|\mfeminis|\mmaternal|\mmaternity|\mmothers?\M|\mmoms?\M|\mladies\M|"
                   r"\msisterhood|\msorority|\mwidows?\M|\mher\M|\mshe\M|\mYWCA|\mmidwi|\mdoula|\mpregnan|\mbirth)",
        "text_re": r"(\mwom[ae]n|\mgirls?\M|\mfemale|\mmaternal|\mmothers\M|\mmoms\M|\mpregnan|domestic violence|"
                   r"intimate partner|gender equ|gender[- ]based|\msorority|\mmidwi|\mdoula|postpartum|prenatal)",
        "query": ("Nonprofit organization dedicated to women and girls: women's rights and gender equality, girls' "
                  "empowerment programs, women's health and maternal health, women's economic empowerment and "
                  "leadership, and services for women survivors of domestic violence."),
        "seeds": ["131915124", "237252609", "770155782", "134040377", "133660566", "131624103", "454102644",
                  "300728021", "411685134"],
        "emb_top": 6000,
        "recall_ntee": r"^(P|O|R|E|B|S|Q|L|X)",
    },
}

SCOPE = "o.subsection IN ('03', '04')"


# ---------------------------------------------------------------- db + io
def psql(sql):
    r = subprocess.run(["psql", DB, "-X", "-At", "-v", "ON_ERROR_STOP=1"], input=sql, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"psql failed: {r.stderr.strip()[:800]}\n--- SQL ---\n{sql[:1500]}")
    return r.stdout


def q(sql):
    sql = sql.strip().rstrip(";")
    return json.loads(psql(f"SELECT coalesce(json_agg(t), '[]') FROM ({sql}) t;"))


def lit(s):
    return "'" + str(s).replace("'", "''") + "'"


def ddir(label):
    d = os.path.join(ROOT, "data", "labels", label)
    os.makedirs(d, exist_ok=True)
    return d


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [json.loads(l) for l in fh if l.strip()]


def write_jsonl(path, rows):
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def embed_query(text):
    js = ("import { embed, toSql } from './embedder.mjs';"
          f"const [v] = await embed([{json.dumps(text)}], 'query'); console.log(toSql(v));")
    r = subprocess.run(["node", "--input-type=module", "-e", js], cwd=os.path.join(ROOT, "embed"),
                       env={**os.environ, "EMBED_PROVIDER": "local"}, capture_output=True, text=True, check=True)
    return r.stdout.strip().splitlines()[-1]


# ---------------------------------------------------------------- steps
def define(label):
    L = LABELS[label]
    psql(f"""INSERT INTO cause_labels (label, name, definition, includes, excludes, mixed_rule, version)
VALUES ({lit(label)}, {lit(L['name'])}, {lit(L['definition'])}, {lit(L['includes'])}, {lit(L['excludes'])}, {lit(L['mixed_rule'])}, {lit(L['version'])})
ON CONFLICT (label) DO UPDATE SET name = EXCLUDED.name, definition = EXCLUDED.definition, includes = EXCLUDED.includes,
  excludes = EXCLUDED.excludes, mixed_rule = EXCLUDED.mixed_rule, version = EXCLUDED.version;""")


def candidates(label):
    L = LABELS[label]
    qv = embed_query(L["query"])
    seeds = ",".join(lit(s) for s in L["seeds"])
    ntee = ",".join(lit(c) for c in L["ntee"]) or "NULL"
    ntee_text = " OR ".join(f"o.ntee_cd LIKE {lit(p + '%')}" for p in L["ntee_text"]) or "false"
    sql = f"""
CREATE UNLOGGED TABLE IF NOT EXISTS label_candidates (
  label text NOT NULL, ein char(9) NOT NULL, sources text[] NOT NULL, sim_query real, sim_seed real,
  PRIMARY KEY (label, ein));
DELETE FROM label_candidates WHERE label = {lit(label)};
SET enable_indexscan = off; SET enable_bitmapscan = off;
CREATE TEMP TABLE s AS
  SELECT f.ein, 1 - (f.embedding <=> {lit(qv)}::vector) AS sq,
         1 - (f.embedding <=> (SELECT avg(embedding) FROM filing_latest WHERE ein IN ({seeds}))) AS ss
  FROM filing_latest f JOIN orgs o USING (ein) WHERE {SCOPE};
RESET enable_indexscan; RESET enable_bitmapscan;
CREATE TEMP TABLE c (ein char(9), src text);
INSERT INTO c SELECT o.ein, 'ntee' FROM orgs o LEFT JOIN filing_latest f USING (ein)
  WHERE {SCOPE} AND (left(o.ntee_cd, 3) IN ({ntee}) OR (({ntee_text}) AND f.ein IS NOT NULL));
INSERT INTO c SELECT o.ein, 'kw_name' FROM orgs o WHERE {SCOPE} AND o.name ~* {lit(L['name_re'])};
INSERT INTO c SELECT f.ein, 'kw_text' FROM filing_latest f JOIN orgs o USING (ein)
  WHERE {SCOPE} AND (coalesce(f.mission, '') || ' ' || coalesce(f.programs, '')) ~* {lit(L['text_re'])};
INSERT INTO c SELECT ein, 'emb_query' FROM (SELECT ein FROM s ORDER BY sq DESC LIMIT {L['emb_top']}) x;
INSERT INTO c SELECT ein, 'emb_seed' FROM (SELECT ein FROM s ORDER BY ss DESC LIMIT {L['emb_top']}) x;
INSERT INTO label_candidates (label, ein, sources, sim_query, sim_seed)
  SELECT {lit(label)}, c.ein, array_agg(DISTINCT c.src ORDER BY c.src), max(s.sq), max(s.ss)
  FROM c LEFT JOIN s USING (ein) GROUP BY c.ein;
SELECT src, count(DISTINCT ein) FROM c GROUP BY 1 ORDER BY 1;
SELECT 'total', count(*) FROM label_candidates WHERE label = {lit(label)};
"""
    print(psql(sql).strip())


def org_text(rows_sql, mission_chars=500, program_chars=900):
    """Org records as the model reads them: name, other name, NTEE, place, mission, programs."""
    return q(f"""
SELECT o.ein, o.name, o.sort_name, o.ntee_cd, o.city, o.state, o.subsection,
       left(f.mission, {mission_chars}) AS mission, left(f.programs, {program_chars}) AS programs
FROM ({rows_sql}) r JOIN orgs o USING (ein) LEFT JOIN filing_latest f USING (ein)""")


NTEE = {}


def ntee_label(code):
    if not NTEE:
        import csv
        with open(os.path.join(ROOT, "etl", "ntee_codes.csv"), newline="") as fh:
            NTEE.update({r["code"]: r["label"] for r in csv.DictReader(fh)})
    code = (code or "").strip()
    return NTEE.get(code[:3]) or NTEE.get(code[:1]) or "none"


def render(o):
    lines = [f"Name: {o['name']}" + (f" (also: {o['sort_name']})" if o.get("sort_name") else ""),
             f"NTEE: {o['ntee_cd'] or 'none'} ({ntee_label(o['ntee_cd'])}); {o['city']}, {o['state']}"]
    if o.get("mission") or o.get("programs"):
        lines.append(f"Mission: {(o.get('mission') or '').strip()}")
        lines.append(f"Programs: {(o.get('programs') or '').strip()}")
    else:
        lines.append("No 990 text: judge from name and NTEE code only.")
    return "\n".join(lines)


def system_prompt(label, judge=False):
    L = LABELS[label]
    head = ("You grade a cause label on US nonprofit records for a research database. Another model already labeled "
            "these records; you decide independently what the right answer is. Be strict and careful."
            if judge else
            "You label US nonprofit records for a research database that measures how much philanthropy goes to a cause.")
    return f"""{head}

Label: {L['name']}
Definition: {L['definition']}
Counts: {L['includes']}
Does not count: {L['excludes']}
Tiers: {L['mixed_rule']}

For each <org>, answer tier = primary, partial or not, using only the record (name, NTEE code, mission, programs).
- Judge the organization's own stated purpose and programs, not its donors, location or one-off mentions.
- A record with no 990 text: label only if the name makes the focus clear; an NTEE code alone is not enough{L.get('name_only', '')}.
- Foundations and funds count when their stated grantmaking focus fits (a women's fund, a climate fund).
- primary or partial needs explicit words in the record: {L['explicit']}. If the record does not state them, the tier is not, even when the work seems related; never use partial as a hedge.
- partial needs a goal, program or service devoted to the cause. A passing mention, a framing phrase, one event or webinar, or the cause listed among many topics is not.
- primary needs the cause to be the whole or clear majority of the stated mission and programs. A group with several program areas where the cause is one of them is partial.
- Large multi-purpose institutions (universities, hospitals and health centers, community foundations, donor-advised fund and fiscal sponsors, general relief and development agencies, broad policy institutes) are not when the cause is one of many programs; partial only when the mission statement itself names the cause.
{L.get('extra', '')}- When unsure between primary and partial, choose partial. When unsure between partial and not, choose not.
{"- why: under 15 words." if judge else "- First fill quote: the exact words copied from the record (name, mission or programs, up to 12 words) that state the focus, or an empty string if there are none. An empty quote means tier = not. Then tier, confidence (0 to 1) and evidence (under 25 words: why this tier)."}
Return one result per org, with its id."""


def out_path(label):
    return os.path.join(ddir(label), f"llm-{LABELS[label]['llm_version']}.jsonl")


def verify_path(label):
    return os.path.join(ddir(label), f"verify-{LABELS[label]['llm_version']}-{LABELS[label]['verify_model']}.jsonl")


def verify(label):
    """Second stage: the verify model re-reads every org the classifier put in the label (any confidence)."""
    L = LABELS[label]
    res = {r["id"]: r for r in read_jsonl(out_path(label))}
    done = {r["id"] for r in read_jsonl(verify_path(label))}
    eins = sorted(e for e, r in res.items() if r["tier"] != "not" and e not in done)
    print(f"verify {label}: {len(eins)} orgs with {L['verify_model']}")
    rows = []
    for i in range(0, len(eins), 5000):
        rows += org_text("SELECT ein FROM orgs WHERE ein IN (%s)" % ",".join(lit(e) for e in eins[i:i + 5000]))
    if rows:
        run_claude("classify", label, [{"id": r["ein"], "text": render(r)} for r in rows], verify_path(label),
                   L["verify_model"], "low", batch=15, api="batch")


def run_claude(mode, label, items, out, model, effort, batch=25, concurrency=8, api="live"):
    d = ddir(label)
    sys_path = os.path.join(d, f"system-{mode}.txt")
    with open(sys_path, "w") as fh:
        fh.write(system_prompt(label, judge=(mode == "judge")))
    in_path = out + ".in.jsonl"
    write_jsonl(in_path, items)
    subprocess.run(["node", CLAUDE, "--mode", mode, "--system", sys_path, "--in", in_path, "--out", out,
                    "--model", model, "--effort", effort, "--batch", str(batch), "--concurrency", str(concurrency), "--api", api],
                   cwd=os.path.dirname(CLAUDE), check=True)


def classify(label, limit=None):
    out = out_path(label)
    done = {r["id"] for r in read_jsonl(out)}
    rows = org_text(f"SELECT ein FROM label_candidates WHERE label = {lit(label)}")
    todo = sorted((r for r in rows if r["ein"] not in done), key=lambda r: r["ein"])
    random.Random(7).shuffle(todo)
    if limit:
        todo = todo[:limit]
    print(f"classify {label}: {len(rows)} candidates, {len(done)} done, {len(todo)} to send")
    if todo:
        run_claude("classify", label, [{"id": r["ein"], "text": render(r)} for r in todo], out, CLASSIFY_MODEL, CLASSIFY_EFFORT, batch=15, concurrency=16,
                   api="batch" if len(todo) > 2000 else "live")


def norm(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def quote_ok(quote, text):
    """The quote is taken from the record: its words are in the text and at least one 3-word run matches exactly
    (models add or drop a 'The', fix a typo or join two phrases)."""
    qw, tw = norm(quote).split(), norm(text)
    if not qw:
        return False
    if " ".join(qw) in tw:
        return True
    words = set(tw.split())
    runs = [" ".join(qw[i:i + 3]) for i in range(len(qw) - 2)]
    return sum(w in words for w in qw) >= 0.8 * len(qw) and any(r in tw for r in runs)


def pg_re(r):
    return r.replace("\\m", "\\b").replace("\\M", "\\b")


def results(label):
    """Classifier output with the quote guard applied. primary/partial stand only when (a) the model's quote is
    taken from the record (name, mission or programs), (b) the name matches the label's name keywords, or (c) the
    org has no 990 text and its NTEE code is one of the label's strong codes (strong_ntee). Otherwise the answer becomes 'not'
    (guard = 'no-quote'): the model labeled on a hunch."""
    L = LABELS[label]
    name_re = re.compile(pg_re(L["name_re"]), re.I)
    cand = {r["ein"] for r in q(f"SELECT ein FROM label_candidates WHERE label = {lit(label)}")}
    res = {r["id"]: r for r in read_jsonl(out_path(label)) if r["id"] in cand}  # drop ids the model made up
    verified = {}
    if L.get("verify_model"):
        verified = {r["id"]: dict(r, stage1=res[r["id"]]["tier"]) for r in read_jsonl(verify_path(label)) if r["id"] in res}
        res.update(verified)
    pos = [e for e, r in res.items() if r["tier"] != "not"]
    recs = {}
    for i in range(0, len(pos), 5000):
        chunk = ",".join(lit(e) for e in pos[i:i + 5000])
        recs.update({r["ein"]: r for r in org_text(f"SELECT ein FROM orgs WHERE ein IN ({chunk})", 100000, 100000)})
    for e in pos:
        r, o = res[e], recs.get(e)
        if not o:
            ok = False
        else:
            name_hit = bool(name_re.search(o["name"] + " " + (o.get("sort_name") or "")))
            if o.get("mission") or o.get("programs"):
                ok = quote_ok(r.get("quote"), render(o)) or name_hit
            else:
                ok = name_hit or (o["ntee_cd"] or "")[:3] in L["strong_ntee"]
        if not ok:
            r["model_tier"], r["tier"], r["guard"] = r["tier"], "not", "no-quote"
            continue
        c = float(r.get("confidence") or 0)
        if e in verified:
            continue  # the second-stage answer stands as given (after the quote guard)
        if r["tier"] == "primary" and c < L["primary_min"]:
            r["model_tier"], r["tier"], r["guard"] = "primary", "partial", "confidence"
        elif r["tier"] == "partial" and c < L["partial_min"]:
            r["model_tier"], r["tier"], r["guard"] = "partial", "not", "confidence"
        elif (r["tier"] == "partial" and L.get("coed_rule") and COED.search(r.get("quote") or "")
              and not re.search(r"\bgirl", o["name"] + " " + (o.get("sort_name") or ""), re.I)):
            r["model_tier"], r["tier"], r["guard"] = "partial", "not", "coed"
    return res


def load(label):
    L = LABELS[label]
    res = results(label)
    model = "llm:" + (L.get("verify_model") or CLASSIFY_MODEL)
    for e, r in res.items():
        r["method"] = model if (L.get("verify_model") and "stage1" in r) else "llm:" + CLASSIFY_MODEL
    # Hand-checked overrides (etl/labels/overrides-<label>.json): the top recipients by dollars, re-graded by the judge
    # model with disagreements accepted after reading. They beat the classifier.
    ov_path = os.path.join(ROOT, "etl", "labels", f"overrides-{label}.json")
    for o in (json.load(open(ov_path)) if os.path.exists(ov_path) else []):
        res[o["ein"]] = {"tier": o["tier"], "confidence": 1.0, "evidence": o["why"], "method": "review:" + JUDGE_MODEL}
    rows = [{"ein": e, "tier": r["tier"], "score": max(0.0, min(1.0, float(r.get("confidence") or 0))),
             "evidence": " ".join((r.get("evidence") or "").split()[:25]), "method": r["method"]}
            for e, r in res.items() if r["tier"] in ("primary", "partial")]
    print("guards:", Counter((r["guard"], r["model_tier"], r["tier"]) for r in res.values() if r.get("guard")))
    payload = json.dumps(rows, ensure_ascii=False)
    tag = "$j$"
    assert tag not in payload
    psql(f"""BEGIN;
DELETE FROM org_labels WHERE label = {lit(label)};
INSERT INTO org_labels (ein, label, tier, score, method, evidence, version)
SELECT x.ein, {lit(label)}, x.tier, x.score, x.method, x.evidence, {lit(L['version'])}
FROM jsonb_to_recordset({tag}{payload}{tag}::jsonb) AS x(ein char(9), tier text, score real, evidence text, method text)
JOIN orgs o USING (ein);
COMMIT;""")
    print(psql(f"SELECT tier, count(*) FROM org_labels WHERE label = {lit(label)} GROUP BY 1 ORDER BY 1;").strip())
    print("classified:", Counter(r["tier"] for r in res.values()))


# ---------------------------------------------------------------- grading
def graded_eins(label):
    p = os.path.join(ddir(label), "graded.txt")
    return set(open(p).read().split()) if os.path.exists(p) else set()


def source_group(sources):
    s = set(sources)
    if "ntee" in s:
        return "ntee"
    if s & {"kw_name", "kw_text"}:
        return "keyword"
    return "embedding"


def sample(label, tag, n_tier=100, n_not=60, n_recall=150, n_top=40, seed=None):
    """Fresh stratified sample: per tier (primary, partial) n_tier orgs spread evenly over candidate source groups,
    the n_top largest recipients of linked 2022-24 foundation dollars among labeled orgs, n_not candidates the
    classifier rejected, and n_recall non-candidates from likely NTEE groups (recall)."""
    L = LABELS[label]
    seen = graded_eins(label)
    rng = random.Random(seed if seed is not None else sum(map(ord, tag)))
    res = results(label)
    cand = {r["ein"]: r for r in q(f"SELECT ein, sources FROM label_candidates WHERE label = {lit(label)}")}
    picked = []

    def take(pool, k, stratum):
        pool = [e for e in pool if e not in seen]
        rng.shuffle(pool)
        for e in pool[:k]:
            seen.add(e)
            picked.append({"ein": e, "stratum": stratum, "tier": res.get(e, {}).get("tier", "none"),
                           "group": source_group(cand[e]["sources"]) if e in cand else "non-candidate"})

    for tier in ("primary", "partial"):
        by_group = defaultdict(list)
        for e, r in res.items():
            if r["tier"] == tier and e in cand:
                by_group[source_group(cand[e]["sources"])].append(e)
        k = n_tier // max(1, len(by_group))
        for g, pool in sorted(by_group.items()):
            take(pool, k, f"{tier}/{g}")
    top = q(f"""SELECT l.ein FROM org_labels l JOIN grants g ON g.recipient_ein = l.ein
                WHERE l.label = {lit(label)} AND g.tax_year BETWEEN 2022 AND 2024 AND g.amount > 0 AND g.recipient_kind IS NULL
                GROUP BY 1 ORDER BY sum(g.amount) DESC LIMIT {n_top * 3}""")
    take_top = [r["ein"] for r in top if r["ein"] not in seen][:n_top]
    for e in take_top:
        seen.add(e)
        picked.append({"ein": e, "stratum": "top-dollars", "tier": res[e]["tier"], "group": source_group(cand[e]["sources"])})
    take([e for e, r in res.items() if r["tier"] == "not"], n_not, "rejected")
    # recall: non-candidates in likely NTEE groups, weighted toward orgs that receive foundation grants
    non = q(f"""SELECT o.ein, EXISTS (SELECT 1 FROM grants g WHERE g.recipient_ein = o.ein AND g.tax_year BETWEEN 2022 AND 2024) AS funded
                FROM orgs o WHERE {SCOPE} AND o.ntee_cd ~ {lit(L['recall_ntee'])}
                  AND NOT EXISTS (SELECT 1 FROM label_candidates c WHERE c.label = {lit(label)} AND c.ein = o.ein)""")
    take([r["ein"] for r in non if r["funded"]], n_recall // 2, "recall/funded")
    take([r["ein"] for r in non if not r["funded"]], n_recall - n_recall // 2, "recall/unfunded")
    pops = {"recall/funded": sum(r["funded"] for r in non), "recall/unfunded": sum(not r["funded"] for r in non)}
    d = ddir(label)
    with open(os.path.join(d, f"sample-{tag}.json"), "w") as fh:
        json.dump({"tag": tag, "version": L["version"], "populations": pops, "items": picked}, fh, indent=1)
    with open(os.path.join(d, "graded.txt"), "a") as fh:
        fh.write("\n".join(p["ein"] for p in picked) + "\n")
    print(f"sample {tag}: {len(picked)} orgs", Counter(p["stratum"] for p in picked))


def sample_top(label, tag, n_top=100):
    """Dollar check: the n_top largest recipients (2022-24 linked foundation dollars) among labeled orgs, all of them,
    graded again with the current judge prompt even if graded before. Summarized by dollar_check()."""
    L = LABELS[label]
    res = results(label)
    top = q(f"""SELECT l.ein, sum(g.amount) AS usd FROM org_labels l JOIN grants g ON g.recipient_ein = l.ein JOIN orgs o ON o.ein = l.ein
                WHERE l.label = {lit(label)} AND g.tax_year BETWEEN 2022 AND 2024 AND g.amount > 0 AND g.recipient_kind IS NULL
                  AND coalesce(o.foundation, '') NOT IN ('02', '03', '04')
                GROUP BY 1 ORDER BY 2 DESC LIMIT {n_top}""")
    items = [{"ein": r["ein"], "stratum": "top-dollars", "tier": res[r["ein"]]["tier"], "group": "top", "usd": r["usd"]} for r in top]
    with open(os.path.join(ddir(label), f"sample-{tag}.json"), "w") as fh:
        json.dump({"tag": tag, "version": L["version"], "populations": {}, "items": items}, fh, indent=1)
    print(f"sample {tag}: top {len(items)} recipients by dollars")


def dollar_check(label, tag):
    d = ddir(label)
    S = json.load(open(os.path.join(d, f"sample-{tag}.json")))
    J = {r["id"]: r for r in read_jsonl(os.path.join(d, f"judge-{tag}.jsonl"))}
    out = {}
    for tier in ("primary", "partial"):
        xs = [x for x in S["items"] if x["tier"] == tier and x["ein"] in J]
        ok = (lambda x: J[x["ein"]]["tier"] == "primary") if tier == "primary" else (lambda x: J[x["ein"]]["tier"] != "not")
        usd = sum(x["usd"] for x in xs)
        out[tier] = {"orgs": len(xs), "orgs_ok": sum(ok(x) for x in xs), "usd": usd, "usd_ok": sum(x["usd"] for x in xs if ok(x))}
        print(f"{tier}: {out[tier]['orgs_ok']}/{len(xs)} orgs, {100 * out[tier]['usd_ok'] / max(1, usd):.0f}% of ${usd / 1e9:.2f}B")
        for x in xs:
            if not ok(x):
                print(f"   x {x['ein']} ${x['usd'] / 1e6:.0f}M {tier} -> judge {J[x['ein']]['tier']}: {J[x['ein']]['why']}")
    with open(os.path.join(d, f"dollars-{tag}.json"), "w") as fh:
        json.dump(out, fh, indent=1)
    return out


def wilson(k, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / den
    return (c - h, c + h)


def judge(label, tag):
    d = ddir(label)
    S = json.load(open(os.path.join(d, f"sample-{tag}.json")))
    out = os.path.join(d, f"judge-{tag}.jsonl")
    eins = ",".join(lit(p["ein"]) for p in S["items"])
    rows = {r["ein"]: r for r in org_text(f"SELECT ein FROM orgs WHERE ein IN ({eins})", 800, 1500)}
    items = [{"id": p["ein"], "text": render(rows[p["ein"]])} for p in S["items"] if p["ein"] in rows]
    run_claude("judge", label, items, out, JUDGE_MODEL, "medium", batch=10, concurrency=6, api="batch")
    summarize(label, tag)


def summarize(label, tag, record=False):
    """Precision per tier and source group, misses among rejected candidates and non-candidates, a recall estimate.
    record=True stores the numbers in cause_labels.grading (only for a fresh sample of the current version)."""
    d = ddir(label)
    S = json.load(open(os.path.join(d, f"sample-{tag}.json")))
    J = {r["id"]: r for r in read_jsonl(os.path.join(d, f"judge-{tag}.jsonl"))}
    items = [dict(p, judge=J[p["ein"]]["tier"], why=J[p["ein"]]["why"]) for p in S["items"] if p["ein"] in J]
    lines = [f"# {label} grading, sample {tag} (classifier version {S['version']}, judge {JUDGE_MODEL})", ""]
    exact = lambda x: x["judge"] == x["tier"]
    inlabel = lambda x: x["judge"] in ("primary", "partial")

    def rate(xs, ok):
        k = sum(ok(x) for x in xs)
        lo, hi = wilson(k, len(xs))
        return {"k": k, "n": len(xs), "p": round(k / max(1, len(xs)), 3), "lo": round(lo, 3), "hi": round(hi, 3)}

    def fmt(r):
        return f"{r['k']}/{r['n']} = {100 * r['p']:.0f}% (95% CI {100 * r['lo']:.0f}–{100 * r['hi']:.0f}%)"

    G = {"tag": tag, "version": S["version"], "judge": JUDGE_MODEL, "classifier": CLASSIFY_MODEL}
    res = results(label)
    cand = {r["ein"]: source_group(r["sources"]) for r in q(f"SELECT ein, sources FROM label_candidates WHERE label = {lit(label)}")}
    for tier in ("primary", "partial"):
        xs = [x for x in items if x["tier"] == tier and x["stratum"] != "top-dollars"]
        G[tier] = {"exact": rate(xs, exact), "in_label": rate(xs, inlabel), "by_source": {}}
        pop = Counter(cand[e] for e, r in res.items() if r["tier"] == tier)
        for g in sorted({x["group"] for x in xs}):
            ys = [x for x in xs if x["group"] == g]
            G[tier]["by_source"][g] = {"exact": rate(ys, exact), "in_label": rate(ys, inlabel), "population": pop[g]}
        # strata are equal-sized, so weight each source group by its share of the tier's orgs
        tot = sum(pop[g] for g in G[tier]["by_source"]) or 1
        for m in ("exact", "in_label"):
            G[tier][m + "_weighted"] = round(sum(pop[g] / tot * v[m]["p"] for g, v in G[tier]["by_source"].items()), 3)
        lines.append(f"**{tier}** (random): tier exact {fmt(G[tier]['exact'])}, weighted by source {100 * G[tier]['exact_weighted']:.0f}%; "
                     f"in label (primary or partial) {fmt(G[tier]['in_label'])}, weighted {100 * G[tier]['in_label_weighted']:.0f}%")
        for g, v in G[tier]["by_source"].items():
            lines.append(f"  - {g} ({v['population']:,} orgs): exact {fmt(v['exact'])}; in label {fmt(v['in_label'])}")
    top = [x for x in items if x["stratum"] == "top-dollars"]
    if top:
        G["top_dollars"] = {"primary_exact": rate([x for x in top if x["tier"] == "primary"], exact),
                            "partial_in_label": rate([x for x in top if x["tier"] == "partial"], inlabel)}
        lines.append(f"**top dollar recipients**: primary exact {fmt(G['top_dollars']['primary_exact'])}; "
                     f"partial in label {fmt(G['top_dollars']['partial_in_label'])}")
    # recall: labeled orgs judged right vs. misses among rejected candidates and non-candidates (population-weighted)
    n_tier = Counter(r["tier"] for r in res.values())
    tp = sum(n_tier[t] * G[t]["in_label_weighted"] for t in ("primary", "partial"))
    rej = [x for x in items if x["stratum"] == "rejected"]
    G["rejected_missed"] = rate(rej, inlabel)
    fn = n_tier["not"] * G["rejected_missed"]["p"]
    lines.append(f"**rejected candidates** judged in label: {fmt(G['rejected_missed'])} of {n_tier['not']:,} (est. {fn:,.0f} missed)")
    for st in ("recall/funded", "recall/unfunded"):
        xs = [x for x in items if x["stratum"] == st]
        if xs:
            r = rate(xs, inlabel)
            G[st] = dict(r, population=S["populations"][st])
            fn += S["populations"][st] * r["p"]
            lines.append(f"**{st}** non-candidates judged in label: {fmt(r)} of population {S['populations'][st]:,} "
                         f"(est. {S['populations'][st] * r['p']:,.0f} missed; upper 95% bound {S['populations'][st] * r['hi']:,.0f})")
    G["recall_est"] = round(tp / (tp + fn), 3) if tp + fn else None
    G["counts"] = {"primary": n_tier["primary"], "partial": n_tier["partial"], "candidates": len(res)}
    lines.append(f"**recall (est.)**: {100 * (G['recall_est'] or 0):.0f}% of in-label orgs in the likely NTEE groups "
                 "(non-candidate misses at the point estimate)")
    lines.append("")
    lines.append("Disagreements:")
    for x in items:
        if (x["tier"] in ("primary", "partial") and x["judge"] != x["tier"]) or (x["tier"] in ("not", "none") and inlabel(x)):
            lines.append(f"  - [{x['stratum']}] {x['ein']} {x['tier']} -> judge {x['judge']}: {x['why']}")
    txt = "\n".join(lines)
    with open(os.path.join(d, f"grade-{tag}.md"), "w") as fh:
        fh.write(txt + "\n")
    with open(os.path.join(d, f"grade-{tag}.json"), "w") as fh:
        json.dump(G, fh, indent=1)
    print(txt)
    if record:
        assert S["version"] == LABELS[label]["version"], "sample is from an older classifier version"
        payload = json.dumps(G)
        psql(f"UPDATE cause_labels SET grading = $g${payload}$g$::jsonb WHERE label = {lit(label)};")
        print(f"recorded grading {tag} in cause_labels")


# ---------------------------------------------------------------- report
def report(label):
    lab = lit(label)
    base = f"""WITH g AS (
  SELECT g.*, l.tier FROM grants g JOIN orgs o ON o.ein = g.recipient_ein
  LEFT JOIN org_labels l ON l.ein = g.recipient_ein AND l.label = {lab}
  WHERE g.tax_year BETWEEN 2022 AND 2024 AND g.amount > 0 AND g.recipient_kind IS NULL
    AND coalesce(o.foundation, '') NOT IN ('02', '03', '04'))"""
    out = {}
    out["total"] = q(f"""{base} SELECT round(sum(amount)) AS linked, round(sum(amount) FILTER (WHERE tier = 'primary')) AS primary_usd,
        round(sum(amount) FILTER (WHERE tier IS NOT NULL)) AS any_usd, count(DISTINCT recipient_ein) FILTER (WHERE tier = 'primary') AS primary_orgs,
        count(DISTINCT recipient_ein) FILTER (WHERE tier IS NOT NULL) AS any_orgs, count(DISTINCT funder_ein) FILTER (WHERE tier IS NOT NULL) AS funders
        FROM g""")[0]
    out["by_year"] = q(f"""{base} SELECT tax_year, round(sum(amount)) AS linked, round(sum(amount) FILTER (WHERE tier = 'primary')) AS primary_usd,
        round(sum(amount) FILTER (WHERE tier IS NOT NULL)) AS any_usd FROM g GROUP BY 1 ORDER BY 1""")
    out["top_funders"] = q(f"""{base} SELECT f.name, f.state, round(sum(amount) FILTER (WHERE tier = 'primary')) AS primary_usd,
        round(sum(amount)) AS any_usd FROM g JOIN funders f ON f.ein = g.funder_ein
        WHERE tier IS NOT NULL GROUP BY 1, 2 ORDER BY primary_usd DESC NULLS LAST LIMIT 15""")
    out["top_recipients"] = q(f"""{base} SELECT o.name, o.state, g.tier, round(sum(amount)) AS usd FROM g JOIN orgs o ON o.ein = g.recipient_ein
        WHERE tier IS NOT NULL GROUP BY 1, 2, 3 ORDER BY usd DESC LIMIT 15""")
    out["by_state"] = q(f"""{base} SELECT recipient_state AS state, round(sum(amount) FILTER (WHERE tier = 'primary')) AS primary_usd,
        round(sum(amount) FILTER (WHERE tier IS NOT NULL)) AS any_usd, round(sum(amount)) AS linked FROM g
        GROUP BY 1 ORDER BY primary_usd DESC NULLS LAST LIMIT 12""")
    print(json.dumps(out, indent=1))
    with open(os.path.join(ddir(label), "report.json"), "w") as fh:
        json.dump(out, fh, indent=1)


def usage(label):
    """Token use and cost of every Claude call for this label (classifier + judge)."""
    price = {"claude-haiku-5-5": (0.10, 0.50), "claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0)}  # $/M in, out
    tot = defaultdict(lambda: [0, 0, 0])  # per model (batch calls under "<model> batch", billed at half price)
    d = ddir(label)
    for f in os.listdir(d):
        if f.endswith(".usage.jsonl"):
            for u in read_jsonl(os.path.join(d, f)):
                m = next((k for k in price if (u.get("model") or "").startswith(k)), u.get("model")) + (" batch" if u.get("batch") else "")
                t = tot[m]
                t[0] += (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
                t[1] += u.get("output_tokens") or 0
                t[2] += 1
    cost = 0.0
    for m, (i, o, n) in tot.items():
        p = price.get(m.replace(" batch", ""), (0, 0))
        p = (p[0] / 2, p[1] / 2) if m.endswith(" batch") else p
        c = i / 1e6 * p[0] + o / 1e6 * p[1]
        cost += c
        print(f"{m}: {n} calls, {i:,} in, {o:,} out, ${c:.2f}")
    print(f"total ${cost:.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True, choices=sorted(LABELS))
    ap.add_argument("step", choices=["all", "define", "candidates", "classify", "verify", "load", "sample", "sample-top", "dollar-check", "judge", "summarize", "report", "usage"])
    ap.add_argument("--tag")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--record", action="store_true", help="summarize: store this grade in cause_labels.grading")
    a = ap.parse_args()
    if a.step in ("all", "define"):
        define(a.label)
    if a.step in ("all", "candidates"):
        candidates(a.label)
    if a.step in ("all", "classify"):
        classify(a.label, a.limit)
    if a.step in ("all", "verify") and LABELS[a.label].get("verify_model"):
        verify(a.label)
    if a.step in ("all", "load"):
        load(a.label)
    if a.step == "sample":
        sample(a.label, a.tag or sys.exit("--tag"))
    if a.step == "sample-top":
        sample_top(a.label, a.tag or sys.exit("--tag"))
    if a.step == "dollar-check":
        dollar_check(a.label, a.tag)
    if a.step == "judge":
        judge(a.label, a.tag or sys.exit("--tag"))
    if a.step == "summarize":
        summarize(a.label, a.tag, a.record)
    if a.step == "report":
        report(a.label)
    if a.step == "usage":
        usage(a.label)


if __name__ == "__main__":
    main()
