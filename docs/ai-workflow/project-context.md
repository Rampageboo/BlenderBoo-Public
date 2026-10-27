# Modular Project Context

Read this module when creating, loading, or updating `project-context/`.

Project Context is compact cross-session memory. An entry earns its place only
if it passes both tests: it would still be true after the code was rewritten,
and it cannot be recovered by reading source. In practice that leaves intent
and non-goals, decisions and the reasons behind them, boundaries that must not
be crossed, and failure modes with their consequence.

Everything else belongs to the codebase map, to the task record, or nowhere.
Repository structure and as-built architecture belong to an adopted map; when
no map exists, inspect source on demand. Write nothing to fill a heading — an
empty module is honest, and a vague one costs a reader time while teaching
nothing.

Entries leave. Remove a risk once a regression test enforces it, compress a
decision once it is settled, and drop state a later stage superseded. Context
that only grows is accumulating task record; this memory should stay roughly
constant in size.

```text
project-context/
|-- index.yaml
|-- exact-facts.yaml
`-- sources/
```

Read `index.yaml` first, then only modules whose `read_when` matches the task.
During route preflight, inspect only the lightweight adoption/routing metadata
in `exact-facts.yaml` when needed to discover a declared map, research path, or
specialized channel; do not load source modules merely to inventory them. Read
exact fact values only when they matter. Never store secrets or personal data;
reference their approved storage location.

## When to adopt it

Create `project-context/` at the first fact that passes both tests, not at
project start. A project with nothing durable to record yet is better served by
no directory than by a template of empty headings, because empty headings read
as questions that were asked and answered.

Work that fits one session does not need it: the goal, the constraints, and the
reasoning are still in the conversation. What earns the directory is a decision
whose reason would otherwise be lost, or a boundary a later session would
otherwise cross without knowing it existed.

Whoever holds that fact writes it down. The code owner may propose it; the
Owner or assigned context integrator applies it.

Use `PROJECT_CONTEXT_TEMPLATE/` to initialize the layout. Mark unknown content
as missing rather than inventing it. Store only routing and validity metadata
for maps or research; their generated artifacts stay with their providers.

## What rots, and checking it

Intent, confirmed decisions, boundaries, and the mechanism behind a risk hold
until the decision itself changes. Anything describing current stage, adopted
tool state, or exact values is true only against the revision it was checked
at, and that is the part that goes stale.

So `exact-facts.yaml` and any state module record the revision they were
verified against. A handwritten `status: current` is descriptive only and is
never freshness evidence. Before relying on a revision-bound fact, run:

```text
python tools/validate_project_context.py project-context/exact-facts.yaml --repo .
```

The validator compares every `source_revision_represented` in the file with the
applicable Git revision, not only the record being edited. A mismatch is stale
and fails closed: refresh the record or treat it as unverified. Operational
failure to obtain the revision also fails the automatic check. For facts that
cannot be revision-compared, record a non-manual refresh trigger, an explicit
validity condition, or a finite `valid_until`; important facts remain
unverified until that condition is checked, while advisory facts may continue
only with the warning stated.

A codebase map keeps its revision under its own `represented_revision` key and
is deliberately outside that comparison, because an `on-query` rebuild makes the
map current when it is read: a value that lags between sessions is expected
rather than stale. The record is still covered where it matters — a map claiming
`status: current` with a manual `update_trigger` has no checkable revision and
fails the same check.

After editing an exact-facts file, validate the whole file and review adjacent
facts affected by the same environment or revision change. A stale record that
reads as current is worse than none, because it sends the next session to plan
work that already exists.

Current Owner direction, the active task, source, and verified tests outrank
stored context. Surface conflicts and mark stale material instead of silently
choosing it. Existing V2 layouts use
`migrations/project-context-v2-to-v3.md`.
