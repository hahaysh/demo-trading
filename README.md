# Demo Trading

국내 주식과 ETF를 대상으로 전략을 연구하고 검증하는 자동매매 프로젝트입니다.
사용자가 읽는 설명과 화면은 한국어를 우선하며, 명령어·데이터 형식·기술 식별자는
호환성을 위해 영어로 유지합니다.

초기 검증 범위는 백테스트와 한국투자증권 Open API 모의투자입니다.
전략은 버전별로 고정하며, 위험 검사는 독립적으로 수행하고 운영 전략 승격에는
사람의 승인이 필요합니다. 범위와 완료 조건은 `docs/plans/ats-master-plan.md`에 있습니다.

`docs/plans/live-autotrading-plan.md`와 ADR 0003에 따라 실거래 기능의 단계적 구현은
승인됐지만, 실제 주문 전송이 승인된 것은 아닙니다. 실전 시세 조회는 별도 정책으로
제한하며, 기존 주문 계약은 여전히 모의투자 전용입니다.

## Local Setup

배포 준비 상태: **미완료(BLOCKED)**. 남은 구현과 승인 조건은
`docs/plans/predeployment.md`에서 확인할 수 있습니다. 아래 합성 데이터 예제는
로컬에서 실행할 수 있지만, 실제 투자 성과나 KIS 운영 인증 결과는 아닙니다.

준비물은 `uv`와 Python 3.11입니다. 다음 명령으로 환경을 구성하고 검사를 실행합니다.

```powershell
uv python install 3.11
uv sync --all-groups
uv run ruff check .
uv run pyright
uv run pytest
```

## Read-Only Research Dashboard

기존 합성 실행 보고서로 읽기 전용 HTML 대시보드를 생성합니다.

```powershell
uv run python -m ats.dashboard --report .local/predeploy-20261001-final/report.json --evidence docs/sources/kis-market-data.md --output .local/dashboard/index.html
```

생성된 HTML을 브라우저에서 바로 열면 됩니다. 서버, Node 빌드, 외부 리소스,
증권사 키나 클라우드 연결은 필요하지 않습니다. 기존 파일은 덮어쓰지 않으므로
다시 생성할 때는 새 출력 파일명을 지정해 주세요.
처음 내려받은 저장소라면 아래 합성 실행을 먼저 수행한 뒤, 생성된 디렉터리의
`report.json`을 `--report`로 지정합니다. `--evidence`는 선택 사항입니다.

화면에서는 실험 선택, 평가자산·낙폭 차트와 데이터 표, 후보 전략 비교,
CSV 미리보기·다운로드, 시뮬레이션 체결 필터, 위험 검사와 보고서 해시를 확인합니다.
모든 값은 선택한 보고서에서 가져옵니다. 화면의 금액은 표시용으로 반올림되며,
포함된 보고서와 CSV의 원본 소수 문자열은 유지됩니다. 회계 처리를 위한 API는 아닙니다.
CSV 열 이름과 실험 식별값은 기존 영어 형식을 유지합니다.

합성 성과·위험 시나리오와 사용자가 보고한 과거 KIS 조회 기록을 구분해서 표시합니다.
KIS 항목은 현재 연결 상태가 아니며, 미충족 조건도 보고서 생성 당시의 기록입니다.
화면 생성은 보고서 구조를 검사할 뿐, 수익률·출처·전략 승격·성과 인증을 보증하지 않습니다.
실주문을 실행하거나 제어하는 기능은 없습니다.

화면 생성기는 지정한 보고서와 증거 파일만 읽으며 원본을 수정하지 않습니다.
CSP로 네트워크 연결을 차단하고, 포함된 JSON이 스크립트 경계를 벗어나지 않도록 처리합니다.
다만 HTML 안에 보고서가 포함되고 접근 제어가 없으므로 개인 보고서를 공개 호스팅에
올리지 마세요. 로그인, 보고서 배포 서비스, Azure 준비·배포는 별도 승인 대상입니다.

## Runnable Synthetic Workflow

```powershell
uv run python -m ats.demo --output .local/my-new-run
```

매번 새 출력 디렉터리를 사용하세요. 합성 원본, 재현 실행·전략 입력, 위험 상태와
`report.json`이 생성됩니다. 일봉 정규화, 다음 거래일 시가 기준 추세 전략,
파라미터 후보 3개 비교, 위험 검사 허용·긴급 중단 거절을 검증합니다.
증권사나 클라우드에 접속하지 않으며 전략 승격이나 정책 변경도 하지 않습니다.
보고서에는 `deployment_ready=false`, `promoted=false`가 명시됩니다.

원문 보존·만료와 영속 paper 장부의 재시작 경계를 함께 검사하려면 다음 opt-in 예제를 실행합니다.
실제 API나 계좌에 연결하지 않으며, 전체 운영 파이프라인 완료를 뜻하지 않습니다.

```powershell
uv run --frozen python -m ats.demo --output .local/my-data-paper-run --exercise-data-to-paper
```

실제 Qlib 피처/LEAN 지표의 별도 격리 smoke는 [연구 안내](research/README.md),
전체 구현 진행과 남은 조건은 [연속 개발 기록](docs/plans/data-to-paper-plan.md)에 있습니다.

가격은 정확한 소수 값으로 처리하며 시가·고가·저가·종가(OHLC) 관계와 정수 거래량을
검사합니다. 원문 바이트, 정규화 해시, 종목과 종료 시각도 확인합니다.
이 합성 JSON 형식 자체가 KIS/DART 어댑터는 아닙니다. 거래일·기업행사·다종목 처리와
독립 엔진 검증은 별도이며, 예제는 제공된 거래 시각과 합성 비용 가정을 사용합니다.

## Full Data-to-Paper Development

재시작 수집 job·manifest·수정 snapshot, 실제 Qlib/LEAN 전체 엔진 평가와 모의 HTTP 주문을
연결한 합성 통합 예제입니다. 실행 중인 로컬 Linux Docker와 PowerShell 7이 필요합니다.

```powershell
./research/engines/build.ps1
$image = docker image inspect ats-backtest-engines:dev --format '{{.Id}}'
uv run --frozen python -m ats.pipeline_demo --output .local/my-full-pipeline --image $image
```

새 출력 디렉터리만 사용하며 실제 데이터·계좌·주문·배포에는 연결하지 않습니다.
엔진은 실제 라이브러리/전체 엔진을 실행하지만 broker는 MockTransport입니다.
지원하는 단일 종목 합성 모델, 의존성 보안 경고와 운영 전 조건은
[연구 안내](research/README.md#full-pipeline)와 [개발 기록](docs/plans/data-to-paper-plan.md#resumption-results)에 있습니다.
이 개발 검증을 실제 투자 성과 인증이나 실거래 활성화 승인으로 사용하지 마세요.

## KIS Production Quotations Only

`ats.data.kis.KisQuoteClient`는 토큰 발급과 과거 일봉 조회만 지원합니다.
주문·정정·취소·계좌 조회·이체 기능은 없으며 계좌번호도 필요하지 않습니다.
실전키 자체가 읽기 전용인 것은 아니므로 연구·전략 실행 환경과 분리해서 보관하세요.

사용 전 `docs/sources/kis-market-data.md`를 확인하세요. KIS는 개인의 자기 자산 투자 목적
이용과 제3자 제공 금지를 안내합니다. 계정별 약관·보존 조건은 운영자가 확인해야 합니다.
별도 승인된 1회 조회 검증은 완료됐지만 기본 `config/source-allowlist.yaml`은
여전히 DRAFT·비활성입니다. 이 승인으로 반복·대량 수집까지 허용되는 것은 아닙니다.

다음 명령은 기본 오프라인 동작이며 키를 읽지 않습니다.

```powershell
uv run python -m ats.data.kis --symbol 005930 --start 2026-09-01 --end 2026-09-30
```

이용 조건 확인 후 승인된 소스 정책을 준비하고, 수집용 로컬 환경에
`KIS_QUOTE_APP_KEY`와 `KIS_QUOTE_APP_SECRET`을 설정합니다. 키를 채팅에 붙여넣거나
커밋하지 마세요. 이 모듈은 `.env`를 자동으로 읽지 않습니다.
`--policy`로 승인된 파일을 지정하고 `--allow-network`를 명시해도 정책 검사를
통과해야만 연결됩니다. 단순 테스트를 위해 기본 정책을 임의 활성화하지 마세요.

연결 검증 CLI는 관측 시각·해시·행 수·기간 완전성 미검증 상태만 출력하며,
가격·토큰을 출력하거나 파일을 저장하지 않습니다. Python API는 원문, 요청 조건,
관측 시각과 정책 해시를 담은 메모리상의 `KisQuoteReceipt`를 반환합니다.
한국 시간 기준 오늘 이전의 최대 100일 구간을 조회하며 KRX 일봉 원주가를 사용합니다.
누락된 행을 만들거나 기간 완전성을 보증하지 않습니다. 저장·스냅샷·백테스트 연결은 별도입니다.

증권사 키 없이 가짜 응답으로 테스트하려면 다음 명령을 사용합니다.

```powershell
uv run pytest tests/unit/data/test_kis.py -q
```

### KIS Daily-Price Normalization

`ats.data.prices.normalize_kis_daily_prices`는 조회 결과를 고정된 `DailyPrice`와
기업행사 표시로 변환합니다. 네트워크·키 접근·파일 저장은 하지 않으며,
기존 합성 데이터 파서와 공개 가격 스키마도 변경하지 않습니다.

KIS 응답의 거래일만으로 실제 종료 시각을 추정하지 않습니다. 신뢰할 수 있는 거래일
정보에서 시간대가 포함된 종료 시각을 제공해야 하며, 빠진 날짜를 15:30으로 채우지 않습니다.
반환 시각은 UTC로 정규화합니다.

```python
from ats.data.prices import normalize_kis_daily_prices

normalized = normalize_kis_daily_prices(receipt, session_closes=verified_session_closes)
review_required = any(bar.requires_review for bar in normalized.bars)
```

위 예제는 조회 결과와 검증된 거래일 정보가 있다는 전제입니다.
`review_required`가 참이면 검토를 마치기 전까지 후속 연구 입력으로 사용하지 마세요.
가격은 정확한 소수, 거래량은 음수가 아닌 정수 문자열을 받습니다. 잘못된 OHLC,
누락 필드, 중복 키·날짜, 종목 불일치, 범위 밖 날짜, `mod_yn=Y`는 응답 전체를 거절합니다.
빈 응답이나 누락된 날짜를 보정하지 않으며, 기간 완전성이 검증됐다고 간주하지 않습니다.

`flng_cls_code`, `prtt_rate`, `revl_issu_reas`, `mod_yn`은 별도 메타데이터로 보존합니다.
알 수 없는 코드는 거절하고 기업행사 표시, 비어 있는 비율·재평가·시가 표시와 거래량 0은
검토 대상으로 표시합니다. 분할·배당 조정을 자동 적용하지 않습니다.
후속 처리에서 이 표시를 확인해야 하며 기업행사 처리 엔진이나 자동 승인 기능은 아닙니다.

정규화 결과에는 관측 시각, 요청, 정책·원문 해시와 정규화 버전이 남습니다.
전체 결과 해시는 가격 해시와 달리 기업행사 표시까지 포함합니다.
다만 출처 인증, 정책 승인 진위, 거래일 검증, 저장 권한이나 시점별 스냅샷을 제공하지는 않습니다.
정규화 테스트는 저장되지 않은 실제 조회 원문이 아니라 가짜 응답을 사용합니다.

## Data Contracts

아래는 개발용 상세 계약과 검증 규칙입니다. 기술적인 설명과 식별자는 영어로 유지합니다.

`MarketEvent` represents a derived observation, not an order. It carries a
versioned producer, confidence, availability timestamp, snapshot reference, and
the exact source revisions used as evidence. Before consuming an event, call
`event.validate_against_snapshot(snapshot)` to verify the reference digest and
evidence against the actual frozen snapshot. Event construction alone does not
resolve the snapshot. This does not replace runtime source-policy approval.

An event must be available by the snapshot cutoff and cannot predate its evidence.
Later retrospective extraction is not admitted into that historical signal path.
Publication and effective timestamps remain distinct from observed availability.

Regenerate both data schemas after changing these contracts:

```powershell
uv run python scripts/generate_data_snapshot_schema.py
uv run python scripts/generate_data_snapshot_schema.py --check
```

The generator retains its original name and now covers `DataSnapshot` and
`MarketEvent`. See `docs/plans/status.md` for implementation boundaries and the
next contract slice.

## Research Contracts

`ExperimentRun` is an immutable terminal receipt (`SUCCEEDED` or `FAILED`), not
a job scheduler. It records exact strategy and snapshot references, engine/code
artifacts, container and dependency-lock digests, seed, timestamps, and outputs.
Call `run.validate_inputs(strategy, snapshot)` before accepting it for evaluation.

`EvaluationResult` records one engine's finite measurements and a versioned
protocol reference. Call `result.validate_against_run(run)` to check the run ID
and digest, successful completion, and evaluation period cutoff. The protocol
must document metric definitions, transaction costs, and data splits. Negative
performance and data-integrity violations remain evidence, not discarded runs.
An omitted deflated-Sharpe confidence is unknown, not a passing result.

These checks do not verify artifact bytes, reproduce metrics, certify multiple
engines, authorize promotion, or replace independent risk checks. Execution,
statistical evaluation, and authenticated human approval remain separate work.

```powershell
uv run python scripts/generate_research_schemas.py
uv run python scripts/generate_research_schemas.py --check
```

## Promotion Receipts

`PromotionDecision` records an approved, rejected, or deferred review; it does
not change the champion or authorize orders. An approved receipt requires human
approval evidence bound to the digest of the exact `PromotionReview` and a
passing report reference for every declared promotion gate. Changing the review
invalidates the approval binding. Rejected/deferred receipts may retain missing
or failing gate evidence, but cannot carry an approval.

Before consuming a receipt, resolve each run's inputs and call
`decision.validate_evidence(policy, evaluations, runs)`. This checks exact
references, timing, candidate consistency, and, for approved receipts, policy
approval, Qlib/LEAN coverage, comparable snapshot/protocol/period, available
metric thresholds, and zero reported data-integrity violations.

Report statuses and `HUMAN` metadata are assertions, not verified identity or
statistical proof. An independent service must verify report contents, signatures,
reviewer authority, paper duration, risk history, fold results, slippage, and
engine tolerances before activation. The current contract implements none of
those runtime authorizations. Operator policies remain unchanged and `DRAFT`.

The existing research schema generator also covers `PromotionDecision`.

## Paper Execution Contracts

`OrderIntent` records a cash-funded, long-only KRX order for `KIS_PAPER`, with
strategy, champion-selection, snapshot, signal, account, and risk-policy
references. Quantities are positive integers. Limit prices use finite positive
`Decimal` values; market orders omit the limit price. Use decimal strings in
JSON to preserve price precision. No live environment or intraday phase is accepted.

`RiskDecision` defaults to `DENY`. An `ALLOW` receipt requires all declared
checks to have passing evidence. Before use, call
`decision.validate_for_intent(intent, policy, at=trusted_now)` using the current
operator policy and a trusted, timezone-aware clock. The decision must match the
entire intent digest and approved policy, fit inside the intent lifetime, and
satisfy `checked_at <= trusted_now < expires_at`. A modified quantity, price,
account, symbol, or other intent field invalidates the binding.

These records do not submit orders or prove real risk checks occurred. An
independent service must verify assessor authority, current positions and cash,
price freshness, exposure, losses, champion/universe eligibility, market session,
and kill switch. `SELL` must only reduce an existing long position. The contract
does not verify holdings, the trading calendar, or tick sizes. Atomic idempotency,
reservations, and revalidation at submission are still required; a digest cannot
prevent replay. All repository operator policies remain `DRAFT`.

```powershell
uv run python scripts/generate_execution_schemas.py
uv run python scripts/generate_execution_schemas.py --check
```

## Adapter Boundaries

`ats.ports` provides `QlibResearchPort`, `LeanCertificationPort`, and
`SignalEvaluatorPort`. Use `run_evaluation` or `run_signal_evaluation` instead
of calling an adapter directly to apply pre-dispatch input validation and
post-dispatch result binding. Evaluation requests pin experiment, strategy,
snapshot, engine, seed, dependency lock, protocol, and period. Signal outputs
carry artifact references only, not orders. Adapter errors propagate; no fallback
result is silently substituted.

These in-process interfaces currently have test doubles only. No real engine,
broker, signal implementation, or sandbox is supplied. A separate local artifact
resolver is available below; engine-specific materialization is not implemented.
Per-bar point-in-time replay and engine tolerances remain unresolved; see
`docs/adr/0002-adapter-boundaries-and-replay-semantics.md` (Proposed).

## As-Of Selection

`ats.data.select_records_as_of(snapshot, at=cutoff, revision_orders=orders)`
returns one known revision per source/item in deterministic source/item order.
The aware cutoff is inclusive and cannot exceed the snapshot freeze timestamp.
Future observations are excluded, while known announcements with future
effective dates are preserved.

Multiple visible revisions require an explicit `RevisionOrder` (oldest to
newest), including an evidence artifact and the time that ordering was observed.
Ordering learned after the cutoff is ignored. Labels and arrival order are never
used as revision precedence. Ambiguous, incomplete, or conflicting histories
raise `AsOfSelectionError` without partial results or fallback to older records.
Original snapshots and records are not modified.

This local selector is not yet integrated with an engine or a source connector.
It does not verify evidence bytes, source permissions, freshness, universe
membership, or economic applicability. Keep the snapshot, cutoff, and ordering
evidence together for reproducible selection. See ADR 0002 for the remaining
source-specific decisions.

## Local Artifact Resolution

`ats.data.LocalArtifactResolver(root, max_bytes=16 * 1024 * 1024)` reads a
prepopulated, trusted directory using `root/sha256/<64 lowercase hex digits>`.
It returns exact bytes only after SHA-256 verification and rejects missing,
corrupt, oversized, non-regular, or statically linked artifacts. It has no writer,
network fetch, repair, installation, or read-cache behavior.

Use `read_digest`, `read_artifact`, `read_raw_payload`, or
`read_revision_evidence` for explicit reads. Use
`resolver.select_verified_records_as_of(snapshot, at=cutoff, revision_orders=orders)`
to verify all visible raw revisions and applicable ordering evidence before
receiving selected records. Future-observed artifact files are not read. The
original `select_records_as_of` remains a pure, filesystem-free selector.

The store must be protected from concurrent untrusted writes; static path checks
are not a race-proof sandbox. A successful read proves byte integrity, not source
rights, timestamp truth, normalized-content correctness, or the meaning of a
revision report. Consumers must use verified bytes or reverify when reading again.
Actual symlink integration tests may be skipped when OS privileges are unavailable.

## Historical Universe Membership

`UniverseMembershipArtifact` holds the actual declarations behind a snapshot's
`UniverseMembershipManifest` reference. `UniverseMember` includes an instrument,
asset class, membership basis, observation time, effective interval, and evidence.
Equities use `KOSPI_200`; ETFs use `ETF_ALLOWLIST`. These labels are not proof of
exchange membership or operator approval.

Use `resolver.read_universe_membership(reference)` to verify and parse the exact
JSON bytes and match the manifest ID/as-of horizon. Use
`resolver.select_universe_members_as_of(snapshot, at=cutoff)` to return sorted
members both known and effective at that instant. Observation and interval start
are inclusive; interval end is exclusive (or open when omitted). Overlapping
declarations and queries beyond the snapshot/manifest horizons fail. For explicit
lookup, `artifact.require_member(instrument_id, at=cutoff)` rejects absent members.

Source evidence, completeness, interval correction provenance, and ETF approval
authority are not authenticated. Later-known removals must not be backdated into
an earlier declaration. The input builder below combines membership and record
selection but does not authorize orders. The existing data schema generator includes
`schemas/data/universe-membership.v1.schema.json`.

## Per-Decision Inputs

Use `ats.data.build_decision_inputs(resolver, snapshot, at=cutoff,
source_policy=approved_policy, revision_orders=orders)` to produce a frozen
`DecisionInputBundle`. The source policy is required; there is no implicit
allow-all default. It combines
verified record selection with effective, known universe membership. Instrument
records outside that universe are excluded with a source/item/revision reason.
Records with no instrument ID default to exclusion because they might represent
unresolved symbols rather than market-wide information. Explicit
`unscoped_policy=UnscopedRecordPolicy.INCLUDE` permits them and records that choice.

The builder verifies all visible raw history before scope filtering, not only
admitted records. Missing/corrupt artifacts or ambiguous revisions fail without a
partial bundle. Future raw observations and ordering evidence are not read.
`bundle.content_digest()` binds snapshot identity, UTC cutoff, universe, effective
members, applicable revision orders, source-policy reference, data requirements, inclusion policy,
records, and exclusions.
It does not remove the underlying snapshot's order-sensitive identity.

This is an in-process provenance receipt, not a persisted schema or trade
authorization. Engine integration remains unimplemented; explicit local freshness
requirements can be supplied as described below.
Direct model creation is not proof of verified bytes;
consumers that later read artifact files must use the resolver again.

## Source Eligibility

Bundle construction rejects policies that are not approved by the cutoff. Before
artifact I/O, every visible raw revision must come from a listed, enabled,
legally approved source, with matching resolved rights. This includes superseded
revisions and records later excluded by universe/unscoped filtering. Applicable
revision-order evidence must also have an eligible source. Future observations
do not affect earlier source eligibility checks. Failures produce no bundle.

`bundle.source_policy` pins the complete policy by ID, version, and digest.
Use `bundle.validate_source_policy(policy)` to resolve the reference and recheck
receipt eligibility. Rebuild from the original snapshot to recheck discarded raw
revisions. The caller must supply the trusted operator policy; hashes and approval
metadata do not authenticate the approver or prove current policy activation.

Collection-time rights, revocation, retention, rate limits, and universe
evidence authorization remain separate controls. Low-level resolver/selection
methods do not apply this gate. Repository policies remain `DRAFT`; only synthetic
test policies are approved in memory, and external collection remains disabled.

## Required Data

Pass `data_requirements=(SourceDataRequirement(...), ...)` to the builder to
require source data and freshness. Each rule specifies `requirement_id`,
`source_id`, `min_records`, `freshness_basis`, and `max_age_seconds`. Optional
`source_item_id` and `instrument_id` restrict matching; omitted filters match all
admitted records for that source. Requirements are included in the bundle digest.

Choose `OBSERVED`, `PUBLISHED`, or `EFFECTIVE` explicitly. The builder does not
invent a fallback timestamp. Missing/future timestamps, stale data, or too few
admitted records cause failure. All matching records must pass, so one recent
record cannot hide an older matching record. Age is elapsed UTC seconds, not
trading sessions; the maximum is inclusive. Source-specific economic timestamp
meaning must be established before using `EFFECTIVE` as a freshness basis.

An empty requirement tuple means no availability/freshness checks. This preserves
selection-only use; it is not a completeness guarantee. Requirements currently
come from the caller, not an authenticated operator policy store, and production
consumers must prevent strategy-controlled omission or relaxation. No operational
thresholds or repository policy approvals were introduced.

## Local Input Replay

Create `InputReplayRequest` with `snapshot`, `source_policy`, ordered `cutoffs`,
and explicit `data_requirements`, plus optional `revision_orders` and
`unscoped_policy`. Call `ats.data.replay_inputs(resolver, request)` to build all
per-decision bundles using the same pinned inputs. Cutoffs must be aware,
strictly increasing, and within snapshot/manifest horizons. Equivalent timezone
representations are normalized to UTC; invalid schedules are not silently sorted.

On success, `InputReplayResult` contains the complete ordered bundles and the
request digest. `result.validate_against_request(request)` checks declared
provenance and exact cutoff coverage, and `result.content_digest()` supports
repeat-run comparisons. No input archive, policy, or source file is modified.

The first failed cutoff raises `InputReplayError` with `index`, `cutoff`, and the
original chained cause; earlier bundles are not returned. There is no fallback
to prior data or partially successful replay. The result is kept in memory, not
published transactionally to storage. The archive must remain trusted/read-only.

This workflow replays input validation, not trading strategies or returns. It
does not run Qlib/LEAN, authenticate policies, calculate metrics, or submit orders.
An explicit empty requirement tuple still makes no freshness guarantee. Actual
engine integration and policy/snapshot changes across cutoffs remain separate work.
