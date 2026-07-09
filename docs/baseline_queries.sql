-- M0.5 baseline. Read-only; safe to run against the live DB.
--   sqlite3 -readonly ~/.no_human/no_human.db < docs/baseline_queries.sql
.mode column
.headers on

SELECT '--- M0 exit criterion: has an authored PR ever been opened? ---' AS section;
SELECT COUNT(*) AS attempts_with_pr_url FROM attempts WHERE pr_url IS NOT NULL;

SELECT '--- flagship task 84251cb2: attempts ---' AS section;
SELECT attempt_number, status, turns_used, tokens_used, cache_read_tokens,
       COALESCE(review_passed, '-') AS review, COALESCE(pr_url, '(none)') AS pr
FROM attempts WHERE task_id LIKE '84251cb2%' ORDER BY attempt_number;

SELECT '--- flagship task 84251cb2: wall clock ---' AS section;
SELECT ROUND((MAX(ts) - MIN(ts)) / 3600.0, 2) AS wall_clock_hours,
       COUNT(*) AS events
FROM task_events WHERE task_id LIKE '84251cb2%';

SELECT '--- per-role token burn (the M3 target) ---' AS section;
SELECT json_extract(data, '$.source') AS role,
       SUM(COALESCE(json_extract(data, '$.cache_read_tokens'), 0)) AS cache_read,
       SUM(COALESCE(json_extract(data, '$.cache_creation_tokens'), 0)) AS cache_creation,
       SUM(COALESCE(json_extract(data, '$.tokens_used'), 0)) AS tokens,
       SUM(COALESCE(json_extract(data, '$.num_turns'), 0)) AS turns
FROM task_events
WHERE task_id LIKE '84251cb2%' AND json_extract(data, '$.kind') = 'result'
GROUP BY role ORDER BY cache_read DESC;

SELECT '--- coder share of cache-read ---' AS section;
WITH r AS (
  SELECT json_extract(data, '$.source') AS role,
         SUM(COALESCE(json_extract(data, '$.cache_read_tokens'), 0)) AS cr
  FROM task_events
  WHERE task_id LIKE '84251cb2%' AND json_extract(data, '$.kind') = 'result'
  GROUP BY role
)
SELECT (SELECT cr FROM r WHERE role = 'agent') AS coder_cache_read,
       (SELECT SUM(cr) FROM r) AS total_cache_read,
       ROUND(100.0 * (SELECT cr FROM r WHERE role = 'agent') / (SELECT SUM(cr) FROM r), 1)
         AS coder_share_pct;
