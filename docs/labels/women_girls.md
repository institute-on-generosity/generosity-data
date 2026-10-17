# Cause label: women and girls

**Question it answers:** how much US foundation money (the part visible on 990-PF returns) goes to organizations dedicated to women and girls?

**Why a label:** no single IRS/NTEE code covers it. Codes such as R24 (women's rights), P83 (women's centers), O22 (girls clubs), O42 (Girl Scouts) and S81 (women's service clubs) hold a few thousand orgs; women's funds, maternal-health programs, women's colleges, girls' sports and women's shelters sit under dozens of other codes. WPI notes this gap: "Partially due to the absence of a technical definition of giving to women and girls—such as a formal subsector classification—this area of philanthropy has not been rigorously studied."

| | |
|---|---|
| **Slug** | `women_girls` (`cause_labels`, `org_labels`, migration 032) |
| **Version** | see `LABELS['women_girls']['version']` in `etl/label_causes.py` |
| **Pipeline** | `python3 etl/label_causes.py --label women_girls all`, then `sample`/`judge` |
| **Results** | [Results](#results) below |

## Definition

Organizations whose **stated work is dedicated to women and/or girls** as the people served or advanced:
- Women's and girls' **rights and gender equity**; **women's funds**.
- **Girls' development** programs (Girls Inc., Girl Scouts, Girls on the Run), girls' sports teams and leagues.
- **Women's, maternal and reproductive health** (family planning and pregnancy care, Planned Parenthood, maternal-health programs).
- **Services designed for women:** women's shelters, domestic-violence services that say they serve women, housing for women, pregnancy and new-mother support.
- Women's **economic security, leadership, professions and education:** women's colleges, girls' schools, scholarship funds for women, women's business centers, women's professional societies.
- YWCAs and women's history or women-in-the-arts organizations.

## Counts / does not count

| Counts | Does not count |
|---|---|
| Ms. Foundation, Global Fund for Women, local women's funds | Orgs listing women among many populations |
| Girls Inc., Girl Scouts, girls' softball leagues | Orgs founded or led by women with no women focus |
| YWCAs, Dress for Success, women's shelters | Catholic sisters' orders serving the general public |
| Planned Parenthood affiliates, pregnancy centers, doula and maternal programs | Ladies' auxiliaries supporting a fire company, veterans post or hospital |
| Women's colleges, girls' schools, AAUW, Society of Women Engineers | League of Women Voters (civic, open to all) |
| | General health, family or youth services without a gender focus |

## Tiers

| Tier | Rule | Examples |
|---|---|---|
| **primary** | Women and/or girls are the **main intended beneficiaries** (most of the mission and programs) | Girls Inc., YWCA, women's shelter, women's fund |
| **partial** | Women or girls are an **explicitly named focus alongside others** | Boys & Girls Clubs (co-ed youth); domestic-violence programs for "survivors and families" without naming women; a homeless agency with a women's shelter program; women's collectives doing general community work (Junior Leagues, sororities, women's service clubs) |
| **not** | Absent, one of a long list, or only implied | |

**Mixed cases (decisions):**
- **Boys & Girls Clubs = partial.** WPI's index excludes them ("the majority of their programming is designed for children of all genders"); we keep them visible as partial, never primary.
- **Women's collectives** (Junior League, women's clubs, sororities) = partial. WPI counts "collectives of women serving general purposes"; we put them in partial because their programs serve the community at large.
- **Reproductive health = primary** when patients are mainly women (Planned Parenthood, family-planning clinics), following WPI, which names Planned Parenthood as an example of an org "dedicated to serving primarily women and girls."
- **Domestic violence:** primary when the record says it serves women; partial when it serves "individuals and families."

Dollar totals are reported for **primary** and **primary + partial** separately. Primary is the closest match to WPI's "dedicated" test (about 80% or more of programs).

## Method

1. **Candidates** (`label_candidates`): 501(c)(3) and (c)(4) orgs from any of:
   - **NTEE:** R24, P83, O22, O23, O42, P27, S81, V32, E40, E42, P43, P47.
   - **Keywords** on the name (women, girl, female, maternal, mothers, ladies, sorority, YWCA, doula, pregnancy …) and on mission + programs (women, girls, female, maternal, pregnant, domestic violence, gender equity, postpartum …). Exact regexes in `LABELS['women_girls']`.
   - **Embeddings:** the 6,000 filings closest to a women-and-girls query and the 6,000 closest to the centroid of 9 seed orgs (Girls Inc., Ms. Foundation, Global Fund for Women, Dress for Success, Equality Now, YWCA USA, Every Mother Counts, Girls Who Code, Women's Funding Network).
2. **Classifier:** Claude Haiku 5.5 (high effort, Batch API, 15 orgs a call, structured output): verbatim quote, tier, confidence, ≤ 25-word evidence. Name only for orgs without an e-filed 990 (NTEE alone counts only for R24, P83, O22, O42).
3. **Guards:** the quote must be taken from the record or the name must match the label keywords. **Confidence:** primary needs ≥ 0.8 (else partial), partial ≥ 0.5 (else not); set on samples g2–g3, checked on fresh g4. **Co-ed rule:** a partial whose only evidence is "boys and girls" (youth leagues, co-ed schools) is not, unless the name says girls (Boys & Girls Clubs stay partial).
4. **Top-100 dollar review:** the 100 largest recipients re-graded by Claude Opus 5.5; 16 disagreements accepted after reading (Planned Parenthood Federation, Abortion Care Network, EngenderHealth, The 19th → primary; fiscal sponsors and broad agencies such as NYFA and Management Sciences for Health → not). `etl/labels/overrides-women_girls.json`.
5. **Store and grade:** as for climate (see [climate.md](climate.md#method)). Recall sample: non-candidates coded P, O, R, E, B, S, Q, L or X. Bars: primary ≥ 95%, partial ≥ 90%.

### Prompt rules added after grading (version .6)

- Primary needs women/girls to be the whole or clear majority of the mission; multi-purpose institutions (universities, hospitals, community foundations, fiscal sponsors) are not unless the mission statement names women or girls.
- League of Women Voters and YMCAs: not. Women's professional and business associations, all-female clubs and teams, girls' sports teams and boosters: primary.
- Reproductive health, family planning, abortion access and pregnancy centers: primary; partial when the record says the service is for both women and men.
- A general clinic or job program listing women among several populations: not. Name-only records: a women's club, sisterhood or sorority name is partial; a name for mothers, widows, girls or women's empowerment is primary.

## Prior art

- **Women's Philanthropy Institute, Women & Girls Index (WGI):** keyword rules on names and missions, not NTEE. "Inclusionary words and phrases (key words), exclusionary words and phrases (stop words)… Initial sets of rules were first applied to organizations' names, mission statements." Validation: "a minimum of 100 organizations were hand-checked to confirm the accuracy of at least 80%." [WGI 2020 report](https://cdn.givingcompass.org/wp-content/uploads/2020/12/08085832/WGI-2020-Report-FINAL.pdf). Results: 1.6% of US giving in 2017; "peaking at 2.18% in 2022 before settling back to 2.04% in 2023" ([2025 release](https://philanthropy.indianapolis.iu.edu/news-events/news/_news/2025/women-and-girls-2025.html)). WGI measures all giving (mostly individual) received by orgs; we measure foundation grants only, so the shares are not directly comparable.
- **Candid PCS population codes:** recipients are coded for "the populations who are consistently served or impacted on an ongoing basis by the organization's programs" ([PCS FAQ](https://taxonomy.candid.org/resources/pcs-faq)). Our primary tier is the stricter "main beneficiaries" test.

## Known limits

- **Organization, not grant:** a grant to a women's program inside a co-ed org is missed; any grant to a partial org counts in full under primary + partial.
- **Foundation grants only;** individual and corporate giving outside foundations isn't visible, and tax data covers itemizers only.
- **Linked grants only:** about 64% of grants (55–57% of dollars) link to a recipient EIN.
- **Name-only judgments** for orgs without an e-filed 990 (small orgs).
- **Single-sex institutions** (women's colleges, girls' schools) are primary even though grants may fund buildings or general operations.

## Results (version .8, Oct 16, 2026)

| | |
|---|---|
| Candidates | 70,942 (NTEE 8,254, name keywords 35,645, text keywords 34,339, embeddings 6,000 + 6,000) |
| **Labeled** | **13,602 primary, 30,515 partial** (4,863 primary and 7,125 partial got a linked foundation grant in 2022–24) |
| **Primary precision** | **97%** (65/66, fresh sample g4; 95% CI 92–100%); 100% of the top-100 recipients' primary dollars |
| **Partial precision** (in the label) | **92%** weighted by candidate source (72/76 raw, 95%); tier exact only 48–58%: many partials are true primaries, so primary dollars are a floor |
| **Recall (est.)** | **92%** within likely NTEE groups: 8/60 rejected candidates (mostly name-only clubs and sisterhoods) and 0/150 non-candidates were in the label |
| Earlier versions | .5: primary 86%, partial 82% (g1); .6: 81%, 94% (g2, mother-daughter leagues called primary); .7: 100%, 86% (g3, co-ed youth sports called partial) |

**Foundation grants 2022–24, linked to a public charity** ($171.6B denominator):

| | Dollars | Share | Orgs |
|---|---|---|---|
| **Primary** | **$2.84B** | **1.65%** | 4,861 |
| Primary + partial | $5.91B | 3.45% | 11,931 |

- **By year (primary share):** 2022 1.67%, 2023 1.56%, 2024 1.74%.
- **Top funders (primary):** Susan Thompson Buffett Foundation $366M, Ford $72M, Gates $72M, Hewlett $52M, Pivotal Philanthropies $50M, Robert Wood Johnson $32M. Top 10 = 27% of primary dollars.
- **Top recipients:** Planned Parenthood Federation $231M, NAF Hotline Fund $180M, Barnard $66M, Wellesley $49M, Global Fund for Women $43M, Spelman $42M; partial: Texas Children's Hospital $87M, Boys & Girls Clubs of America $75M, Johns Hopkins (Jhpiego) $66M.
- **By recipient state (primary):** NY $643M (2.3% of NY's linked dollars), DC $425M (3.7%), CA $313M, TX $147M.
- **Compared with WPI:** WPI's index found women's and girls' organizations got about 2% of all US giving in 2022–23. Our 1.65% is foundation grants only, to linked recipients, for primary orgs; the measures differ.
- **Insight:** `flow-women-girls-share` (NomBot Insights, money-flows).
- Grades: `data/labels/women_girls/grade-g*.md` (git-ignored), latest in `cause_labels.grading`.
