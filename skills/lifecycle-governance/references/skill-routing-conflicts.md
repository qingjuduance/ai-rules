# Skill Routing Conflicts

Priority is native project assets, then `.ai-client/project/skills`, then `.ai-client/ai-client-governance/skills`.

When two skills or rule files appear to apply:

- Prefer the more specific project skill for project-local writing rules.
- Use the common governance skill for lifecycle, gates, worktree, task state, and framework-wide behavior.
- Record same-name skill conflicts in architecture guard or task evidence before editing either skill.
- Do not copy common governance skills into project-local skill directories as a compatibility route.

If a project rule would relax a common safety boundary, keep the stricter common boundary and record the conflict for human review.

When a skill mentions a removed command, old path, or obsolete storage model, treat it as a stale-skill incident: update the skill, run the focused validation, and record the stale reference in task evidence or framework debt.
