-- task_events.ts is REAL: every emitter but one writes it via the shared
-- `time.time()` clock (`Store.set_status`). One emitter — the human
-- landed-override path (`blockers/landed_override.py`, kind
-- `approved_landed_override`) — hand-rolled its event dict and put an
-- ISO-8601 string (`datetime.now(timezone.utc).isoformat()`) into `ts`
-- instead. SQLite orders TEXT above REAL, so those rows sorted as the
-- newest events in every `ORDER BY ts` forever, and any window/aggregation
-- over `ts` mis-handled them. Confirmed on the live ledger 2026-08-20:
-- 344,798 REAL rows, 15 TEXT rows, all `approved_landed_override`.
--
-- The emitter is fixed in the same commit (it now writes `time.time()`,
-- same as every other emitter), so this migration converges to a no-op on
-- every connect after the incident's 15 rows are normalized once.
--
-- `julianday()` parses the exact shape `datetime.now(timezone.utc).isoformat()`
-- emits (`YYYY-MM-DDTHH:MM:SS.ffffff+00:00`) — SQLite accepts the `T`
-- separator, fractional seconds, and the `+HH:MM`/`Z` offset. The Julian day
-- epoch is 2440587.5 (1970-01-01T00:00:00Z); `* 86400.0` converts days to
-- seconds. `ROUND(..., 3)` keeps the value at millisecond resolution, which
-- is well below the clock's own precision.
--
-- `ts` is `NOT NULL` in the schema (migration 0005), so this only ever
-- touches rows SQLite can actually parse as a date/time (`julianday(ts) IS
-- NOT NULL`) — an unparseable TEXT value is left alone rather than turned
-- into NULL, which would abort this whole `executescript` and brick every
-- connect. Only `ts` is touched: the `data` JSON blob keeps its own ISO
-- `ts` key byte-identical, and no row is ever deleted.
--
-- `typeof(ts) = 'text'` is not indexable, so this is one full scan of
-- task_events per connect (~345k rows on the live ledger, low tens of ms).
-- Accepted: a scan is cheaper than adding a schema-version table this repo
-- does not have, and the predicate is false for the overwhelming majority
-- of rows once the 15 are normalized.
UPDATE task_events
   SET ts = ROUND((julianday(ts) - 2440587.5) * 86400.0, 3)
 WHERE typeof(ts) = 'text'
   AND julianday(ts) IS NOT NULL;
