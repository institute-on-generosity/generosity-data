# Research layer: data dictionary

Pre-aggregated foundation-grant facts and lookups behind NomBot's research answers (`web/lib/answer.ts`).
Migrations **038** (bands, profiles), **039** (cause taxonomy), **040** (cubes, refresh function), **041-042** (Schedule I grants by charities), **043** (Census ACS by county), **044** (federal awards to nonprofits), **045** (company foundations), **046** (grant-size histograms for medians and percentiles), **048** (Schedule I coverage per return).
All of it is built from `grant_clean` (migration 033), so it holds the **same grants with the same exclusions**: sums match `grant_clean` exactly (NomBot `web/eval/answer-cube.mts` answers 51 questions both ways and compares).

**Refresh:** `SELECT refresh_research_layer();` (last step of `db/refresh.sql`, ~40 s, `CONCURRENTLY`, so readers keep the old rows). Run it after `grant_clean`, `flag_latest`, `org_labels` or `county_need` change. The 046 histograms are refreshed last and add the most time (first build of all three: 9 min 51 s, Oct 17, machine under load).

## Read this first: caveats that apply everywhere

| Caveat | What it means |
|---|---|
| **Counted where headquartered** | A grant is placed at the recipient address on the 990-PF, usually a head office. National groups' offices pull dollars to **DC** (few residents, many headquarters), New York and other hubs. DC is shown and flagged as an outlier, never dropped. |
| **Private foundations only** | 990-PF grants. Community foundations, donor-advised funds and corporate giving programs file other forms. |
| **Identified recipients** | About two thirds of grants link to an IRS-registered org (EIN). Cause, NTEE and recipient-size figures cover identified recipients only, so they run low. |
| **Tax years** | 2022 to 2025. 2025 returns are still arriving. |
| **Exclusions** | Amount above 0. No grants to individuals, placeholder lines, foreign recipients or other private foundations (same as `grant_clean`). |
| **NTEE** | The recipient's main code as the IRS assigned it. About a third of orgs have none. Multi-purpose orgs are coded once. |

## Size bands (`size_bands`, functions `revenue_band()`, `asset_band()`)

Fixed edges, so a band always means the same dollars. Answers round a requested cutoff ("under $2M") to the nearest edge and say so.

| kind | band | range |
|---|---|---|
| recipient_revenue | 1 to 8 | under $100K · $100K–500K · $500K–1M · $1M–5M · $5M–10M · $10M–50M · $50M–100M · $100M and up |
| funder_assets | 1 to 6 | under $1M · $1M–10M · $10M–50M · $50M–100M · $100M–1B · $1B and up |

Lower edge inclusive, upper edge exclusive. Negative revenue falls in band 1.

## `recipient_profile` (1,396,855 rows): one row per org with a known revenue

| Column | Meaning | Caveat |
|---|---|---|
| `ein` | Organization EIN (unique) | |
| `revenue` | Annual revenue | Newest e-filed 990 / 990-EZ (`flag_latest`), else the IRS master file amount. Not the grant year. |
| `revenue_source` | `990` or `bmf` | |
| `revenue_year` | Tax year of the 990 used | NULL when from the master file |
| `band` | `revenue_band(revenue)` | Orgs with no revenue anywhere (mostly 990-N filers, usually under $50K) are absent: band unknown |

## `funder_profile` (139,749 rows): one row per 990-PF filer

| Column | Meaning | Caveat |
|---|---|---|
| `ein` | Foundation EIN (unique) | |
| `assets` | Fair market value of assets at year end | Newest 990-PF, not the grant year |
| `asset_band` | `asset_band(assets)` | |
| `funder_type` | `family_named` (23,592) · `operating` (7,792) · `trust` (5,443) · `other` (101,907) | The IRS flags neither family nor company foundations. **family_named** = "Family" in the name, a floor (most family foundations carry a person's name). **operating** = master-file foundation code 02/03. **trust** = 4947(a)(1) trust (subsection 92). First match wins in that order. Company foundations are a separate flag, `company_foundations` (migration 045), not a `funder_type` value, so the cube is unchanged. |

## `ntee_codes` (681 rows) and `cause_terms` (287 rows), view `cause_lookup` (890 rows)

| Object | Columns | Meaning |
|---|---|---|
| `ntee_codes` | `code`, `name`, `major` (first letter), `draft` | Every NCCS NTEE code: 26 major groups and their detail codes (`etl/ntee_codes.csv`). `draft` = NCCS marks it "not for internal IRS use". |
| `cause_terms` | `term`, `ntee` (prefixes), `label` | Curated plain-English cause names ("animal shelters" → D20, "homelessness" → L41 + P85, "climate" → label `climate`). A prefix matches the start of an org's NTEE code. |
| `cause_lookup` | `term`, `ntee`, `label`, `source` | Curated terms first, then every NTEE code's own name in lowercase. NomBot resolves a cause the router left without a code or label here, before falling back to keyword search. |

## `grant_cube` (995,187 rows): the fact cube

One row per combination of the dimensions below. **Additive measures** can be summed over any rows. **Distinct counts are per row only.**

| Column | Kind | Meaning | Caveat |
|---|---|---|---|
| `tax_year` | dim | Foundation's tax year | |
| `recipient_state` | dim | State of the recipient address on the grant | Headquarters effect (DC) |
| `rural` | dim | true = the recipient ZIP's county has under 50,000 people (`county_need`) · false = 50,000+ · NULL = ZIP not mapped | A population proxy, not the USDA rural-urban code |
| `ntee3` | dim | First 3 characters of the recipient's NTEE code | NULL for unidentified or uncoded recipients. Prefixes up to 3 characters match exactly as on the full code |
| `recipient_kind` | dim | `nonprofit` (identified EIN) · `school_district` · `college_public` · `college_private` (from `public_entities`) · `unidentified` | School districts and colleges are matched by name and address to NCES / IPEDS lists. A grant naming only a school or department can be missed |
| `linked` | dim | Recipient has an EIN | Some school districts and colleges also have one |
| `recipient_band` | dim | `recipient_profile.band` | NULL = unidentified or revenue unknown |
| `funder_band` | dim | `funder_profile.asset_band` | |
| `funder_type` | dim | `funder_profile.funder_type` | See funder_profile |
| `labels` | dim | Sorted `label:tier` array from `org_labels`, e.g. `{climate:primary}` | `{}` = no label. Labels overlap each other and NTEE groups |
| `usd` | additive | Grant dollars | |
| `n` | additive | Grant lines | One 990-PF line each. Installments count separately |
| `gos_usd`, `gos_n` | additive | Dollars and lines whose purpose reads general / unrestricted / operating | A text rule, a floor |
| `recipients`, `funders` | per row | Distinct recipient EINs and funder EINs in this row | **Not additive.** For exact counts across rows use `grant_recipient_year` or `grant_clean` |

## `grant_recipient_year` (803,981 rows): identified recipients by year

One row per recipient EIN, tax year, recipient state and county (a recipient's grants can carry different addresses). Columns: `recipient_ein`, `tax_year`, `recipient_state`, `county_fips`, `rural`, `recipient_ntee` (full code), `recipient_band`, `labels`, `usd`, `n`, `gos_usd`, `funders` (distinct within the row, not additive). Used for exact distinct-recipient counts and concentration (top 1% of recipients). No funder columns: questions filtered by funder size or type use `grant_clean`.

## `grant_county_year` (139,214 rows): county rollup

`tax_year`, `county_fips` (NULL = ZIP not mapped), `recipient_state`, `ntee_major` (first letter, NULL = uncoded or unidentified), `linked`, `usd`, `n`. All clean grants, for county rankings and county per-resident figures (population from `county_need`, Census 2024).

## Schedule I grants by charities (migrations 041-042)

Grants that **public charities** report on Form 990 **Schedule I Part II** (grants over $5,000 to US organizations and governments): donor-advised fund sponsors, community foundations, United Ways, universities, hospitals, federations. Loaded by `etl/load_charity_grants.py` from the same IRS e-file batches as the 990-PF grants (processing years 2023-2026; `YEAR=2023 TYPES=990 etl/fetch_990_xml.sh` extracts a year's Form 990s). Kept apart from `grants`, so foundation answers don't change. Grants to individuals (Part III) are not loaded.

**Coverage (Oct 17, 2026, with IRS batch 05B):** 305,998 returns with a Schedule I from 113,774 filers · 3,413,866 grant lines · $673B (tax years 2019-2025; 2022: 882,905 lines $166.5B, 2023: 953,741 $188.3B, 2024: 1,021,405 $208.2B, 2025 partial: 127,643 $10.1B). **Linked to an org: 83.8% of lines, 84.5% of dollars** (2,764,491 by the reported EIN, then name matcher: 56,863 exact, 1,487 exact-city, 39,104 fuzzy). 94.5% of lines carry a recipient EIN.

**Checked against filers' own totals** (Schedule I sum vs Part IX line 1, grants to domestic organizations): Fidelity Charitable 2024 $15.07B vs $15.23B, 2023 $12.51B vs $12.62B, Schwab Charitable 2024 $8.62B vs $8.76B, National Philanthropic Trust 2024 $5.99B vs $6.11B, Silicon Valley Community Foundation 2024 $3.16B vs $3.16B.

**Schedule I coverage (migration 048, Oct 17):** named Schedule I dollars cover **92.9% of Part IX line 1** in tax years 2022-24 ($545.2B of $586.7B, each return capped at its line 1); 2022 91.7%, 2023 93.2%, 2024 93.7%. The shortfall is almost all in the e-files themselves, not the loader: each return's `coverage_note` says why (2022-25 returns with line 1 above 0):

| `coverage_note` | Returns | Line 1 · gap to named Schedule I $ | What it means |
|---|---|---|---|
| `itemized` | 113,104 | $519.8B · $5.8B | Named lines cover 90%+ of line 1 |
| `not_itemized` | 35,704 | $45.3B · $15.6B | Rows match the return's own recipient count, but sum short. **Silicon Valley Community Foundation 2022**: 3,580 lines (= its 3,575 + 5 count) sum to $2.06B vs $3.63B on line 1; no amended return in the IRS index, the GivingTuesday copy is byte-identical, no supplemental note. Good360 2022 ($1.09B vs $2.55B), Morgan Stanley GIFT 2024, Duke 2021 alike |
| `see_attached` | 1,726 | $14.4B · $14.2B | One "SEE ATTACHED" / "VARIOUS" line carries the money; the list is in a PDF, not the e-file (Bank of America Charitable Gift Fund 2022-24, Renaissance Charitable, Donors Fund, Greater Horizons, Greater Kansas City CF) |
| `list_incomplete` | 3,344 | $12.2B · $4.6B | The e-file lists fewer recipients than its own count (Omaha Community Foundation 2024: 8 of 863; Blackbaud Giving Fund 2024: 1,860 of 2,063) |
| `none_itemized` | 18,572 | $2.3B · $2.3B | Schedule I lists no recipients (mostly small filers) |
| `exceeds_total` | 1,261 | $3.5B · ~0 | Schedule I sum over 110% of line 1 (grants line 1 puts elsewhere, or a filing error) |

**Loader fixes (Oct 17):** a line with no `RecipientBusinessName` now takes the name from `AddressLine1Txt` ("INNER CITY HEALTH CENTER-3800 YORK" → "INNER CITY HEALTH CENTER"): 79 lines, $16M in 16 returns, reloaded with `--add` (which now also reloads a return already loaded). Lines with only an amount (11,018 lines, $273M; often the return's own total, e.g. Greater Horizons 2024 lists its $212M total as a line) are still skipped. Checked and not a cause: multiple or repeated `IRS990ScheduleI` (none), foreign or noncash-only lines (loaded), missing EIN (loaded and name-matched).

**Placeholder lines still count as grants:** 2,818 lines, $14.9B (`is_placeholder_recipient()`); $11.3B of them (2.0% of dollars) sit in `charity_grant_clean` as unidentified recipients at the filer's own address.

| Caveat | What it means |
|---|---|
| **Which grants a donor advised** | Schedule I doesn't say. A DAF sponsor's grants are mostly DAF grants (that's how `daf_sponsor` is defined); a community foundation's mix DAF and discretionary grants. Schedule D gives each filer's DAF grant total, not by recipient. |
| **Missing returns** | The IRS index files some 2025 and 2026 returns under batch 05A that ship in 05B (Fidelity Charitable's tax year 2023 return among them); `etl/recover_missing_xml.py` finds such batches and fills the gaps, so none of these are missing now. Tax year 2025 is partial: most of those returns aren't filed yet. |
| **Double counting** | Grants to another DAF sponsor, community foundation or United Way (`to_grantmaker`, 57,832 lines, $36.4B in 2022-25) are left out of answers: that money is counted when it's granted again. |
| **In-kind** | `amount` = cash + non-cash. Food banks (Feeding America) and donated-goods charities (Good360) report large in-kind grants. |
| **Threshold** | Schedule I lists grants over $5,000 per recipient. Smaller grants are only in Part IX totals. |
| **Not all itemized** | Some filers list only part of their grants (see coverage above). Use `charity_filers.coverage` / `coverage_note` to say so for a named filer. |
| **Place** | The recipient address on Schedule I, usually a headquarters (same DC effect as 990-PF grants). |

### `charity_filers` (305,998 rows): one row per Form 990 with a Schedule I

`object_id` (PK), `ein`, `tax_year`, `name`, `city`, `state`, `holds_dafs` (Part IV line 6), `daf_count`, `daf_contributions`, `daf_grants`, `daf_value` (Schedule D Part I, DAF column), `grants_domestic` (Part IX line 1), `sched_i_rows`, `sched_i_usd`. Newest return per filer and tax period. Migration 048: `declared_orgs` (Schedule I Part I lines 2 + 3, the filer's own recipient count; NULL when not filed), `placeholder_usd` (dollars on lines naming no recipient), `coverage` ((`sched_i_usd` - `placeholder_usd`) / `grants_domestic`, generated) and `coverage_note` (generated, table above).

### `charity_grants` (3,413,866 rows): one row per Schedule I line

| Column | Meaning |
|---|---|
| `funder_ein`, `object_id`, `tax_year` | The filing charity and its return |
| `recipient_name`, `recipient_city`, `recipient_state`, `recipient_zip` | As written. `recipient_country` set only for a foreign address (1,451 lines) |
| `reported_ein` | RecipientEIN as written |
| `recipient_ein`, `match_method`, `match_score` | Link to `orgs`: `ein` (the reported EIN is an org we know), else `exact` / `exact-city` / `fuzzy` from `etl/org_matcher.py` (the 990-PF matcher's rules) |
| `irc_section` | Recipient's section as written (501(c)(3), government ...) |
| `cash`, `noncash`, `amount` | amount = cash + non-cash |
| `purpose`, `noncash_desc` | Free text |

### `charity_funder_profile` (113,774 rows): one row per filer

`ein`, `name`, `state`, `ntee`, `holds_dafs`, `daf_count`, `daf_value`, `daf_share` (Schedule D DAF grants / Schedule I grants, years with both), `sched_i_usd`, **`funder_class`** (first match wins):

| funder_class | Rule | Filers · Schedule I $ (all years) |
|---|---|---|
| `united_way` | "United Way" in the name | 856 · $5.7B |
| `community_foundation` | "Community Foundation / Fund / Trust" in the name | 1,778 · $44.2B (with the T31 rule below) |
| `daf_sponsor` | Holds DAFs and DAF grants are at least half of its Schedule I grants (Fidelity, Schwab, NPT, Vanguard, Goldman Sachs, National Christian, Jewish Communal Fund, American Endowment Foundation ...) | 469 · $160.5B |
| `community_foundation` | Otherwise NTEE T31 | |
| `other` | Every other charity reporting grants | 108,358 · $391.8B |

### `charity_grant_clean` (2,916,071 rows) and `charity_grant_cube` (301,963 rows)

`charity_grant_clean`: tax years 2022-2025, amount above 0, US state, no foreign address, no private-foundation recipients (as `grant_clean`), plus `funder_class`, `funder_state`, `county_fips`, `recipient_ntee`, `gos` (purpose text reads general / operating) and `to_grantmaker`. **Same column names as `grant_clean`**, so the answer engine reads either. `charity_grant_cube`: sums by `tax_year`, `recipient_state`, `rural`, `ntee3`, `linked`, `funder_class`, `to_grantmaker`, `labels`; additive `usd`, `n`, `gos_usd`, `gos_n`. NomBot source `charity_grants` reads the cube for totals, states, causes and funder groups, and `charity_grant_clean` for counties, keyword causes, top funders and recipients. Refreshed by `refresh_research_layer()`.

## `county_acs` (3,196 rows): Census ACS 5-year by county (migration 043)

American Community Survey **5-year estimates, 2020-2024** (the newest release), from the Census table-based summary files (`etl/load_acs.py`; the Census API now asks for a key). **3,144 counties** (every county in `county_need`), **51 state rows** (`county_fips` = state FIPS + `000`) and the nation (`00000`). Puerto Rico is left out, as in `county_need`. Joins to `county_need` on `county_fips`. `county_need` (SAIPE 2023 poverty, 2024 population estimates) is unchanged and still drives per-resident figures.

| Column | Meaning | Table |
|---|---|---|
| `level`, `name`, `state`, `acs_year` | county / state / us; name and postal code from `county_need`; 2024 = 2020-2024 | |
| `population` | Total population | B01003 |
| `poverty_universe`, `people_in_poverty`, `poverty_moe`, `poverty_rate` | People whose poverty status is known, those below the line, its 90% margin, rate in percent | B17001 |
| `median_income` | Median household income, 2024 dollars | B19013 |
| `under18`, `age65_plus` | Residents under 18, 65 and over | B01001 |
| `white_nh`, `black_nh`, `aian_nh`, `asian_nh`, `nhpi_nh`, `other_nh`, `multi_nh`, `hispanic` | Race alone, not Hispanic or Latino; Hispanic or Latino of any race | B03002 |
| `poc_share` | People of color = 1 - non-Hispanic white / total, percent | B03002 |

**Caveats:** 5-year estimates average 2020-2024, so they lag fast change. Small counties carry wide margins (`poverty_moe`). ACS poverty (40.7M people nationally) and SAIPE (40.8M) differ slightly by county. **Used by NomBot** for `perPoverty` (money per person in poverty, by state or county) and for breakdowns of counties by poverty rate, median income, share of people of color and share aged 65+ (`county_poverty`, `county_income`, `county_race`, `county_age`), placing grants in the recipient's county (headquarters effect applies).

## `federal_awards` (408,462 rows): federal grants to nonprofits (migration 044)

Federal **grants and cooperative agreements** (assistance types 02 block, 03 formula, 04 project, 05 cooperative agreement) to recipients USAspending.gov tags as **nonprofit** (with or without 501(c)(3)) or **private college**, with no government or public-college tag. **Federal fiscal years 2022-2025** (October to September; FY2025 is complete). Loaded by `etl/load_federal_awards.py` from the USAspending award data archive (`FY<yyyy>_All_Assistance_Full_20261006.zip`, ~1.3-1.6 GB each, in `data/gov/usaspending/`, git-ignored). Transactions are summed per award and fiscal year.

**Coverage (Oct 17, 2026):** 408,462 award-years, $272.0B net obligations (FY2022 101,775 · $56.8B, FY2023 102,793 · $64.1B, FY2024 108,991 · $90.3B, FY2025 94,903 · $60.8B). FY2024 includes $20B of EPA Greenhouse Gas Reduction Fund awards to Climate United, Coalition for Green Capital, Power Forward, Opportunity Finance Network, Inclusiv and Justice Climate Fund. 34 awarding agencies, 1,445 assistance listings. **Linked to an org (name + state + city, `etl/org_matcher.py`): 80.6% of award-years, 83.7% of dollars**, 23,557 distinct EINs (exact 278,353, fuzzy 49,704, exact-city 1,147). Spot-check of 14 random links: all right. **Round 2 (Oct 17, `etl/relink_federal.py`): 85.5% of award-years, 88.4% of dollars, 24,214 EINs**: `uei` 19,928 award-years (EIN from the Federal Audit Clearinghouse UEI crosswalk `fac_uei_ein`), `uei-stub` 25,511 (moved off zero-revenue duplicate EINs, e.g. Johns Hopkins), `abbrev` 87.

| Column | Meaning |
|---|---|
| `award_key`, `fiscal_year` | USAspending award key; one row per award and fiscal year (unique) |
| `fain`, `agency_code`, `agency_name`, `sub_agency_name` | Award id, awarding agency |
| `cfda_number`, `cfda_title` | Assistance listing (program), e.g. 93.600 Head Start |
| `assistance_type` | 02-05 |
| `recipient_uei`, `recipient_name`, `recipient_city`, `recipient_state`, `recipient_zip` | Recipient as registered in SAM |
| `business_types`, `recipient_kind` | USAspending codes; `nonprofit_501c3` (M), `nonprofit_other` (N, e.g. rural electric co-ops), `private_college` (O) |
| `pop_scope`, `pop_state`, `pop_county_fips` | **Place of performance** (where the work happens). 24,621 award-years ($37.5B) have no state (multi-state, foreign); 170,982 ($100.4B) have no county (statewide, multi-state) |
| `amount`, `actions` | Net federal_action_obligation in the fiscal year (deobligations net out, so a row can be negative); number of transactions |
| `description` | Award description |
| `recipient_ein`, `match_method`, `match_score` | Link to `orgs` |

| Caveat | What it means |
|---|---|
| **Not the same as 990 government grants** | Form 990 line 1e counts federal, state and local grants together, including federal money passed through states. USAspending has only direct (prime) federal awards. For Ohio's 393 linked recipients with a 2024 Form 990, FY2024 federal awards were $1.2B against $17.2B reported on line 1e. |
| **Left out** | Contracts, loans, direct payments, subawards, grants to governments and public universities, and USAID's "Miscellaneous Foreign Awardees" aggregate line (1,057 rows). |
| **Fiscal years** | Federal fiscal years, not the tax years of the 990 sources. |
| **No EIN** | USAspending carries a UEI, not an EIN. Links are by name and place, then by UEI through single-audit filings (`fac_uei_ein`), which only cover recipients spending $750K or more of federal money a year ($1M from fiscal years starting Oct 2024). Causes (NTEE, labels) cover linked recipients only. |

NomBot source `federal_awards` reads this table directly (`federal_awards fa LEFT JOIN orgs o`): totals and trends, states and counties by place of performance (per resident or per person in poverty), agencies (`top_funders`), programs, recipients, award sizes and ACS county groups. Totals carry the 990 line 1e comparison for the same linked recipients.

## Grant-size histograms (migration 046): medians and percentiles

`grant_size_hist` (3,181,445 rows, 675 MB), `charity_grant_size_hist` (2,229,521 rows, 422 MB), `fed_award_size_hist` (227,582 rows, 36 MB). One row per research-cube cell (the same dimensions as `grant_cube`, `charity_grant_cube`; federal: fiscal year, `pop_state`, rural, `ntee3`, linked, `recipient_band`, `labels`) and **amount bucket**.

| Column | Meaning |
|---|---|
| `bucket` | `amount_bucket(amount)`: the amount cut to 3 significant digits ($12,345 → $12,300); under $1,000 stays as is |
| `exact` | every amount in the row equals `bucket` (a round amount). 92% of foundation grants, 57% of Schedule I lines |
| `n`, `lo`, `hi` | grants (award-years) in the row, smallest and largest amount. `fed_award_size_hist` also has `usd` (no federal cube for the average) |

**Reading a percentile:** sum `n` by `(bucket, exact)` over the matching cells, order by `bucket, exact DESC` (the amounts' sorted order) and walk the ranks as `percentile_cont` does. A rank in an exact row, or a row's first or last rank, is exact; otherwise it is interpolated between the row's `lo` and `hi`, which bound the true value (under 1% apart). **Checked (Oct 17,** `web/eval/answer-cube.mts`, 35 grant-size specs on the histograms vs `percentile_cont` on the same live data): 92 of 108 medians and 90th percentiles exact, max error 0.16%; shown tiles identical in 106 of 111, the rest marked "about". Answer time (whole answer, loaded machine): median 633 → 282 ms, slowest 4.2 → 1.1 s.

## Company foundations (migration 045)

The IRS doesn't flag company (corporate) foundations, and NTEE T21 is unreliable (it codes the Ford Foundation as corporate). `etl/load_company_foundations.py` builds the flag from evidence on each foundation's own 990-PF (name, address, website, Schedule B) plus the free SEC EDGAR name lists (`company_tickers.json`, ordered by market value; `cik-lookup-data.txt`; addresses and current and former names from `data.sec.gov/submissions` for the 1,500 largest listed companies and every matched filer, cached in `data/sec/`). No model calls.

**Refresh:** re-run `python3 etl/load_company_foundations.py` after new 990-PF batches load (`--no-xml` rescores from the tables already loaded; rows are upserted and dropped flags deleted, so the table is never empty mid-load). Plain tables: no materialized view depends on them, and `refresh_research_layer()` doesn't touch them.

**Coverage (Oct 17, 2026, recall pass):** **1,624 foundations** flagged (was 1,446), $33.4B in assets (was $27.5B). Their grants: 615,160 lines, **$10.49B in tax years 2022-2024, 4.7% of `grant_clean` dollars** (was $9.03B, 4.0%; 1,421 foundations with grants).

| Evidence (`evidence` array) | Meaning | Foundations |
|---|---|---|
| `contributor` | A Schedule B business contributor carries the foundation's name stem ("Walmart Inc." → Wal-Mart Foundation, "JPMorgan Chase Bank NA" → JPMorgan Chase Foundation). Not a trust, estate, DAF, family vehicle or nonprofit | 1,053 |
| `corp_name` | The name itself says company ("Boeing Company Charitable Trust", "Illumina Corporate Foundation") | 218 |
| `sec` | The name stem is an SEC EDGAR company: its name, a current or former EDGAR name ("Walgreen Co"), the name without industry words ("Truist Financial" → Truist, "Cigna Group", "Prudential Insurance"), the singular ("Walgreens" → Walgreen), or a ticker (UPS) | 965 |
| `hq` | The foundation's city and state are that SEC company's business or mailing address (first SEC match or a listed one; a stem of 3 letters or fewer needs a top-750 company, e.g. PNC; not when the stem is the foundation's own city or a state) | 637 |
| `street` | Stronger `hq`: the foundation's house number and street (same ZIP or city) are the company's ("1 Nationwide Plaza", "108 Wilmot Rd"). An unlisted LLC, group, holding or enterprises filer doesn't count: family offices house their family's foundation | 471 |
| `brand` | The name stem is a made-up word that is a top-750 listed company's current name (by market value): Amgen, Cigna, Truist, Accenture. Not a dictionary word, first name, board-member surname (10+ boards: Pearson, Eaton, Waters) or filer city (Fairfax). Or the whole stem is a two-plus-word listed name ("American Electric Power"). The name must have a foundation word and no "community foundation" | 150 |
| `website` | The foundation's website is the company's .com site (`cigna.com/...`, `nationwide.com/...`, `ge.com/foundation`): the site name equals the matched SEC stem or the ticker | 136 |
| `addr_name` | The foundation's street is named for the company ("One Amgen Center Drive"). Recorded only; never flags on its own | 17 |
| `address` | The matching contributor shares the foundation's street address | 451 |

**Rule:** flagged with `corp_name`, or `contributor`, or `sec` + `hq` (city or street), or `brand`, or `sec` + `website`. A contributor also matches when the foundation stem minus industry words equals it ("IBM International" ← IBM Corporation), or when the foundation stem ends with a listed contributor at the same street ("John Deere" ← Deere & Company). A foundation "held in trust" ("Textron Charitable Trust", "TR UA Fifth Third Foundation") is scored like a foundation. A stem whose first word is a listed made-up name ("Comcast NBCUniversal") counts for `street` and `website` only. A stem that reads as a person's name (first names from board members and a name list, initials like "W K") needs a listed company behind it. Namesake-family foundations fail on evidence: their stems are people's names, and their Schedule B donors are the family's trusts and estates ("WK Kellogg Foundation Trust", "Lukas T Walton" → not flagged). A short surname list backs this up. Bank-trustee names ("SMITH TR UW BANK OF AMERICA NA") never match.

**Hand checks (Oct 17):** precision **49 of 50** on a random sample of flagged foundations (the miss: a Chinese benevolent "company"). Recall **42 of 49** known corporate foundations (missed: Cigna Group, Nationwide, Prudential, GE Aerospace, Truist, Amgen, Walgreens: no Schedule B match and the SEC name or address differs). Google Foundation and Bank of America Charitable Foundation file no 990-PF in our data.

**Recall pass (Oct 17, later):** the rules above from `street` down, plus the contributor, trust and prefix cases. Those 7 misses are now flagged (49 of 49). On a wider list of **102 well-known company foundations** (banks, insurers, pharma, retail, tech, telecom, energy, autos, industrials) recall went **75 → 99**. Still missed: KeyBank (EDGAR's KeyBank address isn't its Cleveland office; KeyCorp's name differs), Adobe (dictionary word, foundation at Foundation Source in Wilmington), Progressive (PO box in Cleveland, company in Mayfield Village). Precision on a fresh random sample of 50 flagged: **49 of 50** (doubtful: Project Lightwell Foundation, funded by Project Lightwell LLC, maybe a family vehicle; COR Companies Charitable Trust is a company foundation but its `company` column points to Cencora, ticker COR). Doubtful among the 182 new flags, by hand: about 7 (Microsoft Charitable Trust, Knox, Sirius Fund, Dart, Bearing Fruit, Southshore, GBT). Lost from the old set: East West Foundation (city match to East West Enterprises LLC, a family office). Patient-assistance foundations (Merck, AbbVie, Bristol-Myers Squibb) are company-run and flagged; their in-kind drug grants are large.

| Caveat | What it means |
|---|---|
| **A floor** | Company foundations whose names don't match the company and that list no company contributor are missed. |
| **Not corporate giving** | Giving programs run inside a company, sponsorships and matching gifts paid by the company aren't 990-PF grants. |
| **Pass-through** | Many company foundations pay out what the company gives each year, so assets are small next to grants (Wells Fargo Foundation). |

### `company_foundations` (1,624 rows)

`ein` (PK), `company` (contributor or SEC name, else the stem), `cik`, `ticker` (when matched), `evidence` (text[]), `stem` (normalized name stem the match used).

### `pf_business_contributors` (107,182 rows) and `pf_filer_address` (461,677 rows)

Every business-name contributor on 990-PF Schedule B (`ein`, `object_id`, `tax_year`, `name`, `address1`, `city`, `state`, `zip`, `amount`) across all 461,680 990-PF returns in `data/xml/*pf`, 20,586 foundations. People listed as contributors aren't stored. `pf_filer_address`: the filer's street address on each return.

## What uses what (NomBot `lib/answer.ts`)

| Question | Source |
|---|---|
| Totals, shares, trends, compare (label or NTEE cause up to 3 characters, no county) | `grant_cube` sums + exact distinct counts (`grant_recipient_year` recipients, `grant_clean` funders) |
| By state, rural vs urban, fastest-growing causes, general support share | `grant_cube` |
| Breakdowns by cause, NTEE code, recipient size, foundation size or type, recipient kind, year | `grant_cube` |
| Breakdown by county | `grant_county_year` (all causes or NTEE major groups), else `grant_clean` |
| Concentration | `grant_recipient_year` |
| Median and 90th percentile grant (`grant_size`; label or NTEE cause up to 3 characters, no county) | `grant_size_hist` / `charity_grant_size_hist` / `fed_award_size_hist`, count and average from `grant_cube` / `charity_grant_cube` |
| Keyword causes, a named county, top funders / recipients, out-of-state, overlap | `grant_clean` (+ profiles for filters) |
| Company foundations (filter `funderType: company`) | `grant_clean` with `funder_ein IN company_foundations`, never the cube (it has no company column) |
| Schedule I grants (source `charity_grants`, filter `charityFunder`) | `charity_grant_cube`, else `charity_grant_clean` (+ `charity_funder_profile` for funder names) |
| Per person in poverty; counties grouped by poverty, income, race/ethnicity, age | `county_acs` with `grant_county_year`, `grant_clean`, `charity_grant_clean` or `flag_year` by county |
| Federal grants to nonprofits (source `federal_awards`) | `federal_awards` (+ `orgs` for causes, `county_acs` / `county_need` for per-person figures) |
