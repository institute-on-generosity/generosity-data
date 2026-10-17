-- Search reads the half-precision HNSW index (036) since Oct 17; the full-precision one (1.4 GB) only
-- competed for memory on the host. Similarity is still scored on the full-precision embedding column.
DROP INDEX IF EXISTS filing_latest_embedding_idx;
