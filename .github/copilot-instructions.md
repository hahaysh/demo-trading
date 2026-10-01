# Project Guidelines

- At the start of a new work session, read `docs/plans/project-handoff.md` and
  `docs/plans/status.md`. Follow the restart checklist, distinguish historical
  evidence from current state, and do not treat prior approvals as authorization
  for new deployments, credential rotation, data collection, or live activation.
- Read `docs/plans/ats-master-plan.md` and `docs/plans/status.md` before making a
  cross-cutting architectural or scope change.
- This project supports research, backtesting, and Korea Investment paper trading
  only. Do not add live trading, leverage, shorting, derivatives, or intraday
  execution without a new approved plan and ADR.
- Treat strategies and champions as immutable, versioned artifacts. Promotion and
  rollback require explicit human approval.
- Keep risk policies and broker implementations outside agent-generated or
  strategy-controlled mutation surfaces. All order intents must fail closed
  through independent risk checks.
- Preserve point-in-time timestamps, revisions, source rights, provenance, and
  historical universe membership. Never silently substitute stale data.
- Run generated research code only in an isolated environment without broker
  secrets, arbitrary dependency installation, or unrestricted network access.
- Prioritize Korean for README usage explanations, user-facing descriptions,
  dashboard labels, and operator guidance for the Korean primary user.
- Keep technical foundations in English where appropriate: identifiers, API and
  JSON fields, schemas, commands, environment variables, tests, technical logs,
  code comments, machine-consumed exports, and parser-dependent document headings.
  Localize display text without changing historical artifacts or their hashes.
  Do not introduce risky refactors or language-switching infrastructure solely
  for translation; familiar English technical terms may remain.
- Make focused changes and run the narrowest relevant lint, type, and test checks.
  Update `docs/plans/status.md` only when evidence supports the new state.
