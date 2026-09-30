# Project Guidelines

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
- Keep repository documentation, code comments, tests, and operator-facing text
  in English.
- Make focused changes and run the narrowest relevant lint, type, and test checks.
  Update `docs/plans/status.md` only when evidence supports the new state.
