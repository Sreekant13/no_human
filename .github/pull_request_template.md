## What this changes

<!-- One or two sentences. Link the issue: Fixes #123 -->

## Evidence

<!--
Paste the command you ran and its output. Not a description of the output.
Example:

    $ uv run pytest -q -n 4 tests/test_scheduler.py
    38 passed in 7.48s
-->

```
```

## Checklist

- [ ] Tests cover the change, and they fail on the unfixed code.
- [ ] Test count and assertion count did not go down, or the PR body explains why.
- [ ] Lockfiles committed if a dependency changed.
- [ ] Nothing in the diff reads or writes a credential inside the repo.
- [ ] This does not conflict with the constraints in `CLAUDE.md`.
- [ ] First PR only: I have read `CLA.md` and added my `contributors/<handle>.md`
      entry to this PR. (Ticking this box is not the record — the file is. CI
      checks for the file.)

<!--
`main` is protected. The maintainer reviews and merges by hand. There is no
auto-merge on this repository.
-->
