# Start and track selected work

## Resolve the direction

A request to start selected work in new threads authorizes those launches; proceed without asking again. A report request alone does not. Keep in-session subagents distinct from separate top-level conversations. Starting work does not expand permission to merge, deploy, close incidents or post comments.

Resolve a selected row against the report's stated default order and save its stable workstream ID and title. A displayed row number is not a GitHub issue number or a position in another sorted view. Preserve the requested investigation/implementation scope and any model, effort or existing-thread preference.

Confirm the repair owner before selecting a project. The repository containing an incident may be a consumer of the actual defect in `packages`, `emws_shared` or another service. Refresh matching fixes, consumer adoption, staging/deployment evidence and active work before creating another repair. Name overlapping tasks and divide ownership where selected work touches shared code. Ask only when an unresolved ownership or scope decision blocks safe progress.

## Bind the workspace and model

In T3 Code, inspect the current tool schema and `orchestrator_capabilities`. Resolve the requested provider/model and supported options from that catalog. Set effort and other options in structured arguments, not only in the handoff prose. Report an unavailable setting rather than silently substituting a model or downgrading effort.

For each independent remediation thread, use `t3_thread_launch` with the destination `projectId` and explicit `workspaceStrategy`. Select a dedicated feature worktree before the agent starts. Read the destination instructions for its base and PR target; assessment branches and deployed revisions are separate facts. Use `existing_worktree` only when deliberately continuing that checkout. For a stack based on local commits, use the intended parent branch and `startFromOrigin:false`; uncommitted changes are not copied.

Example launch shape after resolving real project, branch and model values:

```json
{
  "projectId": "<repair-project-id>",
  "title": "Investigate <workstream-title>",
  "workspaceStrategy": {
    "type": "worktree",
    "baseRef": "<verified-base-branch>",
    "branch": "fix/<workstream-id>",
    "startFromOrigin": true
  },
  "modelSelection": {
    "instanceId": "<catalog-provider-instance>",
    "model": "<catalog-model-id>",
    "options": [{"id": "reasoningEffort", "value": "high"}]
  },
  "message": "<handoff-brief>"
}
```

The effort option above is a Codex example; use the selected model's descriptors. Inspect the returned selection and prepared workspace, using thread configuration/read tools as needed, before saying the requested setup is running. Omitted workspace strategy means the project root, not the caller's worktree. Asking an agent to create or `cd` into a worktree does not bind its T3 thread. `create_threads` shares the caller's checkout and is unsuitable for independent work in different projects/worktrees.

For child work, use delegation rather than top-level launch tools. With nonempty inherited model options or a requested option, use `delegate_task` with structured `target.options`. Retain its `taskId`; a child thread ID is not an independent remediation conversation. Each delegated review round is a new task with the original brief, prior findings/responses and unresolved objections.

## Write the handoff

Give each thread a self-contained, bounded brief containing:

- The user's requested action, stable report ID/title, exact incident repository/number links, correct repair project and declared PR target.
- Snapshot UTC bounds and accessible evidence/report paths or links. Summarize relevant observations, units and affected builds; label hypotheses and missing evidence.
- Matching fixes, current pins/deployments and what must be refreshed first. Include the explicit staging-first check when directed.
- Scope boundaries, overlapping tasks, file ownership and dependencies. Preserve decisions already made, declined work and authorization limits.
- A concrete completion contract: diagnosis and next action, or justified scoped repair with appropriate tests and a reviewable PR. Distinguish code, consumer adoption, release and runtime verification. Refer to destination instructions for Git, validation and attribution requirements.

Verify that evidence is accessible to the new thread; transfer the necessary sanitized packet or links when it is not. A missing local file must remain an explicit evidence gap. Carry private operational identifiers only through authorized private context.

## Track the result

Keep a private dispatch record mapping workstream ID to thread ID, project, branch, model/options, scope and status. Return clickable thread links with the exact returned ID, such as `[Timing investigation](t3-thread://v1/<threadId>)`.

Launch acceptance and workspace preparation are not completed investigation or implementation. Retain returned IDs and inspect preparation errors. Since `t3_thread_launch` has no retry key, inspect the thread list after an error or lost response before retrying. If launch capability is unavailable, retain the briefs and report the block without claiming threads started.

On the user's next refresh, read the actual thread outcomes and current issue/PR/deployment state. Link and reconcile resulting PRs through the available thread tools. Starting a thread alone does not remove its work from the active list. Move completed merged work to delivery follow-ups as appropriate; keep unresolved findings and dependencies visible. Create additional top-level threads only when the user's direction covers them.
