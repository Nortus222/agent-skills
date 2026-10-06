---
name: emws-api
description: "Use when you need to call, test, or verify the e-manage|ONE API (EMWS) on staging or production: checking an endpoint's response, reproducing an API error, or confirming a deployed change."
---

# EMWS API

`emws` calls the e-manage|ONE API through APIM with credentials already configured on this
machine. Never ask the user for keys, passwords, or tokens, and never read
`~/.config/emws/.env` or `~/.config/emws/tokens/`.

Requires Node 24 or newer. Run it as:

```bash
node ~/.agents/skills/emws-api/scripts/emws.ts <command> ...
```

Use `emws` instead when that alias exists. The examples below use `emws`.

## Start here

```bash
emws profiles            # profiles, their environment, write mode, and whether the secret is set
emws whoami -p <name>    # proves the credentials work and shows the tenant
```

If no profile works, stop and tell the user that setup is needed: `~/.config/emws/profiles.json`
and `~/.config/emws/.env` (mode 600), with the layout in `references/profiles.example.json`.
Do not create or edit those files yourself.

## Calling the API

```bash
emws routes receiving                          # search the route list
emws describe GET /projects/{id}               # parameters and schemas for one route
emws get /projects/123 -p staging-admin
emws get /projects --query start=0 --query limit=20
emws post /things --body @payload.json -p staging-admin
```

- Paths are relative to the environment. Never add a host or an `/api` prefix.
- Default to a staging profile. Use production only to read, and only when the task needs it.
- stdout is the response body; one summary line goes to stderr. `--json` prints one envelope
  with status, headers, body, and call id instead.

## When a call fails

| Exit | Meaning |
| --- | --- |
| 2 | Usage or configuration |
| 3 | Authentication |
| 4 | Write blocked by the profile |
| 5 | Client error (4xx) |
| 6 | Server error (5xx, or 422 for an unhandled exception) |
| 7 | Network or timeout |

Read `error.message` and `error.hint` first. `response.source` says who rejected the call:
`apim` is the gateway (usually credentials), `api` is EMWS itself. For a server error, put
`correlation.correlationId` and `correlation.kql` in your report so the failure can be found
in App Insights.

```bash
emws log --errors          # recent failures, from every agent on this machine
emws log <callId>          # one call in full: request, response, timing, correlation
emws log --here --last 5   # only calls made from this working directory
```

## Rules

- A `WRITE_BLOCKED` result is the answer, not an obstacle. Do not switch profiles or edit the
  configuration to get around it; ask the user.
- Staging runs what has merged to the `staging` branch and deployed. Your unmerged change is
  not there yet. To verify unmerged work, run the Functions host locally and use a profile on
  the `local` environment.
- Report what you verified with the call id, so the user can inspect it with `emws log`.
