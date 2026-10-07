-- Team size from Form 990 Part I: line 5 (total employees, from W-3) and line 6 (total volunteers, estimate).
-- 990-EZ doesn't report them, so these stay NULL for EZ filers. Filled by etl/load_990_text.py.
ALTER TABLE filing_text ADD COLUMN employees int, ADD COLUMN volunteers int;
