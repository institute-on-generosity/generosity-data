-- Record which model produced each embedding, so a provider switch (e.g. the
-- local POC fallback -> OpenAI in the cloud) can re-embed only stale rows.
ALTER TABLE filing_text ADD COLUMN embedding_model text;
