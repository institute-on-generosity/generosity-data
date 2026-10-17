# Cause label: climate

**Question it answers:** how much US foundation money (the part visible on 990-PF returns) goes to organizations working on climate change?

**Why a label:** no IRS/NTEE code means "climate". The closest code, C35 (energy resources conservation), holds fewer than 800 orgs; most climate groups are coded C30, C01, C05, C99 or outside the environment group (U, Q, S, W).

| | |
|---|---|
| **Slug** | `climate` (`cause_labels`, `org_labels`, migration 032) |
| **Version** | see `LABELS['climate']['version']` in `etl/label_causes.py` (methodology changes bump it) |
| **Pipeline** | `python3 etl/label_causes.py --label climate all`, then `sample`/`judge` to grade |
| **Results** | [Results](#results) below |

## Definition

Organizations whose **stated work addresses climate change**:
- **Mitigation:** cutting greenhouse gas emissions: clean and renewable energy, energy efficiency and weatherization, electrification, clean transportation framed around emissions, carbon removal and sequestration.
- **Adaptation and resilience** to climate impacts (heat, flooding, sea-level rise) when framed as climate.
- **Climate science, policy, advocacy, education, finance and justice.**
- **Funds and intermediaries** whose stated grantmaking focus is climate (ClimateWorks Foundation).

## Counts / does not count

| Counts | Does not count (unless climate or clean energy is named as a goal) |
|---|---|
| Clean Air Task Force, 350.org, Citizens' Climate Education, Sunrise Movement | Land, wildlife, ocean, river or water conservation |
| RMI, GRID Alternatives, community solar, Clean Cities coalitions | Parks, gardens, tree planting for beauty or shade |
| Climate Central, climate research institutes | Recycling, waste, litter |
| Climate-resilience and adaptation programs | Pollution, environmental health, toxics |
| Energy-efficiency, weatherization and electrification programs | General environmental education |
| Interfaith Power & Light, climate media | Disaster relief with no climate framing |
| | Orgs that only put solar panels on their own building; utility-bill assistance; places named Carbon or Solar |

**Land conservation vs climate (decision):** land conservation alone is **not** climate. A land trust is **partial** when its record names carbon storage, climate resilience or "the impacts of climate change" as a goal (e.g. Hunterdon Land Trust). This follows the OECD Rio markers, which track biodiversity under its own marker, separate from climate. ClimateWorks counts forest protection as mitigation; our label counts it only when the record frames it that way.

## Tiers

| Tier | Rule | Analogy |
|---|---|---|
| **primary** | Climate or clean energy is the **main stated purpose**: most of the mission and programs | Rio marker "principal" |
| **partial** | Climate or clean energy is **one explicitly named goal, program or service** among broader work: a broad environmental group with a climate program, a land trust naming carbon storage, a community action agency with a weatherization program | Rio marker "significant" |
| **not** | Climate absent, implied, a passing mention, one event or webinar, or one topic in a long list | |

Dollar totals are reported for **primary** and for **primary + partial** separately. Partial dollars are an upper bound: a partial org spends only some of its money on climate (the OECD counts "significant" finance at a share, the EU at 40%). We do not apply a share.

## Method

1. **Candidates** (`label_candidates`): 501(c)(3) and (c)(4) orgs from any of:
   - **NTEE:** C35, C20, Q38 (all), and every other C code with an e-filed 990.
   - **Keywords** on the name (climate, carbon, renewable, solar, clean energy, net zero, electrify …) and on mission + programs (climate, greenhouse gas, carbon emissions, decarbonize, renewable energy, energy efficiency, heat pump, weatherization, sea-level rise …). Exact regexes in `LABELS['climate']`.
   - **Embeddings:** the 6,000 filings closest to a climate query and the 6,000 closest to the centroid of 12 seed orgs (CATF, RMI, ClimateWorks, Ceres, Climate Central, 350.org, Citizens' Climate Education, Sunrise, GRID Alternatives, Project Drawdown, Clean Energy Buyers Association, Clean Energy Group); nomic-embed-text-v1.5, 512 dims, cosine, mission + programs.
2. **Screen:** Claude Haiku 5.5 (high effort, Batch API, 15 orgs a call, structured output) reads name, NTEE code, city, mission (500 chars) and programs (900 chars) and returns a verbatim **quote**, tier, confidence and a ≤ 25-word evidence string. Records with no e-filed 990 are judged on name only.
3. **Decide:** Claude Sonnet 5.5 (low effort) re-reads every org Haiku put in the label, at any confidence, with the same prompt; its answer is final. Added in version .9 because Haiku's partial tier graded 87–89% on fresh samples, under the bar.
4. **Quote guard:** primary/partial stands only if the quote is taken from the record or the name matches the label keywords. Otherwise the answer becomes **not**: the model labeled on a hunch.
5. **Top-100 dollar review:** the 100 largest recipients by 2022–24 linked dollars were re-graded by Claude Opus 5.5; 10 disagreements accepted after reading (e.g. Windward Fund and Multiplier, multi-issue fiscal sponsors, and WWF → not; Ceres and CALSTART → primary). Stored in `etl/labels/overrides-climate.json`, `method = review:claude-opus-5-5`.
6. **Store:** `org_labels` (tier, score = confidence, method `llm:claude-sonnet-5-5`, evidence, version). Reruns pay only for new candidates (cached in `data/labels/climate/`).
7. **Grading:** fresh random samples, stratified by tier and by candidate source (NTEE, keyword, embedding only), plus the largest recipients by grant dollars, rejected candidates, and non-candidates from likely NTEE groups (C, U, Q, S, W, B9, K) for recall. Graded by Claude Opus 5.5 with its own prompt; disagreement lists reviewed. Every graded EIN goes to `data/labels/climate/graded.txt` and is never sampled again, so no precision is reported on a sample used for tuning. Bars: **primary ≥ 95%**, **partial ≥ 90%** (judge says primary or partial).

## Prior art

- **ClimateWorks Foundation, Funding Trends:** mitigation only. "Although this report focuses on funding for climate change mitigation, ClimateWorks is exploring ways to estimate philanthropic funding for adaptation." Mitigation got "less than 2% of global philanthropic giving" (2021 data); 2024 data: "about 2.1% … exceeding the 2% threshold for the first time." [2022 report](https://www.climateworks.org/wp-content/uploads/2022/10/ClimateWorks_Funding_Trends_Report_2022.pdf), [2026 report](https://www.climateworks.org/report/funding-trends-2026/). Our label is broader (adds adaptation and resilience) and narrower (forest and land work counts only when framed as climate).
- **Candid PCS subject SC02 "Climate change":** "Activities concerned with increased levels of greenhouse gases (including carbon dioxide) in the atmosphere and the resulting changes to the earth's climate." [Candid taxonomy](https://developer.candid.org/docs/candids-taxonomy). Candid codes grants by their descriptions; we code recipient organizations.
- **OECD-DAC Rio markers:** principal = "explicitly stated as fundamental in the design of, or the motivation for, the activity"; significant = "explicitly stated but it is not the fundamental driver." [Rio marker handbook](https://wwflac.awsassets.panda.org/downloads/rio_marker___revised_climate_marker_handbook_final.pdf). Our primary/partial tiers follow this split, including the "explicitly stated" test (the quote guard).

## Known limits

- **Organization, not grant:** a grant to a partial org (a university, a broad environmental group) counts in full under primary + partial, whatever its purpose. A climate grant to a non-climate org (a university's climate institute paid through the university) is missed. Grant-purpose labeling is the next step.
- **Foundation grants only:** individual, DAF-sponsor-to-charity, corporate and government money is not visible. Individual giving in tax data covers itemizers only.
- **Linked grants only:** about 64% of grants (55–57% of dollars) link to a recipient EIN; unlinked grants have no label.
- **Text coverage:** only orgs that e-file a full 990 or 990-EZ have mission text; others are judged by name and NTEE, which favors orgs with "climate" or "solar" in the name.
- **US recipients:** foreign recipients are not linkable, so international climate grants (a large share of ClimateWorks-style totals) are missing.
- **Self-description:** the label reflects what an org says on its 990, which can lag or overstate its work.

### Prompt rules added after grading (version .6)

- Primary needs the cause to be the whole or clear majority of the mission and programs; several program areas → partial.
- Multi-purpose institutions (universities, hospitals, community foundations, DAF and fiscal sponsors, relief agencies, broad policy institutes) are **not** when climate is one of many programs; partial only when the mission statement names it.
- "Energy" alone is not clean energy (energy policy, research, grid operations, fossil fuels); energy-efficient homes built by housing groups, solar panels bought as equipment, and "resilience" or "sustainability" without the word climate are not.
- A passing mention, framing phrase, single event or a topic in a long list is not.

## Results (version .9, Oct 16, 2026)

| | |
|---|---|
| Candidates | 21,611 (NTEE 10,751, name keywords 1,167, text keywords 5,040, embeddings 6,000 + 6,000) |
| **Labeled** | **1,305 primary, 1,424 partial** (572 primary and 907 partial got a linked foundation grant in 2022–24) |
| **Primary precision** | **95%** (63/66, fresh sample g5; 95% CI 88–98%); 100% of the top-100 recipients' primary dollars |
| **Partial precision** (in the label) | **95%** (63/66); tier exact 80% (most misses are true primaries) |
| **Recall (est.)** | **89%** within likely NTEE groups: 1/60 rejected candidates and 0/150 non-candidates were climate |
| Earlier versions | .4: primary 85%, partial 52% (g1); .6: 92%, 83% (g2); .7: 97%, 89% (g3); .8: 100%, 87% (g4) |

**Foundation grants 2022–24, linked to a public charity** ($171.6B denominator):

| | Dollars | Share | Orgs |
|---|---|---|---|
| **Primary** | **$2.28B** | **1.33%** | 573 |
| Primary + partial | $4.23B | 2.47% | 1,470 |

- **By year (primary share):** 2022 1.32%, 2023 1.28%, 2024 1.39%.
- **Top funders (primary):** Sergey Brin Family Foundation $263M, Bloomberg Family Foundation $195M, Hewlett $187M, Waverley Street $186M, Beneficus $146M, Sequoia Climate Foundation $129M. Top 10 = 62% of primary dollars.
- **Top recipients:** Climate Imperative Foundation $377M, United States Energy Foundation $293M, Environmental Defense Fund $285M (partial), ClimateWorks $266M, Resources Legacy Fund $185M (partial), Earthjustice $130M (partial).
- **By recipient state (primary):** CA $1.25B (5.1% of CA's linked dollars), DC $288M, NY $178M, MA $142M. Grants are placed at the recipient's address, often a headquarters.
- **Insight:** `flow-climate-share` (NomBot Insights, money-flows).
- Grades: `data/labels/climate/grade-g*.md` (git-ignored), latest in `cause_labels.grading`.
