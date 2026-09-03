"""Verification receipts — what the coder session ran, and what came back.

WHAT A RECEIPT IS. A receipt records that a command **line was submitted to the
shell** and the text the harness returned for it. It is built by a PostToolUse
observer straight from the tool result, so the model does not author a receipt
and cannot edit one after the fact.

SUBMITTED, NOT EXECUTED, and every round of this module until now conflated the
two. A receipt is headed by the kind of the check `classify` RECOGNISES in the
line, and recognition is textual: `/usr/bin/false && pytest -q`, `echo ok || pytest
-q`, `exit 1<LF>mvn -B test` and `cd repo<LF>&& mvn -B test` each yield a `test`
receipt, and in each one bash never reached the check. The last is a SYNTAX
ERROR, and a syntax error does not mean bash ran nothing at all: bash's loop is
read-parse-execute per complete command, so it executes what came BEFORE the
error and then aborts the rest of the line. Driven: `bash -c "touch M<LF>&& mvn
-B test"` exits 2 with `syntax error near unexpected token '&&'` and `M` EXISTS
afterwards. What never runs is the check after the error.
Driven against bash 3.2.57 with the check replaced by
a marker-printing stub: rc 1 / 0 / 1 / 2, marker absent in all four. Nothing
here models control flow, so the rendered limits list says so in words and
`test_the_limits_list_describes_the_code_that_exists` holds it against these
four shapes.

**THERE IS NO VERDICT HERE, AND THAT IS THE DESIGN.** Six independent reviews
failed the earlier version of this module, every one of them on the same thing:
a per-command PASS/FAIL/UNKNOWN badge that a shell could be made to lie about.
Each round closed one door — `;`, then a newline, then a punctuation run, then a
heredoc body, then a function definition, then a trailing `&` and
`eval "x=$(pytest -q)"` — and each round a new door opened, because deciding
whether a zero exit belongs to the checked program means parsing bash, and this
module is not bash.

The measurement that settled it: of 3,517 real Bash commands mined from local
transcripts, 292 were verification commands, and the badge could justify a PASS
for **6 of them (2.1%)**. The other 286 were correctly downgraded to UNKNOWN. A
badge that earns its keep on 2% of real commands, while being the sole source of
every critical defect the branch was failed for, is not worth its blast radius.

So the section shows the command and its captured output, and the human reads
`1 failed, 42 passed` for themselves. What this module still guarantees:

* **A receipt is not authored by the model.** The observer writes it from the
  tool result; the coder cannot add, edit or delete a stored row.
* **Its CONTENT is chosen by the model and is treated as hostile.** The coder
  picks the command string and, through `echo`/`printf`, its output too. A
  command that genuinely ran was used by a review to emit a fake
  `### Manual UI verification` heading with hand-written PASS lines, inside the
  very section whose premise is that the model did not write it. Everything
  rendered from a receipt is therefore neutralised as untrusted text
  (`md_inline_code` / `md_fence`) — no headings, no bold, no list markers, no
  fence escapes, no invisible or bidirectional characters. Read the `md_*`
  helpers before changing any renderer.
* **A receipt CAN be absent, and no code here prevents that.** Every one of
  these leaves NO receipt: a command the classifier does not recognise as a
  check (`_classify_segment` returns None); a command run inside a spawned
  subagent (`agent_id`, skipped on purpose); a command the HARNESS backgrounded
  (`backgroundTaskId` — it has not finished, and a trailing `&` you wrote
  yourself is NOT this: it is recorded like any other line); a command the
  harness refused to
  run (blocked/permission). Anything past the ``RECEIPT_CAP``-th recognised
  command in an attempt is likewise not recorded. The rendered section states
  all of this in words, unconditionally, because a limitation the human cannot
  see is not disclosed.

THE PAYLOAD SHAPES WERE MEASURED, NOT ASSUMED - the discipline
`tool_result_cap.py` demands. Across ~2,400 real Bash tool results in local
transcripts:

    success   -> DICT {"stdout","stderr","interrupted","isImage","noOutputExpected"}
    failure   -> STRING "Error: Exit code <N>\\n..."   (71 of 92 failures)
    blocked   -> STRING "Error: Blocked: ..." / "Error: Permission ... denied"
                 (21 of 92) - the command NEVER RAN, so it yields no receipt.

Three dict shapes are not plain results either, all measured in real transcripts:
``backgroundTaskId`` (100 payloads — still running, no receipt),
``timedOutAfterMs`` (3) and ``returnCodeInterpretation`` (48 — the harness
wording a NON-zero exit). The last two are recorded, with the harness's own
report appended to the captured text so it is visible rather than dropped.

CLASSIFICATION IS RECOGNITION, NOT EXECUTION ANALYSIS. `_segments` splits a
command line on shell separators with `shlex` (stdlib, quote-aware) purely so a
check can be FOUND in `cd repo && pytest -q | tail -3`. It decides which
commands get recorded and under which heading; nothing downstream reads a status
out of it, so it is allowed to be approximate in both directions, and the
rendered limits list says so. Do not grow a verdict on top of it.
"""

from __future__ import annotations

import base64
import html
import os
import re
import shlex
from dataclasses import dataclass
from typing import Any, Callable

#: The kinds a receipt may carry, in the order the PR section presents them.
KINDS: tuple[str, ...] = ("test", "e2e", "http", "typecheck", "lint", "build")


# -- classification: on the PROGRAM, never on a substring ------------------- #
#
# The first version searched the whole command line for `\bpytest\b`, so
# `find /tmp/pytest-of-ci/pytest-216 -name '*.py'` classified as a test run. A
# path is not a program. Every rule below is applied to the resolved program
# name of a pipeline segment and its arguments.

#: Wrappers that delegate to a real program. The set is the subcommand that must
#: follow for the tokens to be stripped; an empty set means "always strip one".
_WRAPPERS: dict[str, set[str]] = {
    "uv": {"run"}, "poetry": {"run"}, "pipenv": {"run"}, "bundle": {"exec"},
    "pdm": {"run"}, "rye": {"run"}, "hatch": {"run"},
    "npx": set(), "pnpx": set(), "sudo": set(), "time": set(), "nice": set(),
    "env": set(), "command": set(), "xvfb-run": set(), "dotenv": set(),
    "timeout": set(), "stdbuf": set(), "ionice": set(), "doas": set(),
}

#: Options of a WRAPPER that mean NOTHING WAS EXECUTED. `command -v pytest`
#: prints a path and runs no test at all, yet `command` is a wrapper here, so
#: without this the tokens are stripped and `pytest` becomes "the program".
_WRAPPER_LOOKUP_FLAGS: dict[str, frozenset[str]] = {
    "command": frozenset({"-v", "-V"}),
}

#: Options of a WRAPPER that consume the following word. Without these,
#: `nice -n 10 pytest` resolved to the program `-n` and produced no receipt at
#: all — as did `env -i`, `timeout 60`, `sudo -u ci` and the e2e case
#: `xvfb-run -a playwright test`. A wrapper that swallows the receipt is a
#: silent suppression channel, which is the one thing this module may not have.
_WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "--class", "-n", "--classdata"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "sudo": frozenset({"-u", "--user", "-g", "--group", "-p", "--prompt",
                       "-C", "--close-from", "-D", "--chdir", "-R", "--chroot",
                       "-T", "--command-timeout", "-U", "--other-user",
                       "-h", "--host"}),
    "doas": frozenset({"-u", "-C"}),
    "xvfb-run": frozenset({"-n", "--server-num", "-s", "--server-args",
                           "-f", "--auth-file", "-e", "--error-file"}),
    "uv": frozenset({"--with", "--with-requirements", "--python", "-p",
                     "--project", "--directory", "--index", "--extra",
                     "--group", "--package", "--isolated-python"}),
    "npx": frozenset({"-p", "--package", "-c", "--call"}),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "poetry": frozenset({"-C", "--directory", "-P", "--project"}),
    "hatch": frozenset({"-e", "--env", "-p", "--project"}),
}

#: `timeout` takes a mandatory DURATION positional before the program.
_DURATION = re.compile(r"[0-9]+(?:\.[0-9]+)?[smhd]?")
#: Package managers whose SCRIPT NAME decides the kind (`npm run test:e2e`).
_SCRIPT_RUNNERS = {"npm", "yarn", "pnpm", "bun"}

_SCRIPT_KIND: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("e2e", re.compile(r"^(e2e|test:e2e|integration|test:integration)$", re.I)),
    ("typecheck", re.compile(r"^(typecheck|type-check|types|tsc)$", re.I)),
    ("lint", re.compile(r"^(lint|lint:fix|format:check|eslint)$", re.I)),
    ("build", re.compile(r"^(build|compile|bundle)$", re.I)),
    ("test", re.compile(r"^(test|tests|test:unit|unit|jest|vitest|check)$", re.I)),
)

#: program -> kind, when the program alone is decisive.
_PROGRAM_KIND: dict[str, str] = {
    "pytest": "test", "py.test": "test", "nose2": "test", "tox": "test",
    "jest": "test", "vitest": "test", "mocha": "test", "ava": "test",
    "rspec": "test", "phpunit": "test", "ctest": "test", "unittest": "test",
    "playwright": "e2e", "cypress": "e2e", "testcafe": "e2e",
    "nightwatch": "e2e", "codeceptjs": "e2e",
    "mypy": "typecheck", "pyright": "typecheck", "tsc": "typecheck",
    "pyre": "typecheck", "flow": "typecheck", "ty": "typecheck",
    "ruff": "lint", "flake8": "lint", "pylint": "lint", "eslint": "lint",
    "golangci-lint": "lint", "rubocop": "lint", "shellcheck": "lint",
    "biome": "lint", "stylelint": "lint",
    "curl": "http", "wget": "http", "http": "http", "https": "http",
    "httpie": "http", "xh": "http",
    "webpack": "build", "vite": "build", "rollup": "build", "esbuild": "build",
}

#: (program, subcommand) -> kind, for multi-tool CLIs.
_SUBCOMMAND_KIND: dict[tuple[str, str], str] = {
    ("go", "test"): "test", ("cargo", "test"): "test", ("dotnet", "test"): "test",
    ("swift", "test"): "test", ("mix", "test"): "test", ("rebar3", "eunit"): "test",
    ("go", "build"): "build", ("cargo", "build"): "build",
    ("dotnet", "build"): "build", ("swift", "build"): "build",
    ("cargo", "clippy"): "lint", ("go", "vet"): "lint",
    ("cargo", "check"): "typecheck",
}

#: The characters `shlex(punctuation_chars=True)` accumulates into their own
#: tokens. A token made ENTIRELY of these separates two segments; `&&`, `);` and
#: `>(` all arrive as one such run and all end a segment.
_PUNCTUATION = frozenset("();<>|&")


def _join_continuations(text: str) -> str:
    """Remove bash LINE CONTINUATIONS — and only where bash removes them.

    A backslash immediately followed by a newline is deleted, both characters,
    outside quotes and inside double quotes. Inside SINGLE quotes both survive,
    because bash keeps them there; the same holds for ``$'...'``, which this
    scanner sees as a single-quoted run and therefore gets right for free.

    NEWLINE HERE MEANS LF AND ONLY LF. A backslash before a CARRIAGE RETURN is
    an escaped CR — an ordinary character in the word — and the LF that follows
    it is a plain command separator, so `\\<CR><LF>` is TWO commands to bash
    where `\\<LF>` is one. `_segments` used to fold `\\r\\n` to `\\n` before
    calling this, which manufactured a continuation bash does not have: it
    folded `mvn -B \\<CR><LF> test` into the single argv `mvn -B test` and
    emitted a `test` receipt for a maven run that never got a test goal. A
    receipt is a claim that something ran, so a false one is worse than a
    missing one; the fold is gone and this function now sees the raw bytes.

    This runs BEFORE the newline -> `;` substitution in `_segments`, and it has
    to: without it, `mvn -B \\<newline> test` reached `shlex` as ``mvn -B \\; test``,
    `shlex` un-escaped ``\\;`` to a bare `;`, and `;` is in `_PUNCTUATION`, so a
    single command was split into three segments and `mvn ... test` classified
    as nothing at all. A 214-test Maven run left NO receipt. `pytest` never
    showed it because its identifying token sits in the FIRST segment and
    survives any split; every other build tool puts its subcommand after a
    continuation.

    Each rule below was checked against real bash (`bash` 3.2, argv printed by
    a helper script), including the mirror-image hazard — a global strip would
    have deleted the backslash-newline that `echo 'a\\<newline>b'` must keep, which
    is the same suppression class pointing the other way:

        mvn -B \\<newline> -DskipITs \\<newline> test  -> argc 4: mvn -B -DskipITs test
        'a\\<newline>b'                             -> argc 1: a\\<newline>b   (both kept)
        "a\\<newline>b"                             -> argc 1: ab          (removed)
        $'a\\<newline>b'                            -> argc 1: a\\<newline>b   (both kept)
        mvn -B \\\\<newline> test                    -> argc 3: mvn -B \\  + a
                                                   SEPARATE command `test`
        \\'mvn -B \\<newline> test                   -> argc 3: 'mvn -B test

    and the CARRIAGE-RETURN family, driven the same way (`bash` 3.2.57, argv via
    a shim, quoted words via ``printf %q``):

        mvn -B \\<CR><LF> test                 -> argc 2: mvn -B $'\\r'  + a
                                                SEPARATE command `test`
        mvn -B \\<CR>test                      -> argc 2: mvn -B $'\\rtest'
        mvn -B te<CR>st                      -> argc 2: mvn -B $'te\\rst'
        "py\\<CR><LF>test"                     -> argc 1: $'py\\\\\\r\\ntest'
                                                (both bytes kept; NOT `pytest`)
        "py\\<LF>test"                         -> argc 1: pytest

    WHERE THIS SCANNER IS APPROXIMATE. It reads quotes and backslash escapes and
    nothing else, so a heredoc body is treated as unquoted text: `<<EOF` matches
    bash (the body IS continued), `<<'EOF'` does not (bash keeps the two
    characters there, this drops them). That is a rendering difference inside a
    quoted string, never a lost receipt, and it is the same class of
    over-recognition the `_segments` docstring already discloses for heredocs.
    """
    out: list[str] = []
    quote: str | None = None
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote == "'":
            # No escapes at all inside single quotes: a backslash is a backslash.
            if ch == "'":
                quote = None
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            if text[i + 1] == "\n":
                i += 2          # the continuation itself — drop both characters
                continue
            # Any other escaped character is carried through UNTOUCHED, which is
            # what keeps `\\` from being read as the start of a continuation and
            # what stops `\'` from opening a quoted run.
            out.append(ch)
            out.append(text[i + 1])
            i += 2
            continue
        if quote is None and ch in ("'", '"'):
            quote = ch
        elif quote == '"' and ch == '"':
            quote = None
        out.append(ch)
        i += 1
    return "".join(out)


def _segments(command: str) -> list[list[str]]:
    """Split a command line into argv lists, one per pipeline/list segment.

    RECOGNITION ONLY. This exists so `pytest` can be found in
    `cd repo && pytest -q | tail -3`, and so `cd repo` on one line and
    `uv run pytest -q` on the next are two commands rather than one unclassified
    blur. Nothing derives a status from it.

    `shlex` does the quoting, escapes and comment stripping; newlines are
    normalised to `;` FIRST so a newline separates segments the way the shell
    treats it, and a newline inside a quoted string stays inside the word
    because `shlex` is the thing tracking the quotes. Being precise about that
    last part: the substitution runs over the raw text, so a newline inside
    quotes becomes a literal `;` IN THE TOKEN — `bash -c 'pytest -q\\nruff
    check .'` yields the single word ``pytest -q;ruff check .``. The word does
    not split, which is the property that matters.

    TWO REVIEWS HAVE NOW CORRECTED THE SENTENCE THAT USED TO FOLLOW, so read the
    history before writing a third. Round 2 struck the claim that the altered
    character "only ever sits inside an argv word that no rule here reads": the
    env-assignment strip in `_strip_wrappers` matches
    ``[A-Za-z_][A-Za-z0-9_]*=.*``, and `.` matches `;` but not a newline, so
    ``FOO='a\\nb' pytest -q`` becomes the word ``FOO=a;b``, IS stripped as an
    assignment, and classifies as `test` where the raw newline would have left it
    unrecognised. That much is still true, and it is one reason the substitution
    earns its keep. What round 2 put in its place was ALSO false, in the more
    dangerous direction: it said the altered character "can only ever make
    recognition MORE shell-accurate ... never suppress a receipt".

    IT COULD SUPPRESS, and did. A backslash-newline is a bash LINE CONTINUATION,
    so the substitution turned it into ``\\;``, `shlex` un-escaped that to a bare
    `;`, and `;` is in `_PUNCTUATION`. `mvn -B \\<newline> -DskipITs \\<newline>
    test` — one command to bash, argv `mvn -B -DskipITs test` — arrived here as
    three segments, none of them classifiable, and a 214-test run left NO
    receipt. `gradle`, `black --check`, `npm run test:e2e` and `cargo test` all
    failed the same way; `pytest` did not, because its identifying token sits in
    the FIRST segment and survives any split, which is why a pytest-shaped test
    corpus could not see the class at all. `_join_continuations` now removes
    continuations before the substitution runs, matching bash in and out of
    quotes; `tests/test_verification_receipts.py` pins the family.

    THEN THE FIX OVER-CORRECTED, IN THE ONE DIRECTION THAT MATTERS. The round-6
    version folded `\\r\\n` (and a lone `\\r`) to `\\n` BEFORE looking for
    continuations, so a CARRIAGE RETURN manufactured a continuation bash does not
    have. `mvn -B \\<CR><LF> test` is TWO commands to bash — the backslash escapes
    the CR into an ordinary argument character and the LF then terminates the
    command, giving `mvn` argc 2 (`-B`, `$'\\r'`) and a separate `test` — and it
    arrived here as the single argv `mvn -B test`, so the module wrote a `test`
    receipt for a maven run that never had a test goal. Round 6 cost a receipt;
    this cost the truth of one, which is the worse trade for a section whose
    whole claim is that the model did not author it. Both halves of the fold are
    gone: the raw bytes reach `_join_continuations`, and `\\r` is removed from
    `shlex.whitespace` so a CR no longer breaks a word bash keeps whole
    (`mvn -B te<CR>st` is ONE goal to bash, and splitting it invented `st`).

    SO STATE THE RISK IN THE DIRECTION IT ACTUALLY RUNS. The substitution is a
    source of BOTH over- and under-recognition, and either direction can be the
    costly one: a lost receipt understates the work, a wrongly joined one
    misstates it. What is checked, not assumed: with continuations removed and
    the CR fold gone, the only text that can still become a segment-splitting
    `;` is a LINE FEED `shlex` sees outside quotes — which bash USUALLY treats
    as a separator too, and a later review corrected this sentence for saying it
    always does. A LINE FEED after a trailing `&&`, `||` or `|` does NOT
    separate for bash: `pytest -q &&<LF>ruff check src/` is ONE and-list to bash
    (driven with stubs: both ran, rc 0) and two segments here. That particular
    over-split is harmless for recognition — each half classifies on its own —
    but it is the same mechanism behind a shape that is not harmless, stated
    below. A LINE FEED can also sit in a heredoc body, which splits a body into
    extra unclassified segments AFTER the command that owns them and so cannot
    displace it. A newline inside quotes becomes a literal `;` in the token and
    does not split. Every rule that inspects token CONTENT was re-checked
    against the same hazard, and again for the CR that now stays inside a word:
    `_SCRIPT_KIND`, `_PROGRAM_KIND`, `_SUBCOMMAND_KIND` and `_DURATION` are
    anchored matches over alternatives containing none of `;`, `\\n` or `\\r`, so
    a CR in the token can only make them MISS; `_skip_options` and `_has_flag`
    test only `-` and `=`; `os.path.basename` splits on `/`; and the
    env-assignment strip in `_strip_wrappers` matches `\\r` inside its `.*`
    value, which is what bash does with it too. That is a statement about the
    shapes enumerated here, not a proof over all of bash — the module is not
    bash, and a reviewer who finds a shape this list missed should expect it to
    be real. One is known and left alone, and it needs no CR to reach: a line
    feed that turns a valid line into a bash SYNTAX ERROR — `cd repo<LF>&& mvn
    -B test`, plain LF, bash rc 2, `syntax error near unexpected token '&&'` —
    makes bash run no part of the line AFTER the error, while this still finds
    `mvn -B test` after the separator and writes a `test` receipt. It does NOT
    make bash run nothing at all: the commands before the error DO run, because
    bash reads-parses-executes one complete command at a time (driven: the same
    shape with `touch M` in place of `cd repo` exits 2 and leaves `M` behind).
    It is the syntax-error member of
    a wider family this module cannot see, all of it CONTROL FLOW rather than
    lexing: `/usr/bin/false && pytest -q` (rc 1), `echo ok || pytest -q` (rc 0)
    and `exit 1<LF>mvn -B test` (rc 1) each get a `test` receipt for a check
    bash never reached — driven with a marker-printing stub on the check, marker
    absent in all four. Six more shapes were driven the same way and behave the
    same way, and the rendered limits entry names all ten: `exec true; pytest
    -q`, an `exit 0` inside a `source`d script, a multi-line `if false<LF>then
    <LF>pytest -q<LF>fi` (the ONE-LINE spelling `if false; then pytest -q; fi`
    leaves no receipt at all — the `;` splits it and no segment classifies),
    `case zz in x) pytest -q ;; esac`, `set -e<LF>false<LF>pytest -q` and
    `set -u<LF>echo $NOPE<LF>pytest -q`. Detecting any of it means parsing bash. What the human
    sees is the command line and whatever came back (for the syntax error, the
    shell's own message), plus an entry in `_VERIFICATION_LIMITS` naming the
    whole family — because for three of the four there is nothing in the output
    to give it away, and the source is not where a disclosure lives.

    WHERE IT IS APPROXIMATE, in both directions, stated because the rendered
    limits list has to be true of this code:

    * OVER-recognition. In POSIX mode `shlex` strips quotes, so `echo '|'` hands
      back a bare `|` indistinguishable from a pipe, and a heredoc body arrives
      as ordinary lines. A check NAMED in a string or a heredoc can therefore be
      recorded as though it ran. For the same reason `_join_continuations` joins
      a backslash-newline inside a `<<'EOF'` body, where bash would keep both
      characters — a difference in displayed text, not a lost receipt.
    * UNDER-recognition. A check reached indirectly — inside `bash -c '...'`, a
      `make` target's recipe, a shell script — is not seen at all and leaves no
      receipt.

    Both were verdict-bearing defects in the version this replaced, and are
    harmless here: an entry shows a command and the text that came back, and the
    human reads it. An unlexable line (an unbalanced quote) falls back to a
    whitespace split, because a best-effort argv is better than no receipt.
    """
    text = _join_continuations(command or "")
    lex = shlex.shlex(text.replace("\n", ";"), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    # A CARRIAGE RETURN IS AN ORDINARY CHARACTER TO BASH — not whitespace, not a
    # newline, not a separator. `shlex.whitespace` contains `\r` by default,
    # which would break a word bash keeps whole: `mvn -B te<CR>st` is one goal
    # to bash, and splitting it invented the word `st`. Dropping `\r` here is
    # the second half of the same rule the CR-folding above used to break.
    lex.whitespace = " \t\n"
    try:
        tokens = list(lex)
    except ValueError:
        tokens = text.split()
    segs: list[list[str]] = [[]]
    for tok in tokens:
        if tok and all(ch in _PUNCTUATION for ch in tok):
            segs.append([])
        else:
            segs[-1].append(tok)
    return [s for s in segs if s]


def _skip_options(argv: list[str], i: int, value_flags: frozenset[str]) -> int:
    """Index of the first non-option word at or after *i*.

    Options in *value_flags* consume the word after them unless they were given
    in `--flag=value` form. A bare `--` is consumed and ends option parsing.
    """
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return i + 1
        if not tok.startswith("-") or tok == "-":
            return i
        i += 1
        if "=" not in tok and tok.split("=", 1)[0] in value_flags:
            i += 1
    return i


def _has_flag(options: list[str], wanted: frozenset[str]) -> bool:
    """True when *options* contains one of *wanted*, including clustered short
    forms (`-vf` carries `-v`)."""
    shorts = {f[1] for f in wanted if len(f) == 2 and f[0] == "-"}
    for tok in options:
        if tok in wanted:
            return True
        if shorts and len(tok) > 1 and tok[0] == "-" and tok[1] != "-":
            if shorts & set(tok[1:]):
                return True
    return False


def _strip_wrappers(argv: list[str]) -> list[str]:
    """Drop leading env assignments and delegating wrappers (`uv run`, `npx`).

    A wrapper's OWN options are skipped as well. They were not, and the result
    was that `nice -n 10 pytest`, `env -i pytest`, `timeout 60 pytest`,
    `sudo -u ci pytest` and `xvfb-run -a playwright test` each resolved to a
    program name of `-n` / `-i` / `60` / `-u` / `-a` and produced no receipt.
    """
    out = list(argv)
    changed = True
    while changed and out:
        changed = False
        while out and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", out[0]):
            out.pop(0)
            changed = True
        if not out:
            break
        head = os.path.basename(out[0])
        if head not in _WRAPPERS:
            break
        flags = _WRAPPER_VALUE_FLAGS.get(head, frozenset())
        i = _skip_options(out, 1, flags)
        lookup = _WRAPPER_LOOKUP_FLAGS.get(head)
        if lookup and _has_flag(out[1:i], lookup):
            # `command -v pytest` RESOLVES a name; it does not run it. Returning
            # no argv means no receipt, which is the truth: nothing executed.
            return []
        if head == "timeout" and i < len(out) and _DURATION.fullmatch(out[i]):
            i += 1
        need = _WRAPPERS[head]
        if need:
            # The subcommand is REQUIRED. Dropping it unconditionally would make
            # `poetry add pytest` classify as a test run and `uv add ruff` as a
            # lint — a package install rendered as a check that ran.
            if i >= len(out) or out[i] not in need:
                break
            i = _skip_options(out, i + 1, flags)
        if i >= len(out):
            break
        del out[:i]
        changed = True
    return out


def _classify_segment(argv: list[str]) -> str | None:
    argv = _strip_wrappers(argv)
    if not argv:
        return None
    program = os.path.basename(argv[0])
    args = argv[1:]

    # `python -m pytest` / `python3 -m unittest`
    if re.fullmatch(r"python[0-9.]*", program) and "-m" in args:
        i = args.index("-m")
        if i + 1 < len(args):
            return _PROGRAM_KIND.get(args[i + 1].split(".")[0])

    if program in _SCRIPT_RUNNERS:
        script = None
        if args:
            script = args[1] if args[0] == "run" and len(args) > 1 else args[0]
        if script:
            for kind, pat in _SCRIPT_KIND:
                if pat.fullmatch(script):
                    return kind
        return None

    if program in ("make", "just", "task"):
        for target in args:
            if target.startswith("-"):
                continue
            for kind, pat in _SCRIPT_KIND:
                if pat.fullmatch(target):
                    return kind
            break
        return None

    if program in ("mvn", "gradle", "gradlew"):
        goals = {a for a in args if not a.startswith("-")}
        if goals & {"test", "verify"}:
            return "test"
        if goals & {"package", "compile", "build", "assemble"}:
            return "build"
        return None

    for a in args:
        if a.startswith("-"):
            continue
        if (program, a) in _SUBCOMMAND_KIND:
            return _SUBCOMMAND_KIND[(program, a)]
        break

    # `black --check` is a lint; plain `black` rewrites files and checks nothing.
    if program == "black":
        return "lint" if "--check" in args else None
    if program == "tsc" and "--build" in args:
        return "build"
    return _PROGRAM_KIND.get(program)


def classify(command: str) -> str | None:
    """The receipt kind for *command*, or ``None`` when it is not a check.

    Pure and deterministic: same string in, same label out, no model involved.
    Every segment of a pipeline, a `&&` list or a NEWLINE-separated list is
    considered, so `cd repo && pytest -q | tail -3` is a test and so is
    `cd repo\\nuv run pytest -q`.
    """
    if not command or not command.strip():
        return None
    for argv in _segments(command):
        kind = _classify_segment(argv)
        if kind:
            return kind
    return None


def kinds_in(command: str) -> set[str]:
    """EVERY kind *command* NAMES, not just the first one `classify` labels it by.

    NAMES, not runs. This reads the text of the line exactly as `classify` does
    and models no control flow, so `pytest -q || ruff check src/` reports
    ``{"test", "lint"}`` for a lint bash reaches only when pytest FAILS. The
    renderer's wording has to match: it says a recorded line "also NAMES a check
    recognised as lint", never that it runs one.

    One command line yields ONE receipt, labelled by the first recognised check.
    That is a rendering fact, not a fact about the line, and the two were
    conflated: `uv run pytest -q\\nuv run ruff check src/` produced a `test`
    receipt, and the section beneath it printed "no ... `lint` ... command was
    recorded" with `ruff check src/` visible in the entry directly above.

    Picking the LAST check instead would only move the contradiction to the
    first one. What has to stop is the CLAIM, so the renderer asks this what a
    recorded line actually names before saying a kind was never seen.
    """
    if not command or not command.strip():
        return set()
    return {k for argv in _segments(command) if (k := _classify_segment(argv))}


# -- bounds ----------------------------------------------------------------- #

COMMAND_MAX_CHARS = 400
EXCERPT_MAX_CHARS = 1200
_HEAD_SHARE = 0.6
_TRUNCATION_NOTE = "\n[... {dropped:,} of {total:,} characters omitted from the middle ...]\n"


def _bound(text: str, limit: int) -> tuple[str, bool]:
    """``(bounded_text, was_truncated)``, with an EXACT omission count.

    The count has to be exact: this is a document whose entire purpose is
    accuracy, and a first version stated "3,800 omitted" while dropping 3,857
    because it computed the number before deciding the slice. Here the slice is
    chosen first and the note is written from it.

    Sizing is single-pass and provably within budget: the note is first measured
    at its widest (`dropped = len(text)`), the head/tail are cut to fit around
    that worst case, and the real `dropped` - necessarily no larger - is then
    formatted into a note no longer than the one budgeted for.
    """
    if len(text) <= limit:
        return text, False
    widest = _TRUNCATION_NOTE.format(dropped=len(text), total=len(text))
    room = max(0, limit - len(widest))
    head = int(room * _HEAD_SHARE)
    tail = room - head
    dropped = len(text) - head - tail
    note = _TRUNCATION_NOTE.format(dropped=dropped, total=len(text))
    return text[:head] + note + (text[len(text) - tail:] if tail else ""), True


# -- redaction -------------------------------------------------------------- #

_REDACTED = "<redacted>"

#: Env var names whose VALUES are masked wherever they appear. Deliberately
#: broad: `GH_PAT`, `DATABASE_URL` and `AWS_ACCESS_KEY_ID` all carry credentials
#: and none of them contain the word "secret".
_SECRET_NAME = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE|API_?KEY|ACCESS_?KEY|"
    r"PAT|DATABASE_URL|_DSN|CONNECTION_STRING|SESSION|COOKIE|AUTH|"
    r"BEARER|OAUTH|SIGNING|WEBHOOK",
    re.I)

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(--?(?:password|passwd|pwd|token|api[-_]?key|secret|auth|bearer)[= ])\S+", re.I),
    # Attached short-flag form: `mysql -phunter2`, `psql -Wsecret`. Deliberately
    # greedy about what it masks - over-redacting a rare `-pvalue` flag costs a
    # reviewer nothing, whereas a leaked database password is unrecoverable.
    re.compile(r"(\s-[pPwW])(?=[^\s-])\S{3,}"),
    re.compile(r"((?:Authorization|X-Api-Key|Private-Token|Cookie)\s*:\s*)(?:Bearer\s+)?[^\"'\s]+", re.I),
    re.compile(r"(\s-u\s+)\S+:\S+"),
    re.compile(r"(://)[^/\s:@]+:[^/\s@]+(@)"),
    re.compile(r"\b(\w*(?:TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_KEY|APIKEY|PAT)\w*=)\S+", re.I),
    re.compile(r"\b(sk-ant-|sk-|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-|xoxb-|xoxp-|lin_api_|AKIA)[A-Za-z0-9_\-]{8,}"),
)


def _secret_literals(environ) -> list[str]:
    """Every literal worth masking: each secret-shaped env value, plus its
    base64 encodings.

    The base64 pass exists because a review found a live secret surviving
    redaction simply by being `base64`-encoded in the output - a shape no
    pattern anticipates and the plain-value pass cannot see.
    """
    out: list[str] = []
    for name, value in environ.items():
        if len(value) < 8 or not _SECRET_NAME.search(name):
            continue
        out.append(value)
        try:
            raw = value.encode()
            b64 = base64.b64encode(raw).decode()
            out.append(b64)
            out.append(b64.rstrip("="))
            out.append(base64.urlsafe_b64encode(raw).decode().rstrip("="))
        except Exception:  # noqa: BLE001 - encoding a str cannot realistically fail
            pass
    # Longest first: masking a prefix before its longer superstring would leave
    # the tail of the secret visible.
    return sorted({s for s in out if len(s) >= 8}, key=len, reverse=True)


def _redact(text: str, env: dict[str, str] | None = None) -> str:
    """Mask credentials in *text* - live env values (and their base64) first,
    then the shapes a credential takes on a command line."""
    if not text:
        return ""
    out = text
    for literal in _secret_literals(os.environ if env is None else env):
        out = out.replace(literal, _REDACTED)
    for pat in _PATTERNS:
        out = pat.sub(lambda m: "".join(g for g in m.groups() if g) + _REDACTED, out)
    return out


# -- markdown neutralisation: receipt text is UNTRUSTED --------------------- #

#: Characters that are removed before display. C0 controls and DEL corrupt the
#: rendering; the rest are INVISIBLE OR DIRECTION-CHANGING, and a review used
#: U+202E (right-to-left override) to make a code span display a command string
#: other than the one that ran. Removing them shows the real character sequence.
#: Homoglyphs (Cyrillic `е` in `pytеst`) are NOT addressed and are disclosed in
#: the rendered section instead - there is no safe automatic answer to those.
_INVISIBLE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f"    # C0 controls and DEL
    "\u00ad\u061c\u180e"                     # soft hyphen, ALM, MVS
    "\u200b-\u200f"                           # zero-width chars, LRM/RLM
    "\u202a-\u202e"                           # bidi embeddings and OVERRIDES
    "\u2060-\u2064\u2066-\u206f"            # word joiner, isolates, deprecated
    "\ufeff\ufffe"                            # BOM / ZWNBSP
    "]")


def _one_line(text: str) -> str:
    """*text* folded onto one line with invisible and direction-changing
    characters dropped — the display form of a command, shared by
    `md_inline_code` and `fold_by_kind` (a newline inside an HTML `<summary>`
    ends the HTML block just as it would end a list item)."""
    flat = (text or "")
    for sep in ("\r", "\n", "\u2028", "\u2029"):
        flat = flat.replace(sep, " ")
    return _INVISIBLE.sub("", flat).strip()


def md_inline_code(text: str) -> str:
    """Render *text* as a markdown code span that cannot escape its delimiters.

    Newlines are folded to spaces (a newline would end the list item and let the
    rest of the string be parsed as block markdown) and the delimiter is chosen
    longer than the longest backtick run inside, per CommonMark. Invisible and
    direction-changing characters are dropped so the span displays the sequence
    that actually ran; see `_INVISIBLE`.
    """
    flat = _one_line(text)
    if not flat:
        return "`` ``"
    longest = max((len(m) for m in re.findall(r"`+", flat)), default=0)
    fence = "`" * (longest + 1)
    # A span whose content starts or ends with a backtick needs padding spaces,
    # which CommonMark strips back out.
    pad = " " if flat.startswith("`") or flat.endswith("`") else ""
    return f"{fence}{pad}{flat}{pad}{fence}"


#: Reader-facing names for `KINDS` in the PR body's "How I verified this"
#: fold summaries.
KIND_LABELS: dict[str, str] = {
    "test": "Tests", "e2e": "End-to-end", "http": "HTTP checks",
    "typecheck": "Type check", "lint": "Lint", "build": "Build",
}


def fold_by_kind(rows: list[dict], *, anchors: dict[str, str] | None = None) -> str:
    """The PR body's verification digest (#23): one `<details>` per receipt
    kind, in `KINDS` order, unknown kinds last. The summary names the kind
    and the LAST command of that kind — the one that describes the tree that
    shipped — with the run count when there were several; opening it shows
    that command's captured output, fenced. Earlier runs of a kind (a failing
    mid-work pytest, say) never reach the body: they are in the full log
    only. Like the log, this asserts no pass or fail.

    The command is model-chosen text rendered inside an HTML `<summary>`, so
    it is HTML-escaped — `md_inline_code` neutralises markdown, not a
    `</summary>`. The excerpt goes through `md_fence` for the same reason.

    *anchors* (kind → URL, from `core/evidence_ledger.py`) adds a
    `full log` link to a kind's summary that opens the ledger's copy of the
    log on that command's line. Absent or missing a kind: no link.
    """
    by_kind: dict[str, list[dict]] = {}
    for r in rows:
        by_kind.setdefault(str(r.get("kind")), []).append(r)
    order = [k for k in KINDS if k in by_kind] + sorted(
        k for k in by_kind if k not in KINDS)
    folds: list[str] = []
    for kind in order:
        group = by_kind[kind]
        last = group[-1]
        runs = f" ({len(group)} runs, last shown)" if len(group) > 1 else ""
        cmd = html.escape(_one_line(str(last.get("command") or "")), quote=False)
        excerpt = str(last.get("output_excerpt") or "").strip()
        if excerpt:
            inner = md_fence(excerpt)
            if last.get("truncated"):
                inner += (f"\n_excerpt — {last.get('output_bytes', 0):,} "
                          f"characters of output in total_")
        else:
            inner = "_nothing was captured on stdout or stderr for this command._"
        href = (anchors or {}).get(kind)
        link = f' · <a href="{html.escape(href, quote=True)}">full log</a>' if href else ""
        folds.append(
            f"<details><summary><b>{KIND_LABELS.get(kind, 'Other')}</b>{runs} — "
            f"<code>{cmd}</code>{link}</summary>\n\n{inner}\n\n</details>")
    return "\n".join(folds)


def md_fence(text: str, *, info: str = "") -> str:
    """Render *text* as a fenced block no content of its own can break out of.

    The opening fence is made longer than the longest fence run at the start of
    any line, which is the only construct that can close a fence early. This is
    what stops a coder's `echo '### Manual UI verification'` from becoming a
    heading inside a section that exists to be un-authored.
    """
    body = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    body = _INVISIBLE.sub("", body.replace("\u2028", "\n").replace("\u2029", "\n"))
    runs = [len(m.group(1)) for m in re.finditer(r"(?m)^\s{0,3}(`{3,}|~{3,})", body)]
    fence = "`" * max(3, (max(runs) + 1) if runs else 3)
    return f"{fence}{info}\n{body}\n{fence}"


# -- the receipt ------------------------------------------------------------ #


@dataclass(frozen=True)
class VerificationReceipt:
    """One thing the session ran, and the text that came back.

    There is deliberately no verdict, no exit status and no per-command note:
    see the module docstring. ``output_excerpt`` is already redacted and already
    bounded; ``output_bytes`` is the size of the ORIGINAL text, so truncation
    can be stated in real numbers.
    """

    kind: str
    command: str
    output_excerpt: str
    output_bytes: int
    truncated: bool
    seq: int = 0


#: The measured NEVER-RAN prose (21 of 92 real failures). These yield no receipt.
_BLOCKED = re.compile(r"\s*Error:\s*(?:Blocked\b|Permission\b|[^\n]*\bnot allowed\b)",
                      re.I)
#: An exit status the harness STATED. Whatever else the prose says, a command
#: with a status of its own RAN — see `_was_executed`.
_STATED_EXIT = re.compile(r"\s*Error:\s*Exit code\s+\d+", re.I)


def _was_executed(tool_response: Any) -> bool:
    """False when there is nothing to put in front of a reviewer.

    Exactly two shapes qualify, both measured, and both are disclosed in the
    rendered section because a command with no receipt is indistinguishable from
    a command that was never run:

    * ``backgroundTaskId`` — the HARNESS backgrounded the command and it has not
      finished, so it hands back a task id instead of output.

      THIS IS THE HARNESS'S BACKGROUNDING AND NOTHING ELSE. A trailing `&` the
      coder wrote is not detected here and is not meant to be: `&` is in
      `_PUNCTUATION`, so it merely ends a segment, and `pytest -q &` classifies
      `test` and gets a receipt like any other line. Measured in the harness
      this runs under: `( sleep 6; touch M ) &` returned rc 0 in 11ms with `M`
      absent and no `backgroundTaskId` in the payload, and a background job that
      echoed 5s later had its output land nowhere. So the receipt is real — the
      line WAS submitted — but the check it names may still have been running.
      Suppressing it was considered and rejected: deciding whether a `&` is a
      trailing background operator, the left half of `&&`, a `2>&1`, or a
      character inside a quoted word means parsing bash, which is the trap this
      module spent six rounds climbing out of, and dropping the receipt would
      trade a scoped disclosure for a silent absence. The rendered limits entry
      says all of this in words instead.
    * ``Error: Blocked`` / ``Error: Permission ... denied`` — the harness
      refused, so the command never executed.

    Everything else is recorded, including a shape this module does not
    recognise: silence about a command that ran is the failure mode here, and an
    entry showing an unfamiliar response is strictly more informative than no
    entry at all.

    TWO NARROW HOLES AN INDEPENDENT REVIEW FOUND, both closed here because each
    made a sentence in the rendered limits list FALSE, and that list is the one
    thing on this branch that may not drift again:

    * The backgrounded test used to sit INSIDE the stdout/stderr branch, so a
      payload carrying `backgroundTaskId` and nothing else produced a receipt
      for a command that had not finished. Measured, all 100 real backgrounded
      payloads carry `stdout`/`stderr`, so it was unreachable — but "only a
      command the HARNESS backgrounded leaves no receipt at all" is
      printed to the human unconditionally, so it is now true unconditionally.
    * `_BLOCKED`'s `not allowed` alternative matched
      ``Error: Exit code 2: this option is not allowed here`` — a command that
      RAN and failed — and dropped it under a rule whose stated reason is
      "because it never ran". `_STATED_EXIT` now wins: the harness does not hand
      back a status for something it refused to start.

      SCOPE THAT PRECEDENCE HONESTLY — a second review flagged the gap between
      the rule and its regex. `_STATED_EXIT` matches ONE spelling,
      ``Error: Exit code <N>``, because that is the only spelling measured (71
      of 92 real failures; module docstring above). Other wordings of a stated
      status — ``Error: Command failed with exit code 1: --strict is not
      allowed`` — are still swallowed by the `not allowed` alternative and leave
      no receipt for a command that ran. Those wordings are speculative, not
      observed, so the regex is not widened on a guess; what is fixed here is
      the claim, which now says one spelling rather than "a stated status".
      Under-capture is disclosed by the header ("not necessarily everything the
      session ran"); widening `_STATED_EXIT` on invented prose would risk the
      other direction, where a refused command gets a receipt implying a check
      happened.
    """
    if isinstance(tool_response, dict):
        return not tool_response.get("backgroundTaskId")
    if isinstance(tool_response, str):
        if _STATED_EXIT.match(tool_response):
            return True
        if _BLOCKED.match(tool_response):
            return False
    return True


def _output_text(tool_response: Any) -> str:
    """The harness's text for one command, verbatim as far as it goes.

    A STRING response is kept WHOLE, `Error: Exit code 1` prefix included. The
    prefix used to be stripped, which threw away the only place the failure was
    written down; here it is simply part of what came back.

    Where a DICT response carries a report instead of output — a timeout, an
    interruption, the harness's own wording of a non-zero exit — that report is
    appended in square brackets rather than dropped. It is text like the rest of
    the excerpt, not a status this module vouches for, and the rendered limits
    say so.
    """
    if isinstance(tool_response, dict):
        text = f"{tool_response.get('stdout') or ''}{tool_response.get('stderr') or ''}"
        notes: list[str] = []
        ms = tool_response.get("timedOutAfterMs")
        if ms:
            notes.append(f"[the harness killed this command at the {ms}ms timeout]")
        if tool_response.get("interrupted"):
            notes.append("[the harness reported this command as interrupted]")
        interp = tool_response.get("returnCodeInterpretation")
        if interp:
            notes.append(f"[the harness reported: {str(interp)!r}]")
        if notes:
            text = (text + "\n" if text and not text.endswith("\n") else text)
            text += "\n".join(notes)
        return text
    if isinstance(tool_response, str):
        return tool_response
    return ""


def build_receipt(
    tool_name: str,
    tool_input: dict | None,
    tool_response: Any,
    *,
    env: dict[str, str] | None = None,
) -> VerificationReceipt | None:
    """A receipt for one tool call, or ``None`` when it is not evidence."""
    if tool_name != "Bash":
        return None
    command = str((tool_input or {}).get("command") or "").strip()
    kind = classify(command)
    if kind is None:
        return None
    if not _was_executed(tool_response):
        return None
    raw_output = _output_text(tool_response)
    excerpt, truncated = _bound(_redact(raw_output, env), EXCERPT_MAX_CHARS)
    cmd, _ = _bound(_redact(command, env), COMMAND_MAX_CHARS)
    return VerificationReceipt(
        kind=kind, command=cmd,
        output_excerpt=excerpt, output_bytes=len(raw_output), truncated=truncated,
    )


#: Most receipts recorded for one attempt. PAST THIS THE OBSERVER GOES SILENT,
#: which is why the renderer has to say so. Import this rather than repeating
#: the number.
RECEIPT_CAP = 200


class VerificationReceiptHook:
    """PostToolUse observer that persists a receipt per verification command.

    **Always returns ``{}``.** It is an observer, never a controller: the
    orchestrator composes hooks with `if result: return result`, so a hook that
    returned anything would suppress the lint and scope-guard hooks behind it.
    It is also placed FIRST in that composite for the mirror-image reason - last
    in line it would stop running as soon as an earlier hook fired, and receipts
    would go missing exactly on the attempts that had the most to report. See
    `Orchestrator._ordered_post_tool_hooks`, which pins the order.
    """

    def __init__(
        self,
        *,
        attempt_id: str,
        persist: Callable[[str, VerificationReceipt], Any],
        on_event: Callable[..., None] | None = None,
        max_receipts: int = RECEIPT_CAP,
    ):
        self.attempt_id = attempt_id
        self._persist = persist
        self._on_event = on_event or (lambda *a, **k: None)
        self.max_receipts = max_receipts
        self._seq = 0
        #: Recognised verification commands seen AFTER the cap. Nothing renders
        #: it (the renderer sees only the stored rows), but it is emitted as an
        #: event so the drop is at least visible in the run log.
        self.dropped = 0

    async def hook(self, input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        try:
            data = input_data or {}
            # `agent_id` is present only inside a Task-spawned subagent; receipts
            # describe what the CODER did. (`tool_result_cap.py` once shipped a
            # dead guard reading `parent_tool_use_id`, which is never sent.)
            if data.get("agent_id"):
                return {}
            receipt = build_receipt(
                data.get("tool_name") or "",
                data.get("tool_input") or {},
                data.get("tool_response"),
            )
            if receipt is None:
                return {}
            if self._seq >= self.max_receipts:
                # Count it before dropping it, and say so once. A cap that drops
                # silently reads downstream as "that is everything that ran".
                self.dropped += 1
                if self.dropped == 1:
                    self._on_event(
                        "verification_receipt_capped",
                        f"receipt cap of {self.max_receipts} reached; further "
                        f"verification commands are not being recorded")
                return {}
            self._seq += 1
            stored = VerificationReceipt(
                kind=receipt.kind, command=receipt.command,
                output_excerpt=receipt.output_excerpt,
                output_bytes=receipt.output_bytes, truncated=receipt.truncated,
                seq=self._seq,
            )
            await self._persist(self.attempt_id, stored)
            self._on_event(
                "verification_receipt", f"{stored.kind}: {stored.command[:80]}")
        except Exception:  # noqa: BLE001
            # Evidence capture must never break the coder session. A lost
            # receipt understates the work; a raised hook ends the attempt.
            return {}
        return {}
