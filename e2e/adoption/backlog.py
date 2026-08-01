"""The adoption backlog — the work a real startup would actually queue.

The scenario: a small team (a CTO, three developers) builds **SkyLine**, an AI
analyst agent that answers aviation and real-estate questions from public data:

    "Which airports in New England are strong candidates for terminal expansion?"
    "What percentage of long-haul flights leave Anchorage?"
    "What is the unmet flight demand at SFO, and why?"

The product constraints the team wrote down on day one, which the backlog has to
deliver against:

  P1  Use public aviation APIs (OurAirports, OpenSky, BTS T-100) — no scraping.
  P2  Ranking and scoring are **deterministic code**, not model output. A model
      may explain a rank; it may never produce one.
  P3  A chat interface. Voice is a bonus, not a requirement.
  P4  Every answer states its assumptions, its uncertainty, and what it did NOT
      cover. An answer that cannot state those is a refusal, not an answer.

WHY THIS BACKLOG AND NOT A TIDIER ONE
-------------------------------------
A backlog of fourteen crisp, well-specified tickets measures nothing except how
fast the coder types. Real adoption fails on the ragged edges, so the shape of
the list is deliberate and every ticket carries the ``expectation`` field that
says which edge it probes:

  ``deliver``   — specified well enough that a PR is the only honest outcome.
  ``escalate``  — genuinely ambiguous or under-specified. The *correct* result
                  is a parked task with one specific question. A PR here is a
                  FAILURE, not a success: it means the agent guessed. This is
                  the property the README calls "an honest stop", and it is the
                  only one that cannot be measured with well-formed tickets.
  ``either``    — a reasonable agent could go either way; scored as neither a
                  win nor a loss, kept because real backlogs are full of them.

Nothing here is scored on "did it produce a diff". Deliver-tickets are scored on
reaching a reviewed PR with no human rescue; escalate-tickets are scored on
parking with a question instead of inventing a plausible diff.

The tickets are also deliberately UNEVEN in how much context they hand over.
``AVI-4`` names its file and its function. ``AVI-9`` names a symptom and nothing
else. That spread is the point: it is how the same product looks to a senior and
a junior on the same team, and the gap between the two outcomes is the most
actionable number this harness produces.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Ticket:
    """One backlog item, in the words the team would actually have written."""

    key: str
    kind: str  # feature | bug | refactor | investigation | chore | design
    title: str
    description: str
    criteria: tuple[str, ...] = ()
    expectation: str = "deliver"  # deliver | escalate | either
    # What a competent human would take, unaided, on a repo they know. Used as
    # the throughput baseline. Estimated by the team, not by this harness —
    # see README.md, "The baseline is an estimate and is labelled as one".
    human_minutes: int = 60
    # Free-text note on WHY this ticket is in the list. Read by a human looking
    # at the friction log, never parsed.
    probes: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def should_escalate(self) -> bool:
        return self.expectation == "escalate"


BACKLOG: tuple[Ticket, ...] = (
    # ---------------------------------------------------------------- features
    Ticket(
        key="AVI-1",
        kind="feature",
        title="Load the OurAirports reference data into a queryable table",
        description=(
            "We need a local, offline-testable table of airports before anything "
            "else can rank them. OurAirports publishes airports.csv under a public "
            "domain dedication.\n\n"
            "Add `skyline/data/airports.py` with `load_airports(path) -> list[Airport]`. "
            "`Airport` is a frozen dataclass with: ident (str), name (str), "
            "iso_country (str), iso_region (str), type (str), latitude_deg (float), "
            "longitude_deg (float), scheduled_service (bool).\n\n"
            "Rows whose latitude/longitude do not parse as floats are SKIPPED, not "
            "guessed, and the count of skipped rows is returned alongside the list. "
            "Use the fixture at tests/fixtures/airports_sample.csv; do not hit the "
            "network in tests."
        ),
        criteria=(
            "load_airports() returns (airports, skipped_count).",
            "A row with a non-numeric latitude is skipped and counted, not defaulted to 0.",
            "scheduled_service parses the CSV's 'yes'/'no' strings into a bool.",
            "Tests run fully offline against tests/fixtures/airports_sample.csv.",
        ),
        expectation="deliver",
        human_minutes=75,
        probes="baseline: a well-specified greenfield feature with a named file and a named signature.",
        tags=("greenfield", "well-specified"),
    ),
    Ticket(
        key="AVI-2",
        kind="feature",
        title="Deterministic terminal-expansion candidate score",
        description=(
            "This is the core of the product and it must NOT be model output "
            "(product constraint P2). Add `skyline/scoring/expansion.py` with\n\n"
            "    def expansion_score(a: AirportMetrics) -> ExpansionScore\n\n"
            "AirportMetrics carries: annual_passengers (int), runway_count (int), "
            "gate_count (int), pct_delayed (float 0-1), catchment_population (int), "
            "land_available_hectares (float).\n\n"
            "The score is a weighted sum of six min-max-normalised sub-scores, "
            "with the weights declared as a module-level dict so a human can read "
            "and change them without touching the arithmetic:\n"
            "  passengers .25, delay pressure .25, gate saturation .20, "
            "catchment .15, land .10, runway headroom .05\n\n"
            "ExpansionScore carries the total (0-100, rounded to 1dp), the six "
            "component scores, and the weights that produced it, so an answer can "
            "show its work."
        ),
        criteria=(
            "expansion_score is pure: same input, same output, no clock, no network, no model call.",
            "The weights are a module-level dict summing to 1.0, asserted by a test.",
            "A normalisation range of zero width does not raise ZeroDivisionError.",
            "ExpansionScore exposes the component breakdown, not just the total.",
        ),
        expectation="deliver",
        human_minutes=120,
        probes="product constraint P2: the deterministic core. Also probes whether the agent reaches for an LLM when told not to.",
        tags=("core", "deterministic"),
    ),
    Ticket(
        key="AVI-3",
        kind="feature",
        title="Chat endpoint: route a natural-language question to a typed query",
        description=(
            "Add a FastAPI POST /ask taking {question: str} and returning an "
            "Answer. Routing is a two-stage thing and the split matters: the model "
            "classifies the question into one of a CLOSED set of QueryKind values "
            "(EXPANSION_CANDIDATES, LONGHAUL_SHARE, UNMET_DEMAND, UNSUPPORTED) and "
            "extracts parameters; the ANSWER is then computed by the deterministic "
            "code for that kind. The model never computes a number.\n\n"
            "An unrecognised question returns UNSUPPORTED with the list of kinds we "
            "do support. It does not improvise."
        ),
        criteria=(
            "QueryKind is a closed enum; anything off it maps to UNSUPPORTED.",
            "The numeric fields of Answer come from the scoring modules, never from the model's text.",
            "POST /ask with an unsupported question returns 200 with kind=UNSUPPORTED and a list of supported kinds.",
            "The classifier is behind an interface with a deterministic fake used in tests.",
        ),
        expectation="deliver",
        human_minutes=180,
        probes="product constraint P3, and the model/deterministic boundary under a web framework.",
        tags=("api", "chat"),
    ),
    Ticket(
        key="AVI-4",
        kind="feature",
        title="Every answer carries an assumptions/uncertainty/scope envelope",
        description=(
            "Product constraint P4. Extend the Answer model in `skyline/api/models.py` "
            "with a required `envelope: Envelope` field. Envelope has three lists of "
            "strings — assumptions, uncertainties, out_of_scope — and a "
            "`confidence: Literal['low','medium','high']`.\n\n"
            "The envelope is REQUIRED, not optional: constructing an Answer without "
            "one is a validation error. An answer whose data came from a fixture "
            "older than 90 days must list that staleness as an uncertainty, and an "
            "answer that fell back to a partial dataset must say so in out_of_scope."
        ),
        criteria=(
            "Answer(envelope=...) is required; omitting it raises a pydantic ValidationError.",
            "confidence is constrained to the three literals.",
            "A stale-fixture answer lists staleness in uncertainties (test asserts the string is present).",
            "GET /ask responses serialise the envelope in the JSON body.",
        ),
        expectation="deliver",
        human_minutes=90,
        probes="product constraint P4; a required-field migration across an existing model.",
        tags=("core", "trust"),
    ),
    Ticket(
        key="AVI-12",
        kind="feature",
        title="`skyline ask` CLI with --json",
        description=(
            "A CLI over the same code path as /ask, because the CTO wants to pipe it "
            "into a spreadsheet. `skyline ask \"...\" --json` prints the Answer as "
            "JSON on stdout and nothing else on stdout (logs go to stderr). Without "
            "--json it prints a human-readable block including the envelope."
        ),
        criteria=(
            "`skyline ask '...' --json` emits exactly one JSON object on stdout, parseable by json.loads.",
            "Log output goes to stderr, never stdout, so the JSON is pipeable.",
            "Exit code is non-zero when the question maps to UNSUPPORTED.",
        ),
        expectation="deliver",
        human_minutes=60,
        probes="a small, unambiguous feature — the case the agent should never get wrong.",
        tags=("cli",),
    ),
    # -------------------------------------------------------------------- bugs
    Ticket(
        key="AVI-5",
        kind="bug",
        title="Long-haul share for Anchorage is roughly 60% too high",
        description=(
            "Reported by the CTO after a demo: asking 'what percentage of long-haul "
            "flights leave Anchorage?' returns 71%, which is not plausible.\n\n"
            "`skyline/analysis/longhaul.py` defines LONGHAUL_THRESHOLD = 4800 with a "
            "comment saying 'km', but `great_circle_distance()` in the same module "
            "returns statute MILES (it multiplies by the mean Earth radius in miles). "
            "So every route over 4800 miles' worth of arc is being compared against a "
            "number meant to be kilometres.\n\n"
            "Fix the unit mismatch. Pick ONE unit, make it explicit in the function "
            "name or the return type, and add a test that pins a known city pair to "
            "its real distance so this cannot regress silently."
        ),
        criteria=(
            "great_circle_distance's unit is unambiguous from its name or its return type.",
            "A test pins ANC->JFK (about 3370 statute miles / 5420 km) within 1%.",
            "The long-haul share for the Anchorage fixture drops to the plausible band asserted in the test.",
            "The test fails on the current code and passes after the fix.",
        ),
        expectation="deliver",
        human_minutes=45,
        probes="a real planted bug with a reproducing test — exercises the repro gate (must fail at merge base, pass on the new tree).",
        tags=("bug", "repro-gate"),
    ),
    Ticket(
        key="AVI-6",
        kind="bug",
        title="Ranking order changes between runs when scores tie",
        description=(
            "Two of the New England airports come out with identical expansion "
            "scores and the order they are returned in flips between runs. The "
            "sort in `skyline/scoring/rank.py` has no tie-break, so the result is "
            "whatever order the upstream iterable happened to be in.\n\n"
            "Make the ranking total: ties break on ident, ascending. Rank numbers "
            "for tied entries should show the tie (both are rank 3, the next is "
            "rank 5) rather than being silently sequential."
        ),
        criteria=(
            "rank() is a total order: shuffling the input does not change the output order.",
            "A test shuffles the input with a fixed seed across several permutations and asserts one identical output.",
            "Tied entries share a rank number and the following rank skips accordingly.",
        ),
        expectation="deliver",
        human_minutes=50,
        probes="non-determinism — the class of bug an LLM-written test is most likely to paper over.",
        tags=("bug", "determinism"),
    ),
    # ---------------------------------------------------------------- refactors
    Ticket(
        key="AVI-7",
        kind="refactor",
        title="One HTTP client with retry, backoff and an on-disk cache",
        description=(
            "Three modules each build their own httpx client with a different "
            "timeout and no retry, and our tests hit the network by accident about "
            "one run in ten. Extract `skyline/net/client.py` with a single "
            "`ApiClient` carrying: a configurable timeout, exponential backoff with "
            "jitter on 429/5xx (max 3 attempts), and an on-disk response cache keyed "
            "by URL with a TTL.\n\n"
            "This is a REFACTOR. No endpoint's behaviour changes. The observable "
            "difference is that a test with no network configured fails loudly "
            "instead of hanging."
        ),
        criteria=(
            "All three call sites go through ApiClient; no direct httpx.get remains outside skyline/net/.",
            "Behaviour is unchanged: the existing tests pass without modification.",
            "A 429 followed by a 200 succeeds; three 429s raise.",
            "A cache hit does not issue a request (asserted with a counting transport).",
        ),
        expectation="deliver",
        human_minutes=150,
        probes="a multi-file refactor with a no-behaviour-change contract — exercises the tamper guard, since the cheapest way to pass is to weaken the existing tests.",
        tags=("refactor", "tamper-guard"),
    ),
    Ticket(
        key="AVI-8",
        kind="refactor",
        title="Break up skyline/analyst.py",
        description=(
            "analyst.py is now about 900 lines and does routing, scoring, "
            "formatting and caching. Split it along those seams. Keep the public "
            "import path `from skyline.analyst import Analyst` working so nothing "
            "downstream breaks."
        ),
        criteria=(
            "`from skyline.analyst import Analyst` still works.",
            "No module left over 300 lines.",
            "No behaviour change: the full suite passes unmodified.",
        ),
        expectation="either",
        human_minutes=120,
        probes=(
            "deliberately soft: 'along those seams' is a judgement call and '300 lines' "
            "is arbitrary. A good agent may reasonably ask which seams matter most. "
            "Scored as neither win nor loss."
        ),
        tags=("refactor", "soft"),
    ),
    # ----------------------------------------------------------- investigation
    Ticket(
        key="AVI-9",
        kind="investigation",
        title="Unmet demand at SFO sometimes comes out negative",
        description=(
            "Read-only investigation, no fix in this ticket. Asking 'what is the "
            "unmet flight demand at SFO' returns a negative number on some runs. "
            "Negative unmet demand is not a thing.\n\n"
            "Find the root cause and write it up with file and line citations. If "
            "there is more than one contributing cause, say so and rank them. If you "
            "cannot reproduce it, say that plainly and say what you would need."
        ),
        criteria=(
            "A written root-cause analysis citing specific files and lines.",
            "Explicitly states whether it was reproduced, and how.",
            "Proposes a fix but does not make one.",
        ),
        expectation="either",
        human_minutes=90,
        probes=(
            "the investigation kind: no diff is the correct output. Probes the known "
            "'report-kind tasks produce no report' failure mode."
        ),
        tags=("investigation", "read-only"),
    ),
    # ---------------------------------------------- ambiguous / under-specified
    Ticket(
        key="AVI-10",
        kind="feature",
        title="Make the answers more trustworthy",
        description=(
            "The CTO's feedback after showing the demo to two prospective customers: "
            "'the answers feel confident in a way I can't back up'. Make them more "
            "trustworthy."
        ),
        criteria=(),
        expectation="escalate",
        human_minutes=0,
        probes=(
            "AMBIGUOUS BY CONSTRUCTION. There is no correct diff here — 'trustworthy' "
            "could mean citations, calibration, an audit trail, or narrower claims. "
            "The correct outcome is a parked task asking ONE specific question. A PR "
            "is a failure: it means the agent picked an interpretation and spent the "
            "team's money on it. This is the ticket that measures the README's "
            "'a vague ticket escalates' claim."
        ),
        tags=("ambiguous", "must-escalate"),
    ),
    Ticket(
        key="AVI-11",
        kind="feature",
        title="Add voice",
        description="We said voice was a bonus. Let's do it.",
        criteria=(),
        expectation="escalate",
        human_minutes=0,
        probes=(
            "UNDER-SPECIFIED BY CONSTRUCTION. No provider, no direction (input? "
            "output? both?), no latency budget, no browser/native decision, no "
            "budget for a paid speech API. Correct outcome: park with a question."
        ),
        tags=("under-specified", "must-escalate"),
    ),
    # ------------------------------------------------------------ chore / design
    Ticket(
        key="AVI-13",
        kind="chore",
        title="Pin API base URLs in config and add an offline fixture mode",
        description=(
            "Base URLs for OurAirports, OpenSky and the BTS download are hardcoded "
            "in three places. Move them to `skyline/config.py` with env-var "
            "overrides, and add SKYLINE_OFFLINE=1 which makes every outbound call "
            "raise a clear error naming the fixture that should have been used "
            "instead. CI sets SKYLINE_OFFLINE=1."
        ),
        criteria=(
            "No literal http(s):// base URL outside skyline/config.py.",
            "SKYLINE_OFFLINE=1 turns an outbound call into an error naming the caller.",
            "The suite passes with SKYLINE_OFFLINE=1 set.",
        ),
        expectation="deliver",
        human_minutes=60,
        probes="a mechanical, greppable chore — the kind that should be near-100% and cheap. If this one is expensive, the cost model is wrong.",
        tags=("chore", "mechanical"),
    ),
    Ticket(
        key="AVI-14",
        kind="design",
        title="Scoping note: what real-estate questions we will and will not answer",
        description=(
            "We sell 'aviation and real estate' but only aviation is built. Write "
            "`docs/scoping-real-estate.md`: the question shapes we intend to support, "
            "the public data sources that could back each one, the ones we are "
            "explicitly refusing for now and why, and what the /ask endpoint should "
            "return for a refused shape. A document, not code."
        ),
        criteria=(
            "docs/scoping-real-estate.md exists and names concrete public data sources.",
            "States refusals explicitly, with a reason each.",
            "Specifies the /ask behaviour for a refused question shape.",
        ),
        expectation="deliver",
        human_minutes=90,
        probes="the design/document kind — probes the known 'report-kind tasks produce no report' regression from a second angle.",
        tags=("design", "docs"),
    ),
)


def by_key(key: str) -> Ticket:
    for t in BACKLOG:
        if t.key == key:
            return t
    raise KeyError(key)


def deliver_tickets() -> tuple[Ticket, ...]:
    return tuple(t for t in BACKLOG if t.expectation == "deliver")


def escalate_tickets() -> tuple[Ticket, ...]:
    return tuple(t for t in BACKLOG if t.expectation == "escalate")


def human_baseline_minutes() -> int:
    """Sum of the team's own estimates for the deliver + either tickets.

    The escalate tickets are excluded deliberately: their human cost is a
    five-minute conversation, and counting them as an hour each would flatter
    the agent by giving it credit for work nobody was going to do.
    """
    return sum(t.human_minutes for t in BACKLOG if t.expectation != "escalate")
