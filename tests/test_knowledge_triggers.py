"""W3.4 knowledge triggers: a tagged memory injects only when relevant."""

from no_human.learning.triggers import (
    filter_triggered,
    memory_is_triggered,
    playbook_is_triggered,
    select_playbook,
)


def test_playbook_without_trigger_never_auto_matches():
    # unlike a memory (no tags = always inject), an untriggered playbook stays
    # manual-only — a heavy procedure must not attach to unrelated tasks.
    assert playbook_is_triggered({"trigger_keywords": "[]"}, "anything") is False
    assert playbook_is_triggered({"trigger_keywords": None}, "anything") is False


def test_playbook_matches_on_keyword_substring():
    pb = {"trigger_keywords": '["stripe", "payment"]'}
    assert playbook_is_triggered(pb, "Add Stripe support") is True
    assert playbook_is_triggered(pb, "fix a logging bug") is False


def test_select_playbook_returns_first_match_or_none():
    pbs = [
        {"title": "A", "trigger_keywords": '["kafka"]'},
        {"title": "B", "trigger_keywords": '["stripe"]'},
    ]
    assert select_playbook(pbs, "add stripe webhook")["title"] == "B"
    assert select_playbook(pbs, "unrelated task") is None


def test_untagged_memory_always_injects():
    """Backward compatibility: no tags = unconditional, exactly as before."""
    m = {"title": "always", "content": "x", "tags": None}
    assert memory_is_triggered(m, "anything at all") is True
    assert memory_is_triggered({"title": "y", "tags": "[]"}, "z") is True


def test_tagged_memory_injects_only_on_match():
    m = {"title": "kafka rule", "tags": '["kafka", "broker"]'}
    assert memory_is_triggered(m, "Fix the Kafka topic creation") is True   # case-insensitive
    assert memory_is_triggered(m, "Update the UI button color") is False


def test_tags_accept_list_or_json_or_junk():
    assert memory_is_triggered({"tags": ["mtls"]}, "fix mTLS cert") is True
    assert memory_is_triggered({"tags": "not json"}, "anything") is True  # junk → unconditional
    assert memory_is_triggered({"tags": '["  "]'}, "anything") is True    # blank tag → unconditional


def test_filter_triggered_partitions_the_set():
    mems = [
        {"title": "global", "tags": None},
        {"title": "kafka", "tags": '["kafka"]'},
        {"title": "clickhouse", "tags": '["clickhouse"]'},
    ]
    out = filter_triggered(mems, "debug the kafka consumer lag")
    titles = {m["title"] for m in out}
    assert titles == {"global", "kafka"}  # clickhouse held back


def test_provenance_tags_are_filter_only_not_triggers():
    # vocab.py: ORIGIN_* tags name where a lesson came from, not what it is
    # about — "address the review comments" must not summon every
    # review-origin lesson in the store.
    m = {"title": "digest pinning", "tags": '["review", "container"]'}
    assert memory_is_triggered(m, "address the review comments on the docs") is False
    assert memory_is_triggered(m, "pin the docker image digest") is True


def test_a_provenance_only_memory_never_auto_injects():
    # No topical tag = nothing to match a task against. It stays visible in
    # `nh learnings` (filterable by producer); it does not ride every prompt.
    m = {"title": "origin only", "tags": '["review"]'}
    assert memory_is_triggered(m, "review the code") is False
    assert memory_is_triggered(m, "anything else") is False
    sup = {"title": "sup only", "tags": '["supervisor"]'}
    assert memory_is_triggered(sup, "the supervisor said so") is False


def test_generic_aliases_do_not_trigger():
    # "path"/"env"/"json"/"request" appear in half the queue's task text; a
    # lesson tagged environment/api fires on its specific terms, not those.
    env_lesson = {"title": "venv trap", "tags": '["environment"]'}
    assert memory_is_triggered(env_lesson, "update the api request path handling") is False
    assert memory_is_triggered(env_lesson, "the venv was built against main") is True
    api_lesson = {"title": "endpoint 500", "tags": '["api"]'}
    assert memory_is_triggered(api_lesson, "fix the json parsing in the config loader") is False
    assert memory_is_triggered(api_lesson, "the endpoint returns 500") is True
