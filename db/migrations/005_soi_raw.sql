-- Keep the whole SOI extract row (non-empty, non-zero fields) so NomBot can show the
-- original record in a readable viewer, plus the IRS field dictionary that explains it
-- (YYeofinextractdoc.xlsx: one sheet per form, and a sheet of code meanings).
ALTER TABLE financials ADD COLUMN raw jsonb;

CREATE TABLE soi_fields (
  form         text NOT NULL,   -- 990, 990EZ, 990PF
  col          text NOT NULL,   -- column name as in the extract CSV (lowercase)
  position     int  NOT NULL,   -- order in the field dictionary
  description  text,
  location     text,            -- where on the form, e.g. "990 Core_Pt VIII-12A"
  codes        jsonb,           -- value -> meaning, when the codes sheet defines them
  PRIMARY KEY (form, col)
);
