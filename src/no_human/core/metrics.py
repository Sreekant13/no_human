"""The measurement spine (M4): the north-star numbers, straight from the DB.

North star: quality merged PRs at lower cost. Everything here is a read-only
SQL aggregate over ``attempts`` / ``tasks`` / ``task_events`` — no derived
state, no caching, so the numbers cannot drift from the record. Served by
``/api/metrics``; the M3 cost work is judged against ``tokens_per_pr`` and
``coder_cache_read_per_attempt``.
"""

from __future__ import annotations

from typing import Any

from .db import Store


async def compute_metrics(store: Store) -> dict[str, Any]:
    db = store.db

    async def one(sql: str, *args) -> Any:
        cur = await db.execute(sql, args)
        row = await cur.fetchone()
        return row[0] if row else None

    prs_opened = await one(
        "SELECT COUNT(DISTINCT pr_url) FROM attempts WHERE pr_url IS NOT NULL")
    prs_merged = await one(
        "SELECT COUNT(*) FROM task_events "
        "WHERE json_extract(data, '$.kind') = 'merged'")
    attempts_total = await one("SELECT COUNT(*) FROM attempts")

    # Per auth profile: attempts and token burn. The profile is stamped on the
    # attempt row from what the process actually exported, not from config.
    cur = await db.execute(
        """SELECT COALESCE(auth_profile, 'unknown') AS profile,
                  COUNT(*) AS attempts,
                  COALESCE(SUM(COALESCE(tokens_used, 0)), 0) AS tokens,
                  COALESCE(SUM(COALESCE(cache_read_tokens, 0)), 0) AS cache_read
           FROM attempts GROUP BY profile ORDER BY attempts DESC""")
    by_profile = [
        {"profile": r[0], "attempts": r[1], "tokens": r[2], "cache_read": r[3]}
        for r in await cur.fetchall()
    ]

    # Per complexity tier (C1.5): cost AND quality, so a cheaper setting
    # is kept only where quality holds — measured, never assumed.
    cur = await db.execute(
        """SELECT COALESCE(json_extract(t.context, '$.complexity_tier'),
                           'unclassified') AS tier,
                  COUNT(*) AS attempts,
                  COALESCE(SUM(COALESCE(a.tokens_used, 0)), 0) AS tokens,
                  COALESCE(SUM(COALESCE(a.cache_read_tokens, 0)), 0) AS cache_read,
                  SUM(CASE WHEN a.status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded
           FROM attempts a JOIN tasks t ON t.id = a.task_id
           GROUP BY tier ORDER BY attempts DESC""")
    by_tier = [
        {"tier": r[0], "attempts": r[1], "tokens": r[2], "cache_read": r[3],
         "succeeded": r[4] or 0}
        for r in await cur.fetchall()
    ]

    # Gate outcomes: review verdicts and what blocked. The rejection reasons
    # are the review-fail event texts — the operator's "why is it failing"
    # list, most recent first.
    review_pass = await one(
        "SELECT COUNT(*) FROM task_events WHERE "
        "json_extract(data, '$.kind') = 'review' "
        "AND json_extract(data, '$.passed') = 1")
    review_fail = await one(
        "SELECT COUNT(*) FROM task_events WHERE "
        "json_extract(data, '$.kind') = 'review' "
        "AND json_extract(data, '$.passed') = 0")
    cur = await db.execute(
        """SELECT substr(COALESCE(json_extract(data, '$.text'), ''), 1, 200)
           FROM task_events
           WHERE json_extract(data, '$.kind') = 'attempt_failed'
           ORDER BY ts DESC LIMIT 10""")
    rejection_reasons = [r[0] for r in await cur.fetchall() if r[0]]

    # Cache economics (P0.3): cache_creation is full-price input; cache_read
    # is ~10% price. A rising creation share means the prompt prefix is being
    # rebuilt instead of reused — the failure mode all context work must
    # avoid (93% of lifetime burn is coder cache-reads).
    cur = await db.execute(
        """SELECT COUNT(*),
                  COALESCE(SUM(COALESCE(cache_creation_tokens, 0)), 0),
                  COALESCE(SUM(COALESCE(cache_read_tokens, 0)), 0)
           FROM attempts WHERE COALESCE(cache_read_tokens, 0) > 0
              OR COALESCE(cache_creation_tokens, 0) > 0""")
    n_attempts, sum_creation, sum_read = await cur.fetchone()
    cache_economics = {
        "attempts_measured": n_attempts,
        "cache_creation_total": sum_creation,
        "cache_read_total": sum_read,
        "creation_per_attempt": sum_creation // n_attempts if n_attempts else 0,
        "read_per_attempt": sum_read // n_attempts if n_attempts else 0,
        "creation_share": round(sum_creation / (sum_creation + sum_read), 4)
        if (sum_creation + sum_read) else None,
    }

    # CI_GATE integration gate (M6): runs started / passed / failed.
    cur = await db.execute(
        """SELECT json_extract(data, '$.kind'), COUNT(*)
           FROM task_events
           WHERE json_extract(data, '$.kind')
                 IN ('ci_gate_trigger', 'ci_gate_pass', 'ci_gate_fail')
           GROUP BY 1""")
    ci_gate_raw = {r[0]: r[1] for r in await cur.fetchall()}
    ci_gate = {
        "triggered": ci_gate_raw.get("ci_gate_trigger", 0),
        "passed": ci_gate_raw.get("ci_gate_pass", 0),
        "failed": ci_gate_raw.get("ci_gate_fail", 0),
    }

    # Repro-gate verdict split (advisory data — decides when "required" ships).
    cur = await db.execute(
        """SELECT COALESCE(json_extract(data, '$.verdict'), '?'), COUNT(*)
           FROM task_events WHERE json_extract(data, '$.kind') = 'repro_gate'
           GROUP BY 1""")
    repro = {r[0]: r[1] for r in await cur.fetchall()}

    # Error-class breakdown (0.2/0.3): how terminal agent errors split, so the
    # wasted-attempt causes are visible — a refusal (fail-fast, needs a human)
    # vs a retryable rate-limit/infra vs a genuine error. Populated by
    # _classify_error; agent_error events from before it group as 'unclassified'.
    cur = await db.execute(
        """SELECT COALESCE(json_extract(data, '$.error_class'), 'unclassified'),
                  COUNT(*)
           FROM task_events WHERE json_extract(data, '$.kind') = 'agent_error'
           GROUP BY 1""")
    error_breakdown = {r[0]: r[1] for r in await cur.fetchall()}

    total_cache_read = sum(p["cache_read"] for p in by_profile)
    total_tokens = sum(p["tokens"] for p in by_profile)

    # The reviewer's burn, kept apart from the coder's so per-tier/per-profile attribution stays
    # honest — but surfaced, so the UI can finally price the whole run instead of the coder half.
    cur = await db.execute(
        """SELECT COALESCE(SUM(COALESCE(review_tokens_used, 0)), 0),
                  COALESCE(SUM(COALESCE(review_cache_creation_tokens, 0)), 0),
                  COALESCE(SUM(COALESCE(review_cache_read_tokens, 0)), 0)
           FROM attempts""")
    rev_used, rev_creation, rev_read = await cur.fetchone()
    return {
        "prs_opened": prs_opened or 0,
        "prs_merged": prs_merged or 0,
        "attempts_total": attempts_total or 0,
        "attempts_per_pr": round(attempts_total / prs_opened, 1) if prs_opened else None,
        "tokens_per_pr": (total_tokens + total_cache_read) // prs_opened if prs_opened else None,
        # The raw in+out total. `tokens_per_pr` folds it together with cache-read AND
        # divides by prs_OPENED, so it cannot be priced honestly on its own: cache-read is
        # a tenth of the price, cache-CREATION is not in it at all, and a "per merged PR"
        # figure needs prs_merged. Emitting the buckets lets one cost function serve the
        # per-PR tile, the lifetime tile and the task table, so they cannot disagree.
        "tokens_used_total": total_tokens,
        "review_tokens_used_total": rev_used or 0,
        "review_cache_creation_total": rev_creation or 0,
        "review_cache_read_total": rev_read or 0,
        "by_auth_profile": by_profile,
        "by_tier": by_tier,
        "review_pass": review_pass or 0,
        "review_fail": review_fail or 0,
        "recent_rejection_reasons": rejection_reasons,
        "repro_gate_verdicts": repro,
        "ci_gate": ci_gate,
        "cache_economics": cache_economics,
        "error_breakdown": error_breakdown,
    }
