# Feature Self-Iteration

Governance feature changes should include the feature, the gate or runtime check that proves it works, and a focused selftest or explicit framework debt when full enforcement is too large.

Before finalizing a feature change, check:

- The runtime component registry or manifest exposes the new command, gate, or policy.
- The CLI path has stable JSON output for automation.
- False claims are avoided: plugin-enforceable, plugin-auditable, host-client-required, and model/API-required controls are separated.
- Selftest covers the success path and at least one failure or drift path when practical.
- Any deferred schema migration, host integration, or broad refactor is recorded as framework debt with severity.

Each feature record or runtime component should name:

- Trigger or matching strategy.
- Priority and conflict behavior.
- Freshness or stale-review trigger.
- Telemetry or effectiveness metrics.
- Feedback sources, including corrections and framework debt.
- Validation/selftest coverage.
- Deprecation or replacement cleanup rule.

If one of those fields is missing, do not mark the feature fully implemented; either add the metadata and check, or record a follow-up framework-debt row.
