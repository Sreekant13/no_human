# no_human — design system

This documents the design system the board **already ships**, so a change can be
judged against what exists rather than re-invented. It is descriptive, not
aspirational: every token and rule here is drawn from `web/src/styles.css` and
the brand commitments in `PRODUCT.md`. Fix real weaknesses; **do not reskin for
its own sake.**

## Identity

A **dark-first operations board** — the surface an engineer watches tasks move
across. Voice is plain, engineer-to-engineer, evidence-first (`PRODUCT.md`
"Voice"). Warm-editorial in tone, not a neon dashboard.

## Colour (source of truth: `web/src/styles.css` `:root`)

Dark-first, defined as CSS variables. Tailwind runs with **preflight off**, so
every variable must be defined in **both** themes and `border` paints nothing
without an explicit `border-*` — never rely on a reset.

- **Canvas / surfaces** (deep navy-black, raised in steps):
  `--base` `#0F1117` → `--surface-1` `#1A1D27` (cards) → `--surface-2` `#22252F`
  (columns/pills) → `--surface-3` `#292D3A` (hover). Borders `--border` `#2E3241`,
  `--border-hi` `#3D4255`.
- **Text ramp** (light on dark, contrast **≥ AA vs every surface** including
  `--surface-3` — this is a hard rule, not a preference): `--text-hi` `#E8ECF2`
  (headings) · `--text` `#C9CDD6` (body) · `--text-muted` `#A8AFC5` (meta) ·
  `--text-dim` `#8C96B2` (faint meta). The muted/dim values were raised
  specifically to pass AA on hover — do not darken them back.
- **Accent** — a single blue: `--accent-500` `#4C9AFF` (primary),
  `--accent-600` `#2684FF` (hover), `--accent-700` `#0065FF` (pressed),
  `--accent-300` `#6BADFF` (AA on the `--accent-100` `#1C3A5E` fill).
- **Role colours** — each agent role has one hue, used consistently wherever a
  role is shown: worker `#4C9AFF`, planner `#45C8DC`, supervisor `#E8A04F`,
  reviewer `#9F8FEF`, agent `#4ADE80`, watcher `#8C92A4`, investigator `#57C98A`.

New colour goes in as a token in both themes; a one-off literal in a component
is a defect the design review rejects.

## Type

Two bundled faces (`web/src/assets/fonts/`), no web fetch: **DM Sans** for UI
text and **IBM Plex Mono** for code, IDs, durations and any monospace field.
These are incumbent and fixed.

## Layout & protected invariants

The following are load-bearing product behaviours, not styling choices, and
design work **must not break** them (`PRODUCT.md`, operator walk 2026-07-11):

- the **lane model** (`web/src/boardLanes.js`) and the single **`isNeedsYou`**
  predicate — one definition of "this needs a human", never a second;
- the shared **burn/cost definition** (cost comes from `web/src/cost.js` only);
- the inline **agent log panel**;
- **cancelled ≠ failed** — a cancelled task is not a failure lane.

## Brand

- Name **no_human**, lowercase with underscore, always; wordmark
  `web/src/Logo.jsx`.
- **Slogan — operator-pinned, BINDING, do not alter, shorten, reorder or
  replace the words** (design may re-set it typographically only):
  "From ticket to reviewed pull request." then "Free and open-source, on your
  machine."
- **Never surface a local or employer repository name** in any UI or demo.

## Scope

This file records the board's system. Whether the hosted tier's UI shares it is
an open product decision (`PRODUCT.md`), not settled here.
