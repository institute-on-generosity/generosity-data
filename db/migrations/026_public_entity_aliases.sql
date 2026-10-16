-- Hand-checked names for school districts and colleges (e.g. "UNIVERSITY MICHIGAN" -> IPEDS University of
-- Michigan-Ann Arbor), so etl/load_public_entities.py --match keeps these links on reload. state '' = any state.
CREATE TABLE IF NOT EXISTS public_entity_aliases (
  alias_norm text NOT NULL,           -- org_norm() of the name as foundations write it
  state      text NOT NULL DEFAULT '',
  id         text NOT NULL REFERENCES public_entities (id),
  note       text,
  added_at   timestamptz DEFAULT now(),
  PRIMARY KEY (alias_norm, state)
);
