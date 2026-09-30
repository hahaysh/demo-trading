# Predeployment Milestone Ledger

Updated: 2026-10-01

## Current Verdict

**NOT READY FOR DEPLOYMENT.** A local synthetic workflow runs end to end. It is
not a real-data backtest, independent certification, KIS paper session, production
risk service, or completed self-improvement pilot. The master plan and its human
approval gates remain authoritative. No real source, policy, or strategy was approved.

The live extension and ADR 0003 have user approval for staged implementation as
of 2026-10-01, starting with production quotations. This does not approve live
activation or deployment. A quotes-only client is implemented and verified with
61 offline tests; source-policy/retention approval and real-account smoke remain
outstanding. The original paper/certification gates are unchanged.

## Runnable Local Evidence

```powershell
uv sync --frozen --all-groups
uv run python -m ats.demo --output .local/my-new-run
```

The output directory must not already exist. It contains synthetic raw artifacts,
the replay request, immutable strategy, synthetic account state, and `report.json`.
There is no network, broker, cloud, promotion, or deployment operation. The local
directory is ignored by Git. Tests compare reports from two independent builds.

The 2026-10-01 local run produced:

- Five declared synthetic sessions for fictitious instrument `krx-test`.
- One TREND baseline with two simulated fills at subsequent session opens.
- Three immutable lookback descendants, within an explicit three-trial budget.
- Synthetic numerical risk acceptance and kill-switch denial.
- `deployment_ready=false`, `promoted=false`, and an explicit blocker list.
- Full suite: 463 passed, 2 skipped for Windows symlink privileges.
- Python wheel built successfully with `uv build --wheel`.
- CI YAML structure parsed successfully; remote Linux/Windows jobs not executed.

Evidence: `src/ats/demo.py`, `tests/unit/data/test_native.py`,
`tests/unit/data/test_prices.py`, `tests/unit/domain/test_execution.py`.
GitHub Actions quality jobs are configured for Linux and Windows but have not
been run remotely in this session. A local pass is not evidence of a CI run.

## Milestones and Exit Gates

| Milestone | State | Required exit evidence |
| --- | --- | --- |
| Local verified price-to-report workflow | Implemented, synthetic only | Deterministic reports, normalized hashes, next-open fills, costs, bounded candidate lineage, negative tests |
| Production KIS quotes-only connection | Implemented, offline tested; collection blocked | Approve source usage/retention and rate policy, configure secrets locally, explicitly verify token and quote responses; no order capability |
| Approved real-data backtest | Blocked and incomplete | Documented source rights; recorded real fixtures; calendars, corporate actions, delistings, coverage and data quality; actual source adapters |
| Qlib/LEAN independent certification | Not implemented | Both engines execute the exact candidate; approved comparison protocol/tolerances; golden order/return replay; untouched OOS and statistical gates |
| KIS paper execution and reconciliation | Not implemented | Paper credentials entered outside chat; independent authenticated risk state; atomic reservations/idempotency; token/retry/fill/reconciliation and kill-switch drills |
| Governed improvement pilot | Partial local sensitivity demo only | Durable registry, bounded scheduled experiments, statistical correction, approvals, rollback, and at least 20 actual paper sessions |
| News/factor/model improvement | Not implemented | Approved sources, bounded analysis, citations, isolated generated-code jobs, sandbox tests, cost controls, independent certification |
| Operator control plane | Not implemented | Authenticated API, durable append-only audit, approval verification, policy activation/revocation, operational monitoring and recovery drills |
| Deployment preparation | Not approved or validated | Restore subscription access; validate region/quota/policy/cost; separately approve infrastructure scaffolding; validate packaging/security/readiness |
| Deployment | Outside this authorization | Separate explicit approval after all applicable gates pass |

## Local Simulation Limits

- The native runner is a single-instrument TREND smoke evaluator, not a general
  `StrategySpec` engine. It uses `signal.lookback_days`; other strategy families
  are rejected. Broader portfolio construction semantics are not implemented.
- Decisions use only known daily closes. Simulated fills use the next supplied
  session's open plus explicit slippage. The schedule is supplied, not verified
  against the KRX calendar. The toy case has no corporate actions or delistings.
- Entry quantity uses an explicit allocation no greater than 10%, including a
  fee reserve. Participation is capped using previous known session volume.
  A zero-volume execution day does not fill. This is not an opening-auction
  liquidity model, tick-size model, or maintained real-time exposure limit.
- Reports mark remaining inventory to close. No forced terminal liquidation,
  benchmark excess-return certification, Sharpe significance, or OOS claim exists.
- Candidate comparison is an in-sample plumbing/sensitivity check, not the
  operational Phase 5 recursive loop. It never promotes or updates a champion.
- Native numerical risk assessment supports LIMIT orders only. State and opening
  windows must come from trusted independent providers. Cash-only valuation must
  balance. Open-order buy reservations conservatively count toward symbol exposure.
  Atomic reservations, authentic quotes, tick sizes, account reconciliation,
  assessor authentication and replay prevention are not supplied by this function.
- The risk smoke is a separate synthetic later-session scenario, not a KIS order.
  Its policy and champion references are test claims, not operational approvals.

## External Inputs Needed

1. Source-specific usage and retention approvals plus legally usable recorded
   data. Existing `config/` policies remain DRAFT and must not be silently enabled.
2. Production KIS credentials configured locally for the quotes-only collector,
  after source-policy approval. Separate KIS paper credentials remain necessary
  for paper-session validation. Never paste secrets into chat. No broker request
  has been sent.
3. Review of unresolved ADR 0002 semantics and quantitative certification criteria.
4. Subscription access and explicit infrastructure-scaffolding consent. The prior
   HTTP 403 is recorded history; access has not been retested in this session.
5. Human promotion/rollback authority and elapsed evidence from actual paper
   sessions. Neither signatures nor elapsed sessions can be manufactured in tests.

## Next Work

Resolve the source-data gate and produce a real-data normalization/backtest
acceptance dataset before expanding simulation claims. Implement and verify the
actual engine, KIS, registry, and operator services against that evidence. Do not
replace unavailable engine runs or paper sessions with synthetic passing reports.
