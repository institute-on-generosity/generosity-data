-- Newest record per organization, precomputed for search. With every US nonprofit loaded, picking the
-- newest filing, SOI year and FLAG row per EIN inside each search sorted ~500K rows three times (spilling
-- to disk, 12-16s a search). Refreshed by db/refresh.sql after any loader (the loaders call it).
CREATE MATERIALIZED VIEW filing_latest AS
  SELECT DISTINCT ON (ein) * FROM filing_text ORDER BY ein, tax_year DESC NULLS LAST, object_id DESC;
CREATE UNIQUE INDEX filing_latest_ein_idx ON filing_latest (ein);
CREATE INDEX filing_latest_search_idx ON filing_latest USING gin (search);
CREATE INDEX filing_latest_embedding_idx ON filing_latest USING hnsw (embedding vector_cosine_ops);

CREATE MATERIALIZED VIEW financials_latest AS
  SELECT DISTINCT ON (ein) ein, tax_year, form, revenue, expenses, assets,
         coalesce(raw->>'totnetassetend', raw->>'totnetassetsend')::numeric AS net_assets
  FROM financials ORDER BY ein, tax_year DESC;
CREATE UNIQUE INDEX financials_latest_ein_idx ON financials_latest (ein);

CREATE MATERIALIZED VIEW flag_latest AS
  SELECT DISTINCT ON (ein) ein, tax_year, cy_revenue, py_revenue, cy_expenses, cash, cy_contributions
  FROM filing_flag ORDER BY ein, tax_year DESC NULLS LAST;
CREATE UNIQUE INDEX flag_latest_ein_idx ON flag_latest (ein);
