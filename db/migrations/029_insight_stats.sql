-- Insights: key numbers for NomBot's research detail page (/research/<slug>), shown as stat tiles.
-- [{label, value}], 2-4 items, value preformatted ("38%", "$1.2B", "4x"). Written by etl/mine_insights.py from
-- the same numbers as the headline; NULL until the next run, and the page simply hides the tiles.
ALTER TABLE insights ADD COLUMN IF NOT EXISTS stats jsonb;
