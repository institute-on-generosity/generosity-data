# generosity-data

Shared IRS nonprofit data pipeline and Postgres + pgvector schema for [NomBot](https://github.com/institute-on-generosity/NomBot) and [GrantRadar](https://github.com/institute-on-generosity/GrantRadar).

## Local setup (proof of concept)

```bash
brew install postgresql@17 pgvector && brew services start postgresql@17
export PATH=/opt/homebrew/opt/postgresql@17/bin:$PATH
createdb nombot
db/migrate.sh                     # apply db/migrations/*.sql once each
etl/load_bmf.sh                   # IRS BMF for WV, KY, TN, VA, OH (~205K orgs, ~10s)
STATES="ca ny" etl/load_bmf.sh    # any other states; safe to re-run (upsert)
```

Set `DATABASE_URL` to target another database (e.g. Supabase after migration). Defaults to `postgresql:///nombot`.

## Tables

| Table | Source | Contents |
|---|---|---|
| `orgs` | [IRS EO BMF](https://www.irs.gov/charities-non-profits/exempt-organizations-business-master-file-extract-eo-bmf) | One row per exempt org; terminating orgs (status 25) skipped; keyword index on name + city |
| `financials` | [IRS SOI extract](https://www.irs.gov/statistics/soi-tax-stats-annual-extract-of-tax-exempt-organization-financial-data) | Revenue, expenses, assets by tax year *(loader: next)* |
| `filing_text` | [IRS 990 e-file XML](https://www.irs.gov/charities-non-profits/form-990-series-downloads) | Mission + program text and 512-dim embeddings *(loader: next)* |

## Status
- [x] Schema + migration runner
- [x] BMF loader (5 Appalachian states loaded: 204,564 orgs)
- [ ] SOI financials loader
- [ ] 990 XML text extraction
- [ ] Embeddings
