-- "Saved so far" tracker: which workspaces are gone, and from when.
--
-- Written by the `track_changes` task (precompute/change_tracker.py) from
-- system.access.workspaces_latest, overwritten in full each run (it is small). Two
-- row kinds:
--   row_kind = 'listing'    one per workspace ever listed: first/last listed, the last
--                           day it was seen ACTIVE, and its last status.
--   row_kind = 'retirement' one per period a workspace was gone: retired_on (the day
--                           after it was last seen active) .. revived_on (NULL = still
--                           gone). reason: banned | no_longer_listed
--                           (deleted | cancelled are reserved, never seen live).
-- A deleted workspace drops out of workspaces_latest (no deleted status exists), and
-- BANNED counts as gone. Nothing spend-based. A failed or empty listing changes
-- nothing. A workspace never listed has an unknown status: its setting changes
-- count, its deletions don't.
-- See docs/DATA_MODEL.md "Savings tracker".

CREATE TABLE IF NOT EXISTS {catalog}.{schema}.finops_workspace_retirement (
  workspace_id   STRING,
  row_kind       STRING,     -- listing | retirement
  retired_on     DATE,       -- retirement rows: first day it counts as gone
  reason         STRING,     -- retirement rows: banned | no_longer_listed (deleted | cancelled reserved)
  revived_on     DATE,       -- retirement rows: first day it was listed active again
  first_listed   DATE,       -- listing rows
  last_listed    DATE,       -- listing rows
  last_active    DATE,       -- listing rows: last day listed with a non-retired status
  last_status    STRING,     -- listing rows: raw status from workspaces_latest (may be NULL)
  updated_at     TIMESTAMP
)
USING DELTA;
