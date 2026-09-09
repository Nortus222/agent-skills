---
name: share-artifacts
description: Share agent-produced artifacts through Tailscale when handing over an HTML report, downloadable file, or local web build or preview. Use before returning a local artifact link, even when the user did not mention Tailscale. Does not apply to existing external URLs or source-code file references.
---

# Share artifacts

Give the user a working Tailscale URL for artifacts they should open on another
device. Keep sharing within their tailnet. Use Tailscale Serve, never Funnel unless
the user explicitly requests public internet access. An existing public deployment
URL can be returned as-is.

## Discover the host

Run these on the machine that will host the artifact:

```bash
tailscale status --json
tailscale serve status --json
tailscale serve --help
```

If `tailscale` is absent from PATH on macOS, check
`/Applications/Tailscale.app/Contents/MacOS/Tailscale`. Require a running, signed-in
client. Read the host from `Self.DNSName`, trimming its final dot; read the Tailscale
IP from `Self.TailscaleIPs`. Discover these each time rather than saving one
machine's identity in this skill. Inspect only the status fields needed; keep full
network inventories out of artifacts and commits.

If the agent runs in a container or remote sandbox, confirm it can run the server
and Serve on the same Tailscale-connected host. Replacing `localhost` in a URL
does not expose a server or bridge two machines.

## Prepare the artifact

For a report, download, or static build, put only the intended files and their
assets in a dedicated serving directory. For links intended to outlive a worktree,
copy those files to a persistent directory, such as
`~/.local/share/agent-artifacts/<unique-artifact-id>/`. Keep logs and process
metadata outside that directory. Check symlinks so serving cannot expose files
outside the intended bundle.

Use an existing suitable server, or start a static server bound to loopback:

```bash
python3 -m http.server "$local_port" --bind 127.0.0.1 --directory "$artifact_dir"
```

Choose an unused local port. Launch the command through the environment's durable
background-process facility, or detach it with stdin closed and logs redirected.
Record the PID or service identity, serving directory, and restart command outside
the served directory. Confirm it survives the launching shell. A foreground tool
session alone is not a durable host. On macOS app installations, proxy this server:
Serve cannot directly serve files or directories from those variants.

For a running web app, reuse its server and preserve its required routing behavior.
Prefer a dedicated Serve port at `/` when the app assumes root-relative paths.
Allow the exact Tailscale hostname in the framework's host/origin settings if
needed. Verify that browser-facing API and WebSocket URLs also work from another
device; browser-side `localhost` addresses refer to the viewing device.

## Expose through Serve

Check the local URL first. Inspect existing Serve listeners and routes before
choosing a port. Reuse a route only when it belongs to this artifact. Otherwise
choose an unused tailnet port, checking both Serve configuration and host listeners.
Preserve other artifacts, and avoid any listener marked for Funnel/public access.
Never use `tailscale serve reset` to make room.

Prefer HTTPS when available:

```bash
tailscale serve --bg --https="$tailnet_port" "http://127.0.0.1:$local_port"
```

Use the actual URL reported by Serve and confirmed in its status. `--bg` preserves
the proxy configuration; it does not keep the upstream server alive.

If HTTPS needs account-side enablement, report the setup URL or error returned by
the CLI. For an ordinary static artifact, use HTTP Serve on an unused port while
HTTPS is unavailable:

```bash
tailscale serve --bg --http="$tailnet_port" "http://127.0.0.1:$local_port"
```

HTTP over Tailscale still travels through the encrypted tailnet, but browsers do
not treat it as an HTTPS secure context. If the app requires secure-context APIs,
finish preparing it and request HTTPS enablement instead of claiming the HTTP
fallback meets that requirement. Bound setup commands with a timeout so an
interactive enablement prompt cannot leave the agent waiting indefinitely.

For HTTP, use `http://<Self.DNSName>:<port>/<artifact-path>`. If MagicDNS is
unavailable, verify and use the Tailscale IPv4 address. HTTPS links must retain
the certificate hostname. URL-encode file paths and include a specific filename
when appropriate.

## Verify and hand over

Fetch the final Tailscale URL and confirm it returns the intended content, including
required assets and redirects. Use a browser when app behavior needs checking.
Check from another authorized tailnet device when one is accessible; a fetch from
the hosting machine alone does not prove another device's DNS or access policy.
Report that distinction without making a second-device check a prerequisite for
providing a locally verified link.

Return a clickable Tailscale link as the primary way to open the artifact. State
that the viewing device needs Tailscale access and the hosting machine must remain
awake, connected, and serving. Mention whether the server survives a reboot; do
not imply permanent hosting. Leave the delivered server and route running.

Record the URL, upstream process, artifact location, and exact cleanup command
outside the served directory. For a dedicated port, cleanup is
`tailscale serve --https=<port> off` (or `--http=<port> off`), followed by stopping
only the server owned by this artifact. Recheck ownership before cleanup.

If sharing is blocked, retain the artifact and explain the specific missing step.
Ask for `tailscale version`, the running state, `Self.DNSName`,
`Self.TailscaleIPs`, `CurrentTailnet.MagicDNSEnabled`, `CertDomains`, and
`tailscale serve status` from the intended host only when you cannot inspect them.
The user can omit unrelated routes and peer/account details. Never present an
invented Tailscale URL or a localhost link as a completed cross-device handoff.

## References

Consult the installed CLI help for syntax and the
[Serve command reference](https://tailscale.com/docs/reference/tailscale-cli/serve)
for proxy modes and platform limitations. The
[Serve setup guide](https://tailscale.com/docs/features/tailscale-serve) covers
HTTPS enablement and tailnet access prerequisites.
