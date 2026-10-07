-- Cosine-distance HNSW index for semantic search over filing text.
CREATE INDEX filing_text_embedding_idx ON filing_text USING hnsw (embedding vector_cosine_ops);
