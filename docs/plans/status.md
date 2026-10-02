# Project Status

Last updated: 2026-10-02

새 작업 시작 전 [프로젝트 인수인계와 점검표](project-handoff.md)를 먼저 확인합니다.

## Active Milestone

최신 로컬 검증은 **698 passed, 2 skipped, 1 warning**이며 타입·lock·4종 schema·JS 검사를 통과했습니다.
공유 계정·호가/capacity·합성 분할/순배당, 독립 서명 증거, bounded 상주 수집과 health를 추가했고
ProDotNetZip 기반 고정 소스 보안 재빌드로 실제 4주기 RSI·holdout·재개를 실행했습니다.
현재 증거는 `.local/rsi-w8-20261002/`, 상세는
[Current Verification and Boundaries](rsi-development-plan.md#current-verification-and-boundaries)입니다.
KRX 세 시장 기본정보·DART 경유 거래소 공시, 자료 분류 권한과 게시물 철회 격리, 명시적
총수익 학습 특징·RS256 검증도 추가했습니다. 역분할/단주/합병 등 추가 시장 구현과 실제 운영
근거·외부 계약·승인 대기를 구분하며 모두 구현됐다고 표시하지 않습니다.
실자료·운영 신원·실제 paper 세션·ADR 0004는 미승인/미연결이며 A-E 전체는 미완료입니다.

후속 연속 개발은 [Implementation Follow-Up](rsi-development-plan.md#implementation-follow-up)에
기록합니다. 원문/분석 보존 재검증, Ridge/ElasticNet 탐색, block bootstrap·리뷰 게이트,
독립 서명 증거 검증, 데이터 복구와 상수 슬리피지의 실제 두 엔진 비교를 추가했습니다.
새 합성 root는 `.local/rsi-retained-20261002/`이며 전체 ATS는 여전히 미완료입니다.
아래 677개 및 그 이전 내용은 앞선 체크포인트이고 최신 실행 근거는 후속 기록을 따릅니다.

현재 진행 대상은 [RSI ATS A-E 전체 개발](rsi-development-plan.md)입니다.
현재 재개 작업에서 DART/RSS/Atom/Naver/공개 Telegram export와 공식 KRX 종목 기본정보 어댑터,
시점별 규칙 분석, 실제 GP 후보 제안·Ridge 신호 학습, 영속 최종 holdout 분리, JWT 운영 API/화면을
추가했습니다. 실제 모델/엔진과 합성 장부의 다주기·재개를 실행했으며 A-E 전체는 미완료입니다.
현재 전체 검사 **677 passed, 2 skipped, 1 warning**, lint/format/types/lock/schema 통과.
현재 합성 연구 4주기·학습 제안 2회·수집 12주기와 고정 holdout을 실제 엔진으로 검증했습니다.
DSR 진단도 실제 격리 SciPy로 연결했으며 6개 합성 OOS 관측은 신뢰도 없이 검증 불가로 남깁니다.
shadow에서는 미승인 후보가 champion 검사로 거절되며 실제 broker 요청은 없습니다.
승인 경계는 [제안 ADR 0004](../adr/0004-paper-trial-authorization.md), 실제 현재 구현·한계는 RSI 계획을 따릅니다.
다중 정보원·정보 분석·실제 다주기 연구·시장 검증·운영 서비스는 진행 중이며 아래 이전
로컬 통합 완료를 그 목표의 완료로 해석하지 않습니다. 완료 기준/새 근거는 새 계획에 기록합니다.

2026-10-02 후속 상태 점검에서는 집중 테스트 **22 passed, 1 warning**, 저장된 holdout 엔진
결과 8건과 해시 일치를 재확인했습니다. 위 677개는 앞선 전체 실행 기록이며 합산하지 않습니다.
이후 정리 요청으로 [진행 요약](rsi-development-plan.md#progress-summary),
[잔여 작업 W1-W8](rsi-development-plan.md#work-queue),
[향후 고려사항](rsi-development-plan.md#further-considerations)을 정리했습니다.
할 일 개수는 전체 A-E 완료율이 아닙니다. 기반 검증 완료·남은 개발·승인 경계를 분리하며
이번 문서 정리로 실제 수집·계좌/주문·예약·배포·Git 게시를 승인하거나 실행하지 않았습니다.

## Historical Data-to-Paper Milestone

이전 결과는 [연속 개발의 Resumption Results](data-to-paper-plan.md#resumption-results)입니다.
영속 수집 job/lease/checkpoint·manifest·수정 이력·snapshot/resolver, 실제 Qlib backtest와
LEAN 전체 Engine.Run 어댑터, KIS paper OAuth/계좌/주문/취소/대사·승인된 복구를 구현했습니다.
합성 job 5개 -> snapshot -> 실제 엔진 2개 -> MockTransport 주문을 통합·반복 검증했습니다.
당시 전체 검사 **651 passed, 2 skipped**, 린트·포맷·타입·스키마·lock 통과.
지원하는 단일 종목 TREND 합성 모델의 로컬 개발 경로 완료이며 실제 운영 인증은 아닙니다.
실제 수집·계좌·주문·예약·배포·커밋·푸시는 하지 않았습니다. LEAN 전이 의존성 high/critical
경고, 실제 KRX 모델·다종목/일반 연구, 인증된 운영 서비스·실제 API·20개 paper 세션은 남습니다.
이번 시작 시 사용자 미커밋 변경과 기존 `3817b42`를 보존했습니다. 아래는 과거 checkpoint입니다.

Working toward predeployment exit gates in `docs/plans/predeployment.md`.
The runnable local milestone is now synthetic price normalization, native trend
backtesting, bounded candidate comparison, and independent risk smoke checks.
Overall deployment readiness remains BLOCKED; real integrations are not complete.

A local read-only dashboard now visualizes an existing synthetic report and a
separately labeled historical KIS smoke receipt. Export with `python -m ats.dashboard`
and open the HTML directly. The local exporter uses no credentials or live order
control. The separately approved Azure F1 synthetic preview is now published
with owner-restricted authentication. The operator accepted the unperformed
different-tenant user test for this synthetic artifact only; full ATS readiness
and live trading remain blocked.

The approved staged extension now includes a production KIS quotes-only client
with offline transport tests and a user-reported successful one-off production
smoke for 005930 on 2026-09-30. The supplied receipt contains one row and matches
the local smoke-policy digest. Raw data was not persisted and coverage remains
unverified. The default source policy stays DRAFT; bulk/recurring collection is
not enabled by this smoke approval.

Phase 1 primary contracts and local adapter boundaries implemented; real adapters
and Phase 0 gates remain open. Phase 2 local as-of selection and artifact byte
verification are implemented, including historical universe artifact resolution.
Verified records and effective membership can now be composed into a local
per-decision input bundle.

## Completed

- Applied the operator's Korean-first presentation preference to README usage
    explanations and dashboard labels, descriptions, accessibility text and KST
    timestamps. Technical documentation, parser-dependent headings, CLI commands,
    internal values and the English CSV format remain unchanged. The project
    instructions now preserve this distinction rather than requiring English
    for all user-facing content.
- Exported `.local/dashboard/korean.html` without overwriting the earlier view.
    Seven dashboard tests passed, including exact embedded-report preservation;
    types, lint and document diagnostics passed. Browser checks verified Korean
    candidate/side/risk displays, unchanged CSV headers and five rows, matching
    receipt policy digest, nonblank charts and desktop/mobile overflow checks.
- Added a standalone offline report dashboard with candidate selection,
    equity/drawdown charts, chart data table, simulated transaction filters,
    risk scenario inspection, CSV preview/download and provenance details.
    Seven focused export tests passed, including invalid-input rejection,
    overwrite prevention, optional evidence and script-termination escaping.
    Strict types and lint passed. Browser checks exercised desktop/mobile layout,
    chart pixels, candidate/side/scenario changes and CSV preview. Native download
    completion could not be verified in the integrated browser; CSV text was verified.
- Generated `.local/dashboard/index.html` from the existing synthetic final run
    and user-reported KIS receipt. No new research run, broker request, operational
    policy change, strategy approval or cloud deployment was performed.
- Recorded user implementation approval of the live extension and ADR 0003,
    retaining separate live activation and deployment gates.
- Reviewed official KIS personal-use, token and daily-price documentation;
    the operator subsequently attested applicable terms review and a permitted
    retention end date of 2027-09-30. The separate one-off smoke policy records
    that attestation, a stricter one-day local maximum and a one-per-minute cap;
    this is not independent legal verification or approval for general collection.
- Implemented `ats.data.kis`: fixed production token/daily-price routes, policy
    gating before network, memory-only tokens/receipts, throttling, bounded reads,
    disabled redirects/proxies, sanitized errors and an offline-by-default CLI.
    HTTPX is locked. The initial 61 focused offline tests passed. Persistent token
    coordination, general collection and a saved point-in-time dataset remain incomplete.
- Implemented offline `normalize_kis_daily_prices` with exact `DailyPrice` bars,
    caller-supplied session close times, preserved corporate-action flags and
    receipt provenance. Invalid/incomplete/no-open rows reject the batch; action
    flags, blank flag values and zero volume require downstream review.
    No price adjustments, calendar authentication, snapshot creation or storage
    are implied. 149 relevant offline price/KIS/native tests, types and lint passed.
- Recorded the user's production smoke receipt observed at
    `2026-09-30T17:00:21.626656+00:00` (2026-10-01 02:00:21 Korea time): one
    daily-price row, `coverage_verified=false`, `persisted=false`. The policy
    digest was independently matched locally without another API request.
    Full receipt and verification limits: `docs/sources/kis-market-data.md`.
- Confirmed market, cadence, paper-only broker boundary, and pilot budget.
- Selected immutable champion/challenger improvement with human promotion.
- Selected Qlib for research and LEAN for independent certification.
- Defined initial risk and promotion thresholds.
- Persisted the approved master plan and core governance decision.
- Established the Python 3.11 `uv` package with Ruff, Pyright, pytest,
    Hypothesis, pre-commit, and a locked development environment.
- Implemented immutable `StrategySpec` lineage, bounded signal-parameter
    mutation, deterministic content digests, and append-only lifecycle events.
- Generated `schemas/strategy/v1.schema.json` with a drift-check command.
- Added typed, operator-owned source, risk, and promotion policies. The original
    policies remain `DRAFT`; a separate explicitly approved KIS smoke policy
    permits only the named one-off connection check.
- Implemented immutable `PointInTimeRecord`, `DataSnapshot`, and historical
    universe-manifest contracts. A snapshot rejects future-observed records and
    duplicate source revisions, and has a deterministic content digest.
- Generated `schemas/data/snapshot.v1.schema.json` with a drift-check command.
- Implemented frozen `MarketEvent` observations with versioned producer evidence,
    finite confidence, unique source revisions, and availability cutoff checks.
    `validate_against_snapshot` checks the snapshot ID, cutoff, digest, and exact
    evidence records; consuming code must call it before using an event.
- Generated `schemas/data/market-event.v1.schema.json` through the existing data
    schema generator, including missing/stale schema regression tests.
- Implemented frozen `ExperimentRun` terminal receipts and `EvaluationResult`
    evidence. Runs pin strategy/snapshot references, code, engine, container,
    dependency lock, random seed, times, and output artifacts. Evaluations bind
    to the exact successful run digest and a versioned evaluation protocol.
- Added research schema generation and missing/stale checks for both contracts.
- Implemented frozen `PromotionDecision` receipts with review-bound human
    approval evidence, complete gate attestations, and exact policy/evaluation
    digest resolution. Approved receipts require an approved policy, comparable
    Qlib/LEAN evidence, and passing available numerical metrics.
- Extended the existing research generator with the promotion decision schema.
- Implemented frozen paper-only `OrderIntent` and default-deny `RiskDecision`
    contracts. Risk receipts bind the exact order and operator policy digests,
    require complete passing check evidence for `ALLOW`, and validate expiry.
- Added execution schema generation and missing/stale tests for both contracts.
- Added typed Qlib research, LEAN certification, and signal-evaluator ports with
    request/output validation wrappers. Tests use deterministic in-memory doubles,
    including invalid input rejection before adapter invocation.
- Recorded unresolved replay, revision, universe, runtime, and engine-comparison
    semantics in proposed ADR 0002; no tolerances or new approvals were selected.
- Implemented local as-of selection with inclusive observation cutoffs,
    explicit time-bound revision ordering, deterministic output, and fail-closed
    ambiguous/incomplete history handling. Original snapshots remain unchanged.
- Recorded the tested local selection semantics and remaining source-specific
    decisions in proposed ADR 0002.
- Implemented a read-only local SHA-256 artifact resolver with bounded reads,
    exact-byte verification, static link/reparse rejection, and no stale fallback.
    Verified as-of selection checks all visible raw revisions and ordering
    evidence while leaving future artifact files unread.
- Implemented immutable `UniverseMember` and `UniverseMembershipArtifact`
    contracts, with observation/effective-time selection and rejection of
    overlapping intervals. The resolver verifies artifact bytes, manifest ID,
    and horizon before returning effective, known members for a snapshot cutoff.
- Added the universe membership artifact to the existing data schema generator.
- Implemented `build_decision_inputs` and immutable `DecisionInputBundle` with
    snapshot/cutoff/universe/revision provenance, scope filtering, and explicit
    exclusion reasons. Unscoped records are excluded unless explicitly included.
    Visible raw history is verified before filtering; failures return no bundle.
- Added mandatory source-allowlist validation to bundle construction before
    artifact reads. Policies must be approved by the cutoff; every visible raw
    revision and applicable revision order must use an eligible source. Record
    rights must match the policy classification, including superseded/excluded data.
- Bundle provenance now includes the exact source policy ID, version, and digest;
    `validate_source_policy` resolves that reference and rechecks receipt eligibility.
    Repository policies remain unchanged and `DRAFT`; tests use synthetic approvals.
- Added explicit `SourceDataRequirement` checks to bundle construction and
    receipt validation: minimum admitted counts, scoped source/item/instrument,
    and maximum age against a declared observation/publication/effective timestamp.
    Requirements are retained in the bundle digest; no production limits were set.
- Implemented `InputReplayRequest`, `InputReplayResult`, and `replay_inputs` for
    strictly increasing UTC cutoffs over pinned local inputs. Each cutoff runs
    the existing bundle builder; failure raises with its index/cutoff and chained
    cause, without returning partial results. Results bind the complete request.
- Added exact-decimal `DailyPrice` normalization from verified local JSON bytes,
    with instrument/session provenance, OHLC/volume checks, and normalized hashes.
- Added a functional single-instrument native TREND smoke backtest with next-session
    opens, explicit transaction costs/slippage, volume caps, and equity/drawdown reports.
- Added bounded immutable lookback comparisons and a numerical paper LIMIT risk
    assessor. No registry, authenticated approval, broker submission, or certification
    is implied by these local functions.
- Ran `uv run python -m ats.demo --output .local/predeploy-20261001-final`:
    two baseline fills, three candidate reports, risk ALLOW and kill-switch DENY,
    with deployment and promotion flags remaining false.
- Added deployment-free Linux/Windows CI configuration; remote CI has not run here.
- Built the Python wheel and validated CI YAML structure locally.
- Latest local checks on 2026-10-01: 463 tests passed; 2 actual symlink tests
    skipped because Windows link creation requires privileges unavailable here.
    The mocked reparse-attribute test, lint, format, types, schemas, and lock
    checks passed.
- Passed focused validation for the point-in-time contract and schema: Ruff,
    Pyright, 6 unit tests, and 2 schema contract tests on 2026-09-30.

## In Progress

- GitHub Actions 자동 배포를 우선 구현했습니다. PR에는 Azure 권한이 없으며 main의
    Linux/Windows 품질 검사 후 같은 실행의 합성 ZIP만 OIDC로 게시하도록 연결했습니다.
    로컬 합성 빌드·패키징과 최종 경계 검사 23개, 실제 사이트 설정의 읽기 전용 검사가 통과했습니다.
    배포 전용 Entra 앱·비밀 없는 연합 신뢰·웹 앱 한정 Website Contributor, main 전용
    GitHub 환경과 변수는 실제 구성·재조회했습니다. 후속 승인으로 커밋 `3293182`를 main에
    푸시했고 [Actions 36865676575](https://github.com/hahaysh/demo-trading/actions/runs/36865676575)가
    2026-10-01 13:03:19 UTC에 성공했습니다. Linux/Windows 각각 pytest 596개, CI 경계 검사 23개,
    OIDC 로그인, ZIP 게시, 사후 해시·익명 차단 검사가 통과했습니다. 13:04:57 UTC 별도 조회도
    서버 해시 일치·익명 401·본인 제한·기본 게시 인증 false를 확인했습니다. 합성 HTML만 갱신됐고
    로그인 비밀과 인증 정책은 변경하지 않았습니다. PR·수동 실행·실제 복구의 별도 원격 시험과
    이번 자동 게시 후 본인 브라우저 회귀 검사는 미수행입니다. Node 20 액션의 Node 24 강제 실행
    경고와 ubuntu-latest 이미지 변경 예고는 유지보수 항목으로 남깁니다.
    반복 합성 게시에 한한 타 테넌트 검사 예외 확대를 승인받았습니다. 당시 나머지 ATS 개발은
    사용자 검토 전까지 보류했으나, 후속 연속 개발 요청으로 로컬 개발을 재개했습니다.
    상세 범위·ID·복구 절차: `infra/github-actions.md`.
- 2026-10-01 후속 승인 후 Azure for MCT / `atsview-rg` / Korea Central에 Windows F1
    플랜과 사이트, 본인 전용 인증 및 게시 정책을 배포했습니다. 최종 what-if는 생성 6개,
    수정·삭제 0개였고 ARM 배포와 하위 리소스 5개가 `Succeeded`입니다. 실제 보안 설정
    13개를 확인했습니다. 전용 Entra 앱과 30일 인증 비밀을 생성했으며 비밀값은 로컬에
    보관하지 않았습니다. 현재 만료는 2026-10-31 09:28:37 UTC입니다.
    접근 검증 명령 중단 후 공개 진입을 `Disabled`로 복구하고 익명 HTTP 403을 확인했습니다.
    후속 브라우저 검사에서 사용자가 본인으로 확인한 계정은 로그인 후 기본 페이지 200,
    같은 테넌트 비소유자로 확인한 계정은 콜백 403을 반환했습니다. 신원 클레임 직접 대조는
    미수행입니다. 익명 경로 3개와 잘못된 토큰은 401이었고 검사 후 Disabled 복구를 확인했습니다.
    추가 승인 후 기존 비밀로 발급한 유효 앱 전용 토큰은 3개 경로에서 403으로 거부됐으며
    같은 시점의 익명 요청 401 및 IP 차단 헤더 부재로 네트워크 차단과 구분했습니다.
    비밀 생성·교체 없이 검사 후 Disabled를 복구했습니다. 이후 사용자가 다른 테넌트 검사의
    미수행 위험을 현재 합성 보고서에 한해 수용하고 게시를 승인했습니다. Entra로 `index.html`
    한 개를 게시해 서버 해시 일치를 확인했고, 게시 후 본인 200·비소유자 403·익명 401·앱 전용
    403, 기능 검사 6개 및 데스크톱·모바일 화면을 확인했습니다. 현재 진입은 Enabled이며 인증
    필수·본인 허용 목록·SCM/FTP 기본 인증 false를 유지합니다. 합성 미리보기 배포만 완료했으며
    다른 테넌트 검사는 NOT_TESTED, 실거래·전체 ATS 준비는 미완료입니다. 아래 Azure 준비 항목은 배포 전 기록입니다.
- Azure 읽기 전용 미리보기 준비를 시작했습니다. 2026-10-01 Azure CLI 활성 구독,
    로그인 사용자 및 사용 가능한 구독 목록 조회에 성공했습니다. 정적 HTML은
    계획 수립이 가능한 상태지만, 지정 사용자 인증과 배포 파일 분리, 대상별 권한·지역·비용
    검토가 남아 있습니다. 대상은 `Azure for MCT`, 지역 우선순위는 `Korea Central`이며,
    본인 Entra 계정만 접근하도록 준비합니다. 공급자 조회상 Static Web Apps에는
    Korea Central이 없고 App Service 사이트에는 포함돼 있어 대안을 검토합니다.
    특정 SKU·할당량·비용·본인 인증 권한은 아직 확정하지 않았습니다.
    구독 기본값 변경·인프라 생성·배포는 수행하지 않았으며 전체 운영 준비 완료가 아닙니다.
- 선택한 구독에서 상속된 West Europe 제한 및 리소스 변경 시 MFA 정책을 확인했습니다.
    Advisor 도구는 테넌트 불일치로 조회에 실패했고, 위임한 무료 앱 한도 조회는 정확한
    사용량 결과를 회수하지 못해 미검증으로 남겼습니다. 다른 지역이나 유료 요금제로
    자동 전환하지 않습니다.
- 2026-10-01 사용자의 명시적 승인 후 `Azure for MCT`에 `Microsoft.Quota` 공급자를
    등록했고 `Registered` 상태를 확인했습니다. 이는 앱·리소스 그룹 생성과는 다릅니다.
    ARM 관리 권한 조회에는 전체 작업 허용 항목이 있으나 정책·MFA·디렉터리 권한은 별도입니다.
    Korea Central 일반 사용량 API는 한도 30/사용량 0을 반환했지만 F1별 수치가 아닙니다.
    SKU별 할당량은 `InvalidResourceName` 등으로 미확정입니다. 무료안의 사용 가능 여부를
    추정하지 않고 배포 계획을 초안으로 보류했습니다. 인프라 파일과 앱 리소스는 만들지 않았습니다.
- 추가 읽기 전용 확인에서 해당 구독의 Windows F1 제공 지역에 Korea Central이 포함됨을
    확인했습니다. 이는 할당량 여유 확인과는 다릅니다. 대상 테넌트를 명시한 인증으로 본인
    사용자와 기본 앱 등록 허용 정책도 확인했으며, 토큰은 출력·저장하지 않았습니다.
    실제 인증 앱 생성과 로그인 검증은 아직 하지 않았습니다. F1 할당량은 미검증 상태를 유지합니다.
- Actual engine and signal adapters, engine-specific input materialization,
    and engine-driven strategy replay remain unimplemented. Local input replay
    now exercises the data gates, but local ports and receipts do
    not constitute an executable trading system or independent certification.
- Phase 0 ADRs for point-in-time and revision semantics, source/data rights,
    Azure service selection, and Qlib/LEAN comparison metrics and tolerances.

## Approval Gates

- Requested direction (2026-10-01): automatic live orders without per-order
    confirmation, for personal use. The user approved the extension and ADR 0003
    for staged implementation, beginning with production quotes-only connectivity.
    Live activation and deployment are not approved.
- Confirmed requirements: available cash only, no additional daily buy/count
    caps, existing 1% daily-loss and 15% drawdown halts retained, and all eligible
    domestic ordinary stocks/ordinary ETFs. The 10% per-symbol limit remains.
    No fixed pilot budget or hand-picked symbol list is required by this proposal.
- Implement the approved `docs/plans/live-autotrading-plan.md` and ADR 0003
    without activating live execution or reinterpreting paper artifacts. Account,
    champion, and activation evidence remain outstanding; operational policies
    and all existing certification/paper-session gates remain unchanged.
- Complete source-specific legal review before enabling collection.
- KIS public-document review is recorded in `docs/sources/kis-market-data.md`;
    personal own-asset use and no third-party provision are documented. Operator
    terms attestation and the one-off smoke receipt are now recorded. The default
    source policy remains DRAFT; persistence and recurring collection need their
    own reviewed scope and enforcement.
- Azure synthetic-preview scaffold, F1 infrastructure deployment, dedicated
    30-day credential setup and fail-closed ingress testing were separately approved.
    Actual owner/non-owner access tests remain mandatory before report upload.
    No paid/region fallback or live-trading activation is approved.
- Promotion remains subject to explicit human approval.

## Blockers

- Earlier Azure MCP subscription discovery returned HTTP 403. On 2026-10-01,
    subsequent explicit-target checks, F1 provider validation, what-if and actual
    Korea Central F1 provisioning succeeded. Numeric quota headroom and final cost
    remain unverified, not zero. The synthetic preview is published with verified
    owner/non-owner, anonymous and app-only outcomes. The different-tenant user test
    is deferred under an explicit operator-approved exception, not marked passed.
- Collection through source-specific connectors remains gated on documented
    legal and license review. The KIS one-off smoke succeeded by user report;
    a durable, normalized real-data dataset is still unavailable.

## Next Executable Step

2026-10-02: 이번 로컬 개발 범위의 검증은 완료했습니다. 재현은 `research/README.md`의
Full Pipeline을 따릅니다. 실제 실행은 명시적으로 제외됐으며 활성화 승인이 아닙니다.
운영 전 미구현·미검증 항목과 보안 경고는 현재 연속 개발 기록을 기준으로 확인합니다.
다음 문단은 재개 전 상태 기록이며 이미 구현한 job/전체 엔진/paper 어댑터를 다시 만들지 않습니다.

The first main-push CI/CD run, OIDC exchange and synthetic-only publication are
verified by run 36865676575 at commit 3293182. Do not repeat provisioning or
credential creation. Additional CI/CD runtime maintenance, PR/manual-trigger and
failure-recovery tests require a scoped follow-up. Other ATS implementation is
resumed for local development by the subsequent continuous-task request. See
`docs/plans/data-to-paper-plan.md` for the current implementation and observed tests.

Define and approve persistence/broader-collection scope, retention enforcement
and collection policy before another real-data collection. Implement a trusted
calendar, corporate-action resolution and explicit revision/provenance mapping
from normalized KIS batches to snapshots without backdating observed availability.
The historical production smoke did not retain a replayable source fixture. New local
storage/ingestion primitives restore raw-bound batches but do not yet build snapshot manifests. Fixed
Qlib-feature/LEAN-indicator container probes pass, but full engine evaluation adapters
remain unfinished. Then build a verified real-data dataset and actual Qlib/LEAN
certification. KIS paper transport,
registry, authenticated operator services, isolated AI research and deployment
prerequisites remain unfinished. See `docs/plans/predeployment.md`.

## Known Boundaries

- The new native smoke evaluator calculates synthetic returns but is not Qlib/LEAN
    certification, a complete KRX execution model, or a production portfolio engine.
    Numerical risk checks operate on caller-supplied trusted state. The new paper
    ledger adds local transactional reservations and submission claims, not an atomic
    broker transaction or authenticated account access. Detailed new storage, calendar,
    engine and ledger limitations are recorded in `docs/plans/data-to-paper-plan.md`.
- Local replay materializes data bundles only; it does not simulate a strategy,
    calculate returns, or execute an engine. Request cutoffs must be nonempty,
    aware, unique, and increasing; no sorting or deduplication silently repairs them.
- Replay pins one snapshot, source policy, requirement tuple, and unscoped policy
    across all cutoffs. Policy activation changes and evolving snapshots require
    a separate workflow. Requirements must be provided explicitly; `()` still
    means no freshness/completeness assertions.
- Replay results are held in memory and returned only after all cutoffs pass.
    Errors preserve the first failing cutoff and cause, not earlier bundles.
    This is not a filesystem transaction or a race-proof archive snapshot.
- `result.validate_against_request(request)` checks request digest, exact cutoff
    coverage and declared provenance. It does not rerun I/O or prove record-level
    completeness/authenticity. Direct receipt construction is not verified replay.
- `data_requirements` is explicit, in-process metadata, not a signed operator
    policy. The empty tuple preserves selection-only behavior and makes no
    freshness/completeness claim. Production consumers must require the correct
    trusted requirements rather than letting strategies omit or relax them.
- Requirements inspect final admitted records, not excluded or superseded ones.
    Every matching admitted record must satisfy its declared age limit; no record
    is silently dropped to make the check pass. Required source eligibility is
    checked before I/O, even when no matching record exists.
- Age uses elapsed UTC seconds with inclusive maximum age, without trading
    calendars. Missing or future chosen timestamps fail; there is no fallback
    to observation time. `effective_at` is only an economic-age proxy where the
    source contract defines it accordingly. Unmatched sources are not age-checked.
- `build_decision_inputs` combines verified record and universe selection, with
    exclusions recorded by source/item/revision and reason. Unscoped inclusion
    is an explicit caller choice, not proof that a record is market-wide.
- `DecisionInputBundle` is an in-process receipt, not a persisted schema or
    execution capability. Model construction validates shape/timing/scope, not
    provenance completeness or bytes; use the builder for actual resolution.
    Consumers must reverify referenced payload bytes on subsequent reads.
- Bundle identity includes snapshot digest, normalized UTC cutoff, universe
    reference, effective members, applicable revision orders, unscoped policy,
    source-policy reference, data requirements, admitted records, and exclusions. It inherits the snapshot's order-sensitive
    identity; it is not an order-independent hash of the complete input archive.
- Bundle construction requires a revalidated source policy and enforces declared
    eligibility before I/O, even for records later excluded by universe scope.
    Future observations/order claims are outside that earlier cutoff's source check.
- Callers must supply a trusted operator-selected policy. Neither approval
    signatures, historical policy activation/revocation, nor collection-time rights
    are authenticated here. The policy approval date is checked against the bundle
    cutoff, not every historical record's collection timestamp.
- Bundle construction is not yet wired to engine ports and does not enforce
    retention, rate limiting, normalized-content validity, membership
    evidence authenticity, or trade authorization. Universe evidence references
    lack source IDs and are not covered by this source gate. Empty bundles do not
    authorize signals. Low-level resolver/selector methods remain policy-free.
- Direct bundle construction is not evidence the builder ran. The receipt's
    `validate_source_policy` rechecks admitted records, exclusion source IDs, and
    revision-order sources; discarded raw revisions require rebuilding from the
    original snapshot for a complete eligibility check.
- As-of selection is local and not wired into an actual engine. `RevisionOrder`
    evidence and observation timestamps are caller-supplied claims. The resolver
    verifies evidence bytes, not their interpretation or source-specific
    completeness. Errors are raised
    without partial results; persistent quarantine is not implemented.
- Use `LocalArtifactResolver.select_verified_records_as_of` when byte integrity
    is required; the original pure selector remains filesystem-free. The resolver
    verifies raw payload digests, not normalized `content_hash` values or semantic
    agreement of an order with its report. Universe resolution is a separate call.
- The artifact root must be trusted and protected from concurrent untrusted
    writes. Static link checks are not a race-proof filesystem sandbox. The
    resolver has no write/install/fetch interface, and every read rechecks bytes;
    consumers must not later read the path without verification.
- The selector returns known records, not a new universe-validated snapshot.
    It does not filter by economic effective time or check stale data/source
    permissions. Callers must retain cutoff and revision-order evidence as well
    as the snapshot identity to reproduce selection.
- Adapter wrappers validate contracts and identity before/after invocation, not
    actual engine execution, metric accuracy, or adapter side effects. Their
    request/output models are in-process interfaces, not new persisted schemas.
- Tests use synthetic doubles only; Qlib and LEAN are not installed or invoked.
    A whole-snapshot cutoff does not prevent per-bar look-ahead inside an engine.
    Engine integration of as-of selection and a numerical comparison policy
    remain open.
- Order/risk objects are receipts, not broker capabilities. Call
    `decision.validate_for_intent(intent, policy, at=trusted_now)` with the current
    operator policy; this checks binding and a half-open validity interval, not
    authenticated assessor identity or the truth of the check reports.
- Numerical position/cash/exposure/loss/kill-switch checks now exist for LIMIT
    intents using supplied state. Live state acquisition, authenticated champion
    and universe providers, holidays, tick sizes, reservations and broker wiring
    are not implemented. `SELL` means reducing an existing long position; actual
    account reconciliation remains necessary to rule out a short sale.
- Session dates only enforce ordering, not the next KRX trading session or its
    opening time. Client order IDs are metadata; atomic replay prevention,
    resource reservations, and revalidation before submission remain required.
- Promotion decisions are records, not activation capabilities. Consumers must
    resolve run inputs and call `decision.validate_evidence(policy, evaluations,
    runs)`. No lifecycle, champion pointer, policy file, or broker state is changed.
- Gate reports and human approval artifacts remain supplied attestations. No
    signature, reviewer authority, report bytes, or append-only audit persistence
    is verified here. Paper session counts, walk-forward folds, realized slippage,
    hard-risk history, and dual-engine tolerances require independent report
    verification before any activation. Hash binding is not authentication.
- Research models describe completed runs and supplied measurements; they do
    not schedule jobs, execute Qlib/LEAN, calculate metrics, or enforce a sandbox.
    Call `ExperimentRun.validate_inputs` with the actual strategy and snapshot,
    then `EvaluationResult.validate_against_run` before consuming results.
- Evaluation protocol references must pin metric definitions, costs, and split
    methodology. Artifact bytes, statistical correctness, walk-forward evidence,
    multiple-testing correction, and dual-engine tolerances are not verified yet.
    Missing deflated-Sharpe confidence is represented as unknown, not as a pass.
- Research inputs currently use the exact snapshot and runtime pinned by the
    strategy. A protocol for alternative evaluation datasets/runtimes is deferred.
- Event construction checks structure and declared timing. Snapshot resolution
    is an explicit second check, not a registry lookup or authorization boundary.
- Event availability must not exceed the selected snapshot cutoff. A later
    extraction cannot be backdated into a historical signal. Offline retrospective
    analysis needs a separately documented protocol before use in evaluations.
- Rights labels are metadata, not proof of legal approval; runtime source-policy
    enforcement, authenticated approvals, and append-only persistence remain open.
- Snapshot membership references remain unchanged. `read_universe_membership`
    resolves their exact JSON bytes and checks ID/horizon; membership selection
    requires observed-at <= cutoff and effective-from <= cutoff < effective-until
    (an absent end is open). Empty/unseen membership never authorizes an instrument.
- Membership evidence artifacts, observation timestamps, source completeness,
    and ETF approval authority remain unverified claims. Interval corrections
    cannot overwrite history; overlapping declarations are rejected, not resolved.
    Callers must supply as-known interval declarations and separately verified
    evidence; a later-known removal cannot be inserted into an earlier declaration.
- Membership and record selection are combined only in the new local builder,
    not engine replay or order risk checks. Manifest JSON is an archival container, not proof that
    the whole file existed at each earlier replay timestamp.
- Snapshot digests identify serialized snapshots, including record ordering and
    creation metadata. Order-independent content-set hashing is not implemented.
- Event confidence is not a trade decision. Rumor-influence caps and independent
    pre-trade risk enforcement remain future signal/execution work.

## Evidence

- Master plan: `docs/plans/ats-master-plan.md`
- Core governance ADR: `docs/adr/0001-governed-recursive-improvement.md`
- Proposed adapter/replay ADR: `docs/adr/0002-adapter-boundaries-and-replay-semantics.md`
- Adapter boundaries: `src/ats/ports.py`
- Adapter regression tests: `tests/unit/domain/test_research.py`
- Strategy contract: `src/ats/domain/strategy.py`
- Strategy schema: `schemas/strategy/v1.schema.json`
- Point-in-time contract: `src/ats/domain/data.py`
- Local as-of selector: `src/ats/data/asof.py`
- Local artifact resolver: `src/ats/data/artifacts.py`
- Per-decision input bundle: `src/ats/data/bundle.py`
- Runnable local workflow: `src/ats/demo.py`
- Daily-price contract/normalizer: `src/ats/domain/prices.py`, `src/ats/data/prices.py`
- Native backtest and comparison: `src/ats/backtest/native.py`, `src/ats/backtest/candidates.py`
- Numerical risk assessor: `src/ats/risk/assessor.py`
- Predeployment milestone ledger: `docs/plans/predeployment.md`
- CI configuration: `.github/workflows/quality.yml`
- Local multi-cutoff replay: `src/ats/data/replay.py`
- Availability/freshness rules: `src/ats/data/requirements.py`
- Historical universe contract: `src/ats/domain/universe.py`
- Universe artifact schema: `schemas/data/universe-membership.v1.schema.json`
- Artifact integrity and selection tests: `tests/unit/data/test_artifacts.py`
- Selection and revision tests: `tests/unit/domain/test_data.py`
- Data snapshot schema: `schemas/data/snapshot.v1.schema.json`
- Market event schema: `schemas/data/market-event.v1.schema.json`
- Event and provenance tests: `tests/unit/domain/test_data.py`
- Data schema and generator tests: `tests/contract/test_data_snapshot_schema.py`
- Research contracts: `src/ats/domain/research.py`
- Research tests: `tests/unit/domain/test_research.py` and
    `tests/contract/test_research_schema.py`
- Research schemas: `schemas/research/experiment-run.v1.schema.json` and
    `schemas/research/evaluation-result.v1.schema.json`
- Promotion contract: `src/ats/domain/governance.py`
- Promotion schema: `schemas/research/promotion-decision.v1.schema.json`
- Promotion tests reuse `tests/unit/domain/test_research.py` and
    `tests/contract/test_research_schema.py`.
- Execution contracts: `src/ats/domain/execution.py`
- Execution tests: `tests/unit/domain/test_execution.py` and
    `tests/contract/test_execution_schema.py`
- Execution schemas: `schemas/execution/order-intent.v1.schema.json` and
    `schemas/execution/risk-decision.v1.schema.json`
- Operator policies: `config/source-allowlist.yaml`, `config/risk-policy.yaml`,
    and `config/promotion-policy.yaml`
- Validation: `git diff --check`, `uv lock --check`, Ruff lint/format, Pyright,
    all four schema generator checks (ten schemas), and `uv run pytest -q`
    (463 passed, 2 skipped for Windows symlink creation privileges)
- Point-in-time validation: focused Ruff/Pyright checks, 6 unit tests, and 2
    schema contract tests (2026-09-30)
