# Cause label: substance use and opioid addiction

**Question it answers:** how much US foundation money (the part visible on 990-PF returns) goes to organizations working on alcohol and drug use, addiction and overdose?

**Why a label:** NTEE F20–F22 (substance abuse prevention and treatment) hold about 7,900 orgs, but recovery cafes, harm-reduction groups, sober-living homes, drug-free coalitions and behavioral-health agencies sit under F30, F60, F70, E, P, L, R and other codes, and many F2x orgs have no 990 text.

| | |
|---|---|
| **Slug** | `substance_use` (`LABELS['substance_use']` in `etl/label_causes.py`) |
| **Status** | **Pilot (Oct 17, 2026):** 500 candidates screened on the Claude subscription (`--api cli`). **Not loaded** into `org_labels`/`cause_labels` (`approved: False`) |
| **Pipeline** | `python3 etl/label_causes.py --label substance_use candidates`, then `classify --api cli`, then `sample`/`judge --api cli` |
| **Results** | [Pilot results](#pilot-results-oct-17-2026) below |

## Definition

Organizations whose **stated work addresses alcohol or drug use and addiction**:
- **Prevention** of substance misuse: drug-free community coalitions, youth alcohol and drug prevention, councils on alcoholism and drug dependence.
- **Treatment** of substance use disorders: detox, residential and outpatient treatment, medication for opioid use disorder (methadone, buprenorphine), drug courts.
- **Recovery support:** recovery residences and sober living, recovery community centers and recovery cafes, peer recovery coaching, 12-step fellowships and clubhouses, family support (Al-Anon, Nar-Anon).
- **Overdose prevention and harm reduction:** naloxone distribution, syringe services, drug checking.
- **Research, policy and advocacy** on addiction and drug policy; addiction medicine societies.

## Counts / does not count

| Counts | Does not count |
|---|---|
| Hazelden Betty Ford, Phoenix House, Gaudenzia, Odyssey House | Tobacco, vaping or nicotine work alone |
| Sober-living homes, recovery residences, recovery cafes | Gambling, eating, gaming or sex addiction |
| National Harm Reduction Coalition, naloxone and syringe programs | General mental health with no substance use named |
| Drug-free community coalitions (CADCA members), NCADD affiliates | DUI enforcement and traffic safety without a substance-use program |
| Partnership to End Addiction, Drug Policy Alliance, Faces & Voices of Recovery | Pharmacies, drug pricing, "drug-free" as a facility rule |
| AA intergroups and clubhouses, Al-Anon, Nar-Anon | Marijuana legalization or cannabis industry groups |
| | Homeless, re-entry or youth programs listing addiction among many needs |

## Tiers

| Tier | Rule | Examples |
|---|---|---|
| **primary** | Substance use (prevention, treatment, recovery or harm reduction) is the **main stated purpose** | treatment center, sober-living home, harm-reduction group, recovery cafe |
| **partial** | Substance use is **one explicitly named program or goal** among broader work | community mental health center with an addiction program; behavioral-health agency serving "mental health and substance use"; shelter with a recovery program; re-entry group with substance-use treatment |
| **not** | Absent, one of a long list of needs, or only implied | |

**Mixed cases (decisions):**
- **Behavioral health = partial** when the mission names both mental health and substance use; primary only when addiction is the main named focus.
- **"Recovery" alone is not enough** (disaster, economic or mental-health recovery).
- **Tobacco-only = not;** youth prevention covering alcohol and drugs as well as tobacco counts.
- **Drug courts = primary;** general legal aid or re-entry listing substance use = partial only if it names a substance-use program.

## Method

Same pipeline as [climate](climate.md#method) and [women and girls](women_girls.md#method), with these inputs:

1. **Candidates** (`label_candidates`, 22,713 orgs): NTEE F20, F21, F22, F50 (8,302); name keywords (addiction, alcohol, substance, opioid, overdose, naloxone, harm reduction, sober, detox, recovery house/cafe/community, Al-Anon, 12-step, teen challenge …; 3,703); mission + program keywords (also syringe services, fentanyl, methadone, buprenorphine, MOUD, drug court …; 12,638); embeddings: 6,000 closest to a substance-use query and 6,000 closest to the centroid of 11 seed orgs (Partnership to End Addiction, Hazelden Betty Ford, Drug Policy Alliance, Faces & Voices of Recovery, National Harm Reduction Coalition, Gaudenzia, Odyssey House, Phoenix House Foundation, Recovery Cafe, Harm Reduction Therapy Center, NCADD). Exact regexes in `LABELS['substance_use']`.
2. **Screen:** Claude Haiku 5.5, high effort, 15 orgs a call, structured output (verbatim quote, tier, confidence, ≤ 25-word evidence), **run through Claude Code on the Claude subscription** (`--api cli`, `etl/labels/claude_cli.mjs`), not the API key. Name only for orgs with no e-filed 990 (NTEE alone counts only for F20, F21, F22).
3. **Guards:** quote guard as for the other labels; confidence thresholds copied from women and girls (primary ≥ 0.8, partial ≥ 0.5), not yet tuned.
4. **Grade:** fresh samples judged by Claude Opus 5.5 (medium effort, its own prompt), also on the subscription. Recall sample: non-candidates coded F, P, E, I, K, L, O or S. Bars: **primary ≥ 95%**, **partial ≥ 90%** in the label.

## Known limits

- **Organization, not grant:** a grant to a recovery program inside a hospital or shelter counts in full under primary + partial.
- **Name-only judgments** for small orgs without an e-filed 990 (many AA clubs and sober homes).
- **Foundation grants only;** linked grants only (about 64% of grants).

## Pilot results (Oct 17, 2026)

Screen: 500 random candidates, Claude Haiku 5.5 high effort, **on the Claude subscription** (`claude -p`, `apiKeySource: none` on all 34 calls). Grade: sample p1 (182 orgs), Claude Opus 5.5 medium effort, also on the subscription.

| | |
|---|---|
| Candidates | 22,713 (NTEE 8,302, name keywords 3,703, text keywords 12,638, embeddings 6,000 + 6,000) |
| Screened (pilot) | 500: 209 primary, 107 partial, 184 not (before guards) |
| **Primary precision** | **100%** (41/41; 95% CI 91–100%) |
| **Partial precision** (in the label) | **95%** (39/41; CI 84–99%); tier exact only 46%: 20 of 22 misses are true primaries, mostly F20–F22 records with no 990 text that Haiku kept at partial |
| Rejected candidates in the label | 1/40 |
| Non-candidates in the label | 0/60 (F, P, E, I, K, L, O, S) |
| Speed | screen 5.2 min for 500 orgs (**~5,800 orgs/h**, 3 parallel calls, ~26 s a call); judge 1.0 min for 182 |
| Subscription use | 0 limit hits, 0 waits; five-hour window 39% → 41%, seven-day 77% → 77% (shared with other Claude use) |

**Before a full run:** say in the prompt that a name-only record with an F20–F22 code and a substance-use name is primary (the judge's main disagreement); grade a fresh sample of at least 100 per tier; then set `approved: True` to load. Full candidate set: ~22,200 more orgs, about 4 h on the subscription at this rate.

Grades: `data/labels/substance_use/grade-p1.md` (git-ignored).
