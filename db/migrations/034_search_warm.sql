-- Keep the search's hot relations in memory. National searches walk the HNSW graph (~25K index pages for
-- the 2000 nearest filings); with the default 128MB shared_buffers those pages live only in the OS file
-- cache, which evicts them between searches (cold walk: 0.6-3s instead of ~50ms). NomBot calls
-- warm_search() when its server starts and every 10 minutes (web/lib/db.ts; migration 036 swaps in the half-precision vector index). 'read' mode pulls the files
-- into the OS cache (~2.3GB: the vector index, filing_latest's main table and the small indexes and views
-- the joins use; not the 1.5GB of filing text in TOAST). Returns blocks read.
-- Durable fix: shared_buffers of ~4GB (needs a Postgres restart) and free RAM for the OS cache.
CREATE EXTENSION IF NOT EXISTS pg_prewarm;

CREATE OR REPLACE FUNCTION warm_search() RETURNS bigint LANGUAGE sql AS $$
  SELECT coalesce(sum(pg_prewarm(c.oid, 'read')), 0)::bigint
  FROM pg_class c
  WHERE c.relname IN ('filing_latest_embedding_idx', 'filing_latest', 'filing_latest_ein_idx', 'filing_latest_search_idx',
                      'orgs_pkey', 'orgs_search_idx', 'orgs_state_idx',
                      'financials_latest', 'financials_latest_ein_idx', 'flag_latest', 'flag_latest_ein_idx', 'zip_regions')
$$;
