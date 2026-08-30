from no_human.api.title_short import title_short


def test_short_title_is_returned_unchanged():
    assert title_short("Fix login redirect loop") == "Fix login redirect loop"


def test_cuts_at_first_separator_when_long():
    t = "Merge-ready verdict in the hands of the human, part 2: a MERGE-READY chip on the board card and a per-task banner"
    assert title_short(t) == "Merge-ready verdict in the hands of the human, part 2"


def test_hard_limit_with_ellipsis_when_no_separator():
    t = "a" * 100
    out = title_short(t)
    assert len(out) == 60 and out.endswith("…")


def test_strips_leading_path_and_quotes():
    t = "`docs/DISTRIBUTION.md` describes a private repo — fix the wording and other things too"
    assert title_short(t) == "docs/DISTRIBUTION.md describes a private repo"


def test_empty_title_stays_empty():
    assert title_short("") == ""


def test_short_title_with_colon_is_returned_unchanged():
    assert title_short("Fix: login redirect loop") == "Fix: login redirect loop"
