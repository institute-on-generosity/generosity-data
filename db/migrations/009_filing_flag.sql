-- FLAG diligence fields (Financial, Legal and Governance) from 990 / 990-EZ e-file XML, one row per filing.
-- Filled by etl/load_990_flag.py. cy_/py_ = current year / prior year (Form 990 Part I columns), so most
-- filings give a two-year comparison. 990-EZ has no prior-year columns, governance answers or Part IX split.
CREATE TABLE filing_flag (
  object_id text PRIMARY KEY REFERENCES filing_text (object_id) ON DELETE CASCADE,
  ein char(9) NOT NULL,
  tax_year int,
  form text NOT NULL,
  -- Part I (990) / Part I (990-EZ)
  cy_revenue bigint, py_revenue bigint, cy_expenses bigint, py_expenses bigint,
  cy_contributions bigint, py_contributions bigint, cy_program_revenue bigint, py_program_revenue bigint,
  cy_salaries bigint, py_salaries bigint, cy_fundraising_expenses bigint,
  government_grants bigint,                 -- Part VIII line 1e
  -- Part X balance sheet (end of year unless _boy)
  cash bigint,                              -- cash + savings and temporary cash investments
  total_assets bigint, total_assets_boy bigint, total_liabilities bigint, total_liabilities_boy bigint,
  net_assets bigint, net_assets_boy bigint, unrestricted_net_assets bigint,
  -- Part IX functional expenses
  program_expenses bigint, management_expenses bigint, fundraising_expenses bigint,
  -- Part VI governance (990 only)
  voting_members int, independent_members int,
  family_business_ties boolean, conflict_policy boolean, whistleblower_policy boolean, retention_policy boolean,
  audited boolean, audit_committee boolean, diversion_of_assets boolean, form990_to_board boolean,
  -- Part IV insider and related-party flags
  excess_benefit boolean, insider_loan boolean, grant_to_insider boolean, business_with_insiders boolean,
  -- Part VII (990) or Part IV (990-EZ): officers, directors, key employees, highest paid
  people jsonb                              -- [{name, title, hours, pay, other, role}]
);
CREATE INDEX filing_flag_ein_idx ON filing_flag (ein, tax_year DESC);
