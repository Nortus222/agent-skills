# Resolving and gathering the target

Classify what you were pointed at, then follow that recipe. When a target is genuinely
two kinds at once — "explain PR #634" is both a change and a subsystem — gather for the
change and read outward into the subsystem.

---

## Code — a file, class, module, or subsystem

**Signals:** a path, a type name, a folder, a named area ("the grid loading pipeline").

1. **Locate.** For a path, read it. For a name, find the definition before assuming which
   one is meant — a name that resolves to three types is an ambiguity to raise, not to
   pick from.
2. **Read the whole thing.** Not an outline, not a grep. The target itself gets read in
   full.
3. **Read outward.** In order of payoff:
   - the interfaces or base types it implements — that is its contract
   - its callers — grep the type name; the call sites tell you what it is *for*
   - what it calls or returns — where its data goes next
   - its tests — test names state intended behaviour more plainly than any comment
4. **Find the seams.** Where does this hand off to something else? Those boundaries are
   usually what the reader most needs drawn.

**Why section for code:** the trail is design docs under `docs/`, the commit that
introduced the file (`git log --diff-filter=A -- <path>`), and explanatory comments.
Often there is no stated rationale. Say so.

---

## Concept — "how does X work"

**Signals:** a question about behaviour or mechanism with no path attached — "how does
MFA enrollment work", "explain how caching works here".

1. **Find the entry point.** Grep for the domain vocabulary, not for guessed type names.
   The user's own words usually appear in the code.
2. **Trace one concrete path end to end.** Pick a single realistic scenario and follow it
   from trigger to result. Breadth-first reading of every related file produces a
   catalogue; one traced path produces an explanation.
3. **Then widen** to the variants that matter — the error path, the second entry point —
   only if the reader would be misled without them.
4. **Name the pieces in the codebase's own vocabulary.** If the code calls it a
   `SessionTicket`, do not invent "auth token" for the document.

**Why section for concepts:** usually architectural — an ADR, a design doc, a spec.
Search `docs/` before concluding there is none.

---

## Change — commit range, branch diff, uncommitted work, PR

**Signals:** `HEAD~5..HEAD`, `main...HEAD`, "this change", "these commits", "my working
tree", `#634`, a PR URL.

1. **Get the diff and the stat.**
   - Range or branch: `git diff <base>...HEAD` and `git diff --stat <base>...HEAD`
   - Uncommitted: `git diff HEAD` plus untracked files
   - PR: `gh pr view <n> --json title,body,author,baseRefName,url,files` then
     `gh pr diff <n>`. Note: `--json files` caps at 100 entries and `gh pr diff` fails
     past ~300 files — fall back to a local `git diff` against the merge base.
2. **Triage the file list before reading.** Generated files, lockfiles, `.resx`, bulk
   renames, and vendored code are noise that will swamp the document. Decide per file:
   read it, skim it, or ignore it — and say in the Scope tag what you ignored and why.
   Never describe a file you put in the ignore bucket.
3. **Read the unchanged code the diff touches.** This is the step that separates a real
   explanation from a narrated patch. The interface a new class implements, the caller
   whose behaviour now shifts, the type whose shape changed downstream — none of that is
   in the diff.
4. **Read at the right commit.** For a PR, the tree you read must be the PR's head
   commit, not whatever your working directory happens to be on.

**Why section for changes:** the richest trail of any target type — PR body, linked
issues, commit messages, and any ExecPlan or spec referenced from them. Follow the links;
echo only what you actually opened.

---

## Sizing the read

An explanation of code you skimmed is worse than no explanation, because it reads
confident. If the target is too large to read properly:

- Narrow it with the user — offer the two or three sub-areas you would explain well —
- or explain one coherent slice in full and say plainly in the Scope tag what the
  document does not cover.

Do not silently downgrade to skimming everything.
