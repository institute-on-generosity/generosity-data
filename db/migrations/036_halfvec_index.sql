-- Half-precision HNSW index for NomBot's vector candidates (web/lib/search.ts orders by
-- embedding::halfvec(512)). 696MB vs 1.39GB for the full-precision index, so on a busy Mac it stays in the
-- OS cache: a cold walk for the 2000 nearest filings dropped from 0.4-2.7s to ~50ms. Similarity scores are
-- still computed at full precision on the candidates; the top 40 matched the full index on the 55 test
-- questions. Built in ~4 min (1GB maintenance_work_mem); REFRESH MATERIALIZED VIEW filing_latest rebuilds it.
-- The full-precision filing_latest_embedding_idx stays for now (eval/bench.mts reports its size).
SET maintenance_work_mem = '1GB';
CREATE INDEX IF NOT EXISTS filing_latest_embedding_half_idx ON filing_latest USING hnsw ((embedding::halfvec(512)) halfvec_cosine_ops);

-- warm_search() (migration 034) now warms this index instead of the full-precision one the search no longer reads.
CREATE OR REPLACE FUNCTION warm_search() RETURNS bigint LANGUAGE sql AS $$
  SELECT coalesce(sum(pg_prewarm(c.oid, 'read')), 0)::bigint
  FROM pg_class c
  WHERE c.relname IN ('filing_latest_embedding_half_idx', 'filing_latest', 'filing_latest_ein_idx', 'filing_latest_search_idx',
                      'orgs_pkey', 'orgs_search_idx', 'orgs_state_idx',
                      'financials_latest', 'financials_latest_ein_idx', 'flag_latest', 'flag_latest_ein_idx', 'zip_regions')
$$;
