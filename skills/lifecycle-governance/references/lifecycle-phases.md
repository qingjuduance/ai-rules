# Lifecycle Phases

Use the smallest command set that records durable facts for the current phase.

- Session start: run sync-check and inspect open P0/P1 corrections or framework debt.
- Intake: use `lifecycle input-filter` to classify scope, task types, approval, command compression, shell proxy usage, and capability boundary facts.
- Preflight: apply the generated task-record payload and run `task-record gate --event preflight`.
- Execution: route important local commands through `task-run`, `gate-pool`, `shell-adapter`, `tool-invocations`, or `telemetry record`.
- Final: run completion-test for changed paths, then gate-pool or the equivalent `task-record gate --event final` plus `task-queue lifecycle --fail-on-drift`.
- Closeout: use worktree-task closeout commands for embedded governance worktrees and host-closeout for the host gitlink/state update.

If a phase cannot pass, record the blocker as task-record evidence, correction, framework debt, or explicit follow-up instead of silently skipping it.
