---
name: explain
description: 'Use when the user wants something explained so they genuinely understand it — a file, a class, a module or subsystem, a concept, a commit range, a branch diff, uncommitted work, or a PR. Triggers: "explain X", "how does X work", "walk me through X", "what does this change do", "help me understand X", "onboard me to X".'
argument-hint: "What should I explain? (path, subsystem, concept, commit range, branch, or PR)"
---

# Explain

Produce one rich, self-contained HTML document that makes a reader genuinely understand
something. Not a summary, not a review — an explanation, written to be read once, from
the top, by a skilled engineer who does not know this area.

**Core principle: comprehension, not judgement.** The reader should finish able to
reason about the thing. Whether it is any *good* is a different job.

## The three invariants

These are the whole discipline. Everything else is craft.

**1. Never comment on correctness.** No bugs, no risks, no "this should be", no
suggested fixes, no severity labels, no "note that this could fail if". If you notice a
problem, stay silent here and mention it in chat *after* handing over the document. An
explanation that editorialises stops being trusted as an explanation.

**2. Never invent a rationale.** If no plan, spec, issue, commit message, or comment
stated why, write "no stated rationale was found" in the Why section. A plausible
reconstruction is indistinguishable from a real one to the reader, and it is wrong often
enough to poison the whole document.

**3. Never describe what you did not read.** Do not infer a file's contents from its
name, its imports, or a diff hunk that touches it. If something matters but you could not
read it, say so in the Scope tag rather than covering for the gap.

## Workflow

### 1. Resolve the target

Classify what you were pointed at, then gather. See [gathering.md](gathering.md) for the
recipe per target type — code (file/class/module/subsystem), concept, or change (commit
range, branch diff, uncommitted work, PR).

If the target is ambiguous — "explain the login stuff" — ask one clarifying question
naming the two or three candidates you found. Do not explain the wrong thing thoroughly.

### 2. Read outward, not just at the target

The single most common failure is explaining the target in isolation. Read what it
touches: the interface it implements, the caller whose behaviour depends on it, the type
its data becomes next. For a change, read the *unchanged* code the diff interacts with —
an explanation built only from the patch describes code you never saw.

Stop when you can answer, without looking: what calls this, what this calls, and what
breaks if it vanishes.

### 3. Find the intent trail

Look for evidence of *why*, in descending order of trust: a linked issue or spec, the
commit messages, an ExecPlan or design doc in `docs/`, a comment that explains a choice,
a test name that states an expectation. Record what you actually used as sources.

Found nothing? That is a finding. Invariant 2.

### 4. Write the document

Copy `template.html` to the output path and fill it in. The `<head>`, the `<script>`
blocks, and the CSS are copied **verbatim** — the styling is deliberately identical
across every explanation so they read as one publication. Delete any section slot you
have nothing real to put in, and regenerate the table-of-contents list to match.

**Default output:** `docs/explanations/<YYYY-MM-DD>-<slug>.html` from the repo root,
creating the directory if needed. Outside a repo, write to the current directory. If the
user named a path, use theirs.

### 5. Hand over

Report the path, one line on what it covers, and — separately, in chat — anything you
deliberately kept out of the document under invariant 1.

## The sections

**At a glance.** One paragraph naming what this achieves, in plain language. Someone who
reads only this should be able to repeat the gist accurately. No file paths.

**Background — two levels.** `narrow` is short and change-specific, for a reader already
in this area; it sits in the open. `deep` explains the surrounding system to someone who
has never worked here, and lives in a collapsed, explicitly skippable block — so write it
for a genuine newcomer, at genuine length, without worrying about the expert's patience.
The collapsing is what buys you the freedom to be thorough.

**Why.** The intent, drawn from the trail. Sources list echoes only what you actually
used. Invariant 2 governs this section absolutely.

**Intuition.** The essence, carried by concrete toy data — a made-up row, a sample value,
a two-element list you follow through the system. No file paths, no line numbers, no API
names where a plain noun works. This is the section a reader will remember; if it reads
like the walkthrough, you have written the walkthrough twice.

Up to four Mermaid diagrams. Reuse a small number of diagram families rather than
inventing a visual language per diagram. A data-flow diagram carrying example values is
worth more than a class diagram. Every diagram must be valid Mermaid — verify before
shipping (see Verify below).

> **Mermaid-in-HTML trap.** The browser HTML-decodes the diagram source *before* Mermaid
> parses it. A `&quot;` inside a `["quoted label"]` therefore becomes a real `"`, nests
> quotes, and breaks the parse at render time — after every validator has passed, because
> the `.mmd` file you validated never had the entity in it. Use single quotes inside
> labels. Same hazard for `&amp;`, `&lt;`, `&gt;`.

**Walkthrough.** Ordered to be *understood*, not to match the file listing or the diff
order. The best first step is usually the smallest self-contained one, not the largest or
the alphabetically first. Each step centres on one file and carries only the essential
lines — never a whole file, never a hunk pasted for completeness. Each step says what it
does and what that buys.

## Style

Write in classic style: engaging prose with smooth transitions, explaining rather than
listing. Prefer a concrete example to an abstract statement. Assume a skilled engineer
who does not know this subsystem — never condescend, never assume familiarity.

Reach for a bulleted list only when the content is genuinely a set of parallel items. An
explanation that has become a list of bullets has stopped explaining and started
inventorying.

## Verify before handing over

Do not claim the document is ready until you have:

- Rendered every Mermaid block through a validator — extract each block to a `.mmd` and
  run `npx -y @mermaid-js/mermaid-cli@11 -i d.mmd -o d.svg`. Unescape HTML entities during
  extraction so you validate what the browser will actually feed Mermaid, or the trap
  above sails straight through. A broken diagram shows the reader a raw error string
  where the explanation should be.
- Confirmed every code snippet appears verbatim in the file it claims. Script this rather
  than eyeballing it: pull each `<pre><code>` and its step's file path out of the
  document, unescape, and check every non-comment line against the source.
- Confirmed every file path resolves from the repo root at the commit you explained.
  Write paths in full even when two appear in one step — an abbreviated second path
  (`Foo.cs` after `some/dir/Bar.cs`) is not resolvable by the reader or by your own check.
- Spot-checked any `file:line` reference you cite in prose.
- Re-read the Why section against invariant 2, and the whole document against
  invariant 1.

## Common mistakes

| Mistake | Fix |
|---|---|
| Walkthrough follows the file list or diff order | Reorder for comprehension; smallest self-contained piece first |
| Intuition section restates the walkthrough abstractly | Rewrite it around toy data; if it has file paths, it's the wrong section |
| "This is a solid approach", "this could be risky" | Cut it. Invariant 1 |
| Rationale reconstructed from the code | Cut it. Say no rationale was found. Invariant 2 |
| A file described from its name or its diff hunk alone | Read it, or drop it and note the gap |
| Snippet is a whole method pasted for completeness | Cut to the lines that carry the point |
| Deep background written short to spare the expert | It's collapsed. Write it for the newcomer, at length |
| Diagrams in four different visual idioms | Pick one or two families and reuse them |
