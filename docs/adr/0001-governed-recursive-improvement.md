# ADR 0001: Governed Recursive Strategy Improvement

- Status: Accepted
- Date: 2026-09-30

## Context

The system must improve strategies without allowing research automation to
change production behavior, risk controls, or broker integration directly.
Financial datasets are vulnerable to look-ahead bias, survivorship bias, data
revisions, and repeated-test overfitting. A single backtest engine also creates
common-mode implementation risk.

## Decision

Recursive improvement is an offline, governed champion/challenger process.

- Strategy versions are immutable and content-addressed.
- Every challenger references exactly one parent version and the frozen dataset
  snapshot used to evaluate it.
- A champion is never edited in place. Rollback selects a prior approved version.
- Lifecycle states are `DRAFT`, `VALIDATED`, `BACKTESTED`, `PAPER`, `CHAMPION`,
  `RETIRED`, and `QUARANTINED`.
- Parameter mutation is limited to ranges declared by the parent strategy.
- Risk policy references, broker behavior, lifecycle state, lineage, provenance,
  and execution environment are not mutable strategy parameters.
- Qlib supports research; LEAN independently certifies the exact candidate signal
  stream. Material disagreement fails closed.
- Promotion requires configured statistical gates, at least 20 paper sessions,
  zero hard-risk breaches, reproducible artifacts, and explicit human approval.
- Generated research artifacts run without broker credentials and cannot import
  or modify execution and risk modules.

## Consequences

- Research velocity is lower than unrestricted self-modification, but every
  candidate can be reproduced, audited, rejected, quarantined, or rolled back.
- Contract and policy design must precede connector, model, and broker work.
- Storage and experiment tracking must retain lineage and exact artifact digests.
- Human approval remains an operational dependency by design.
