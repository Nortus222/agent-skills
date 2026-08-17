# agent-skills

Locally authored agent skills, installable with the same CLI that pulls the
third-party ones. Runtime-agnostic: they install into `~/.agents/skills/`, which
Claude Code, Codex, Gemini CLI, and Copilot CLI all read.

## Install

```bash
npx skills add Nortus222/agent-skills
```

Pick individual skills interactively, or take the lot. To update later:

```bash
npx skills update
```

## Skills

| Skill | Use when |
| --- | --- |
| [deploy-mobile-apps](skills/deploy-mobile-apps/) | You need to prepare or release the managed Pocket Manage mobile apps with a saved approval boundary. |
| [explain](skills/explain/) | You want something explained so you genuinely understand it — a file, class, module, subsystem, concept, commit range, branch diff, uncommitted work, or PR. Writes a self-contained HTML document with rendered Mermaid diagrams. |

## How this fits the rest of the setup

Skills on a machine come from three places, and they coexist deliberately:

- **This repo** — the shared union, installed on every machine.
- **Third-party repos** — `mattpocock/skills`, `vercel-labs/skills`, and so on.
- **Hand-authored local skills** — created directly in `~/.agents/skills/` and never
  installed. The installer leaves them alone, so a machine can carry extras that
  nobody else needs.

`~/.claude/skills/<name>` are symlinks into `~/.agents/skills/<name>`.

The companion [claude-config](https://github.com/Nortus222/claude-config) repo covers
settings and `CLAUDE.md`; its `skills-manifest.txt` lists which skills are expected
on every machine, and `skills-check.sh` reports any that have gone missing.

## Adding a skill here

One directory per skill under `skills/`, containing a `SKILL.md` with `name` and
`description` frontmatter. Keep broadly reusable skills repo-agnostic. Personal
multi-repository operator skills may keep their managed inventory in a bundled
reference file instead of duplicating it in `SKILL.md`.
