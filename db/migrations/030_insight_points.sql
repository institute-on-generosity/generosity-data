-- Insights: 2-3 short bullet points for NomBot's feed cards and finding page ("What it means").
-- jsonb array of strings; key numbers wrapped in **bold** (the only markup), the last bullet starts
-- "**Why it matters:**". Written by etl/mine_insights.py from the same numbers as the headline.
-- NULL until the next run; NomBot then falls back to `summary`.
ALTER TABLE insights ADD COLUMN IF NOT EXISTS points jsonb;
