# Repository instructions

## Git workflow

Feature PRs target `main`. Keep the main checkout on its integration branch and
make changes in a dedicated feature worktree. Default new worktrees to
`.claude/worktrees/<slug>`; continue in an existing task worktree when provided.

Before the first commit, review `git diff HEAD` and untracked files. After committing,
review `git diff origin/main...HEAD`. Commit and push task changes, then open the
PR against `main`. Leave merging to the owner.

## Skills

Keep each skill in `skills/<name>/SKILL.md` with `name` and `description` frontmatter.
Add its entry to the README's skills table. Keep reusable instructions independent
of machine-specific paths, hostnames, and account details. Add supporting scripts
or references only when the workflow needs them.

## Verification

This repository has no solution build. For skill documentation changes, validate
the skill with the installed skill-creator validator, review links and examples,
and run `git diff --check`. Exercise documented commands when practical and report
which behavior remains unverified.

For changes to the mobile release scripts, run:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

## Attribution

End each PR body with `Model: <model> · Harness: <harness>`. Open published comments
with a GitHub note naming the model and harness, on behalf of Ihor. Keep model
co-author trailers out of commits.
