# Continuous Data-to-Paper Development

Updated: 2026-10-02
Status: RESUMPTION DEVELOPMENT PATHS VERIFIED; OPERATIONAL ACTIVATION BLOCKED

## Authorization

2026-10-02 재개 요청은 로컬 개발·합성 시험·격리된 무료 도구 사용을 명시적으로 허용했습니다.
실제 데이터 수집/저장, 계좌/주문, OS 예약, 배포, 커밋/푸시는 이번 작업에서 실행하지 않습니다.
과거 완료 수치는 역사적 근거로 유지하며 아래 재개 완료 기준은 축소하지 않습니다.

## Resumption Acceptance Criteria

| ID | 개발 완료 기준 | 필요한 검증 |
| --- | --- | --- |
| D1 | 영속 수집 job/checkpoint, 중복/실패/재수집 | 재시작·경합·lease 만료·실패 후 재시도·완료 재사용 테스트 |
| D2 | 원문/정규화/달력/수정 manifest와 resolver/snapshot 연결 | 바이트/관측/권리/수정 계보 보존, 만료/변조/미래 데이터 거절 |
| E1 | 실제 Qlib 연구 backtest 어댑터 | 격리 실행, 고정 합성 입력/시장 가정, 실제 주문·평가자산 결과와 해시 |
| E2 | 실제 LEAN 전체 엔진 어댑터와 비교 | 같은 입력/가정의 전체 엔진 실행, 결과 비교, runtime/lock/input/output 바인딩 |
| P1 | KIS paper 계좌/주문/취소/대사 HTTP 어댑터 | 오프라인 transport의 인증·페이지·부분 체결·timeout·중복·재시작·위험 거절 |
| I1 | 데이터부터 엔진·paper 경계까지 통합 및 문서 | 합성 통합 시나리오와 현재 전체 품질 검사, 재현 명령 |

실제 계좌 인증/20개 실제 paper 세션/투자 성과 인증은 개발 검증과 분리합니다.
구성요소 probe만으로 E1/E2를 완료 처리하지 않습니다. 막힌 항목과 독립적인 개발은 계속합니다.

## Resumption Results

2026-10-02 요청의 D1/D2/E1/E2/P1/I1 로컬 개발 경로를 구현하고 현재 코드로 검증했습니다.
지원하는 합성 단일 종목 TREND/다음 시가 모델의 개발 완료이며 전체 ATS 운영·일반 전략 인증은 아닙니다.
아래 2026-10-01 기록은 역사적 checkpoint입니다. 새 결과와 혼합하거나 합산하지 않습니다.

| ID | 현재 결과 | 검증 근거 |
| --- | --- | --- |
| D1 | 구현·검증 | SQLite job/lease/checkpoint, 재시작 성공 재사용, 동시 claim, lease fencing, 실패 재시도/예산 |
| D2 | 구현·검증 | 불변 이름 manifest, KIS raw digest 유지, 정규화/달력 archive, 수정 순서 근거, snapshot 및 StoredArtifactResolver 연결 |
| E1 | 실제 전체 backtest 통과 | Qlib 0.9.7 SimulatorExecutor, 비용·현금·체결·평가자산, EvaluationPort 연결 |
| E2 | 실제 전체 engine 통과 | LEAN Engine.Run 2.5.18127, FileSystemDataFeed/BacktestingBrokerage, 같은 입력·주문·평가자산 |
| P1 | 오프라인 HTTP 구현·검증 | paper OAuth/잔고/현금 매수가능/현금 LIMIT/취소/일별 대사, 장부 claim/미관리 주문 차단/복구 승인 |
| I1 | 통합·반복 실행 통과 | ats.pipeline_demo: 수집 job 5개 -> snapshot -> 실제 엔진 2회 -> mock paper ACCEPTED |

전체 `pytest -q`: **651 passed, 2 skipped**. 건너뜀은 기존 Windows 심볼릭 링크 생성 권한
제한이며 권한 상승하지 않았습니다. `ruff check .`, `ruff format --check .` (73 files),
strict `pyright`, `uv lock --check`, 기존 4종 schema `--check` 통과.

같은 합성 사례를 두 엔진에서 실제 실행: 2026-09-23 BUY 10 @ 100, 비용 1;
2026-09-24 SELL 10 @ 100, 비용 3. 평가자산 `[100000, 100000, 99899, 99996, 99996]`.
서로 같은 metrics와 반복 실행 metrics를 확인했습니다. 체결·가격·비용·현금 보존은 호스트에서
별도로 검사합니다. 합성 OOS 필드 수치는 고정 전략/no-training/cash-benchmark 프로토콜 값일 뿐
실제 표본 외 투자 성과, 독립 심사 또는 promotion 근거가 아닙니다.

최종 통합 근거: `.local/pipeline-20261002-verified/report.json`, SHA-256
`4c8e19445fb2fc4fddecea201964ebd2893125eac9bcc7d7ec5cf0a78e6a3c91`.
동일 디렉터리에 SQLite, Qlib/LEAN EvaluationOutput, content-addressed 입력·결과 artifacts가 있습니다.
Git 제외 산출물이므로 다른 환경에는 자동 복제되지 않습니다. 새 실행의 UUID/시각/snapshot 해시는
달라지며 수치 결과 재현성과 영수증 바이트 동일성을 혼동하지 않습니다.

최종 공통 runtime image:
`sha256:2887d452a163337c3ed91b426612f3204982550473e202d21cbc70276e6bd172`.
두 engine은 같은 불변 StrategySpec/container 계약을 사용합니다. 입력 바이트·worker 소스·lock·
image·strategy·snapshot·output 해시를 확인하며 실패/불일치에 성공 영수증을 쓰지 않습니다.

런타임은 로컬 Docker socket 고정, `--pull never`, network none, 호스트 mount 없음,
UID 65532, read-only root, capability 없음, no-new-privileges, CPU 1, 메모리 2GB,
PID 128, tmpfs 256MB, 내부 120초/호스트 150초 제한입니다. 임의 생성 코드/전략 path는 받지 않습니다.

검증한 추가 경계: 미래/만료/변조 데이터 거절, 수정본의 관측 시각 보존, 원문 중복 재사용,
동시 claim, HTTP redirect/중복 JSON/과대 응답/페이지 순환 거절, 토큰 조기 만료,
주문 timeout/401/불완전 ACK의 UNKNOWN 유지, 명시적 거절의 REJECTED, 취소 ACK 후 예약 유지,
부분 체결 대사, 단일 전송 소비, 재시작 후 재전송 차단, 미관리 broker 주문 차단,
승인 콜백 없는 복구 거절, kill/1% 손실/15% 낙폭 halt 영속화.

실제 데이터 수집/저장, 증권사 접속·주문, OS 예약, 배포, 커밋/푸시, 비밀 교체는 실행하지 않았습니다.
실제 KIS 입력을 받은 것이 아니라 API 형식의 합성 응답과 MockTransport만 사용했습니다.
문서 진단·로컬 링크·`git diff --check`도 통과했습니다. 테스트 컨테이너가 남지 않았음을
확인하고 이번에 시작한 Docker Desktop을 정지 상태로 복원했습니다. 로컬 image/캐시는 유지했습니다.

## Current Limits and Operational Gates

1. **시장/전략 지원:** 검증된 engine adapter는 합성 단일 종목 TREND, 고정 수량,
  이전 종가 신호와 다음 09:00 시가 체결, 명시적 비용·슬리피지 0입니다. Qlib float32로
  정확히 표현되지 않는 가격, 비정상 달력/크기, 30% 가격제한 경계는 거절합니다. 실제 KRX
  거래일·호가단위·기업행동·상장폐지·거래정지·체결 대기/부분 체결 모델과 다종목 최적화·
  일반 모델 학습/OOS 스트레스 인증은 남았습니다. 현재 통합 성공으로 대신하지 않습니다.
2. **데이터/보존:** source/storage permit은 신뢰된 caller의 선언입니다. 실자료 권리·달력·
  과거 전체 적격 universe 인증은 별도이며 기존 KOSPI_200/ETF_ALLOWLIST 계약은 그대로입니다.
  raw/batch write와 최종 job/manifest publish는 별도 단계이고 orphan은 보존 기한으로 제거합니다.
  manifest는 payload purge 때 cascade되지만 작업 metadata, 외부 복제본/백업/평가 산출물 보존과
  전역 tombstone은 별도 운영 설계가 필요합니다. 경로 검사는 악의적인 파일 교체 방지 sandbox가 아닙니다.
3. **broker/위험:** 네트워크 기본 비활성, 모의 host 고정. 실제 인증·응답 호환성은 mock으로
  입증하지 않았습니다. HTTP client/장부는 신뢰된 실행 서비스용이며 연구 container에는 비밀이 없습니다.
  계좌 수치를 대조하지만 가격/챔피언/적격성/현금흐름 조정 손실은 독립 RiskState 공급자가 필요합니다.
  다종목 예약은 보수적으로 합산합니다. 복구 authorizer는 외부 운영자 인증 구현 지점이며 기본 거절입니다.
  broker 복구는 RISK_STOP을 지울 수 없고, 위험 halt 해제 서비스·운영 UI·경보는 별도입니다.
4. **취소:** 공식 정정취소가능조회 예제에는 실전 ID만 확인돼 호출하지 않습니다. paper 일별 주문
  잔량을 재확인해 원주문을 취소하고 확정 대사 전까지 예약합니다. 실패한 취소를 자동 반복하지 않습니다.
  실제 paper 환경의 필드·취소 동작은 운영 전에 확인해야 합니다.
5. **의존성 보안:** LEAN 2.5.18127의 고정 전이 의존성에서 NuGet 경고가 확인됐습니다.
  DotNetZip 1.16.0 (GHSA-xhg6-9j5j-w4vf), System.Drawing.Common 4.7.0 (GHSA-rxg9-xrhp-64gj),
  System.Net.Http.WinHttpHandler 4.4.0 (GHSA-6xh7-4v2w-36q6),
  System.Private.ServiceModel/System.ServiceModel.Primitives 4.4.0
  (GHSA-jc8g-xhw5-6x46, GHSA-p9wx-v264-q34p). 경고를 비활성화하지 않았습니다.
  고정 합성 CSV/네트워크 차단 실행을 취약점 해결로 표현하지 않습니다. 운영 사용에는 패치/업그레이드와
  호환성·취약점 검토가 필요합니다. Qlib upstream gym 유지보수 경고도 남습니다. 호스트 환경은 그대로입니다.
6. **활성화:** 실제 수집·계좌/모의주문·예약 실행은 이번에 명시적으로 제외됐습니다. 사람의 전략 승인,
  실제 API 연동 검증, 최소 20개 실제 paper 세션, 운영 서비스/배포 검증이 남습니다. 실거래·추가 비용·
  권한·비밀·배포·Git 게시에는 이번 개발 승인을 재사용하지 않습니다.

실행 명령은 [연구 안내](../../research/README.md)를 따릅니다. 아래 내용은 과거 상태입니다.

## Prior Authorization Record

사용자는 데이터 수집·저장 -> 검증 -> Qlib·LEAN -> 모의주문 개발을 하나의 연속 작업으로
진행하고, 범위 내 구현·테스트·수정에는 단계별 재질문을 하지 않도록 요청했습니다.
기존 나머지 개발 보류는 이 개발 범위에 한해 해제합니다.

최초 운영 질문에는 선택값 없이 사용자가 부재하니 자율적으로 판단하라는 응답만 있었습니다.
이는 데이터 이용 약관 확인, 수집·저장 권한, 주문 전송 또는 커밋·푸시의 명시적 승인이 아닙니다.
다음 보수적 실행 경계를 적용하고 미결 항목을 승인으로 바꾸지 않습니다.

| 항목 | 현재 실행 경계 |
| --- | --- |
| 개발 | 기존 KIS 일봉 경로를 중심으로 로컬 구현·합성 입력·오프라인 검사 수행 |
| 실제 수집·저장 | 미승인. 기존 단발 조회 정책은 확장하지 않고 기본 소스 정책은 DRAFT 유지 |
| 자동 예약 | 스케줄 계산·재시작 동작을 개발하되 OS 작업 등록과 상주 수집은 실행하지 않음 |
| 엔진 | Qlib 피처/LEAN 지표를 무료 로컬 컨테이너에서 합성 입력으로 시험. 전체 평가·인증은 미완료 |
| 의존성 | 별도 Docker 이미지에 버전·해시 고정. 프로젝트 Python/uv.lock과 호스트 SDK는 변경하지 않음 |
| 모의주문 | 상태 관리·어댑터·실패 검증 개발. 실제 모의계좌 조회·주문 전송은 비활성 |
| Git | 파일 수정과 검증만. 이번 작업의 새 브랜치·커밋·푸시·PR·병합은 하지 않음 |
| 클라우드 | 기존 합성 사이트·OIDC·배포 워크플로를 변경하거나 재배포하지 않음 |
| 범위 밖 | 실거래, 추가 비용·권한, 비밀 생성·교체, 승인되지 않은 소스 수집 |

비밀값이나 실제 계좌 식별자를 문서·코드·테스트·로그에 넣지 않습니다.
수집 정책, 전략 승인, 위험 검사를 건너뛰거나 미승인 상태를 기본 허용으로 바꾸지 않습니다.
미결 외부 실행에 의존하지 않는 구현은 계속합니다. 실제 API/엔진/20개 모의 세션의 증거는
mock이나 합성 결과로 대체하지 않습니다.

## Delivery Sequence

1. 저장: 로컬 원문과 수집 이력, 원자적 기록, 해시 검증, 중복 처리, 보존 만료 집행.
2. 수집·검증: 명시적 요청 계획, 승인·속도·시점·신선도·자료 완전성 경계, 재시작/재수집.
3. 엔진: 기존 평가 계약을 지키는 Qlib·LEAN 입력/출력 연결과 제한된 실행 경로.
4. 모의주문: 영속 주문 상태, 중복 방지, 예약·조회·대사·실패 시 중단 경계.
5. 통합: 오프라인 시나리오와 품질 검사, 실제 실행이 필요한 잔여 조건과 재시작 자료 기록.

먼저 필요한 최소 경로를 읽고 작은 변경 직후 해당 테스트를 실행합니다.
기존 계약·유틸리티·테스트를 재사용하며 불필요한 일반 프레임워크를 만들지 않습니다.
기존 소스·전략·snapshot의 해시와 의미를 뒤늦게 변경하지 않습니다.

## Historical Completion Evidence

| 단계 | 상태 | 관측 근거 |
| --- | --- | --- |
| 범위·실행 경계 | 기록 완료 | 운영 질문에 구체적 선택 없음; 외부 실행은 미승인 유지 |
| 로컬 저장·보존 | 기본 경계 구현·검증 | 별도 저장 허가, SQLite 원문/영수증 원자 기록, 크기·해시·만료 검사; 저장 테스트 12개 |
| 수집·검증 연결 | 계획·ingestion·batch 보존 경계 구현·검증 | 100일 요청 분할·예산, 예정 시각 계산, 제공 달력 대조, 기업행동 거절, batch 재시작 복원·원문 재대조; 테스트 10개 |
| Qlib·LEAN | 실제 구성요소 smoke 통과; 전체 어댑터 미완료 | Qlib 0.9.7 / LEAN Indicators 2.5.18127, 이동평균·신호 5행 일치; 전체 엔진 평가 0회 |
| 모의주문 경계 | 장부 구현·검증; 실제 어댑터 미완료 | SQLite 예약·claim·대사, 경합/중복/재시작/미확정 중단/미전송 만료; 실행 계약 포함 테스트 103개 |
| 통합 검사 | 현재 로컬 검사 통과 | 전체 630 passed, 2 skipped; 린트·포맷·strict types·4종 스키마·기본 lock 검사 통과 |

개발 완료와 운영 준비 완료는 구분합니다. 최소 20개 실제 모의운영 세션, 데이터 약관·보존
조건, 신뢰할 거래 달력·기업행동·과거 투자 대상, 불변 전략의 인간 승인 및 실제 계좌 바인딩이
확보되지 않으면 해당 실제 실행은 차단합니다. 누락 자료를 추정해 인증 성공으로 표시하지 않습니다.

## Historical Observed Evidence

- `uv run --frozen pytest -q`: **630 passed, 2 skipped**. 건너뛴 것은 기존 Windows
  심볼릭 링크 생성 권한 테스트 2개이며 권한 상승은 하지 않았습니다. 테스트 수를 합산한 추정이
  아니라 변경 후 전체 실행 결과입니다. 본 에이전트는 커밋·푸시하지 않았으며 원격 CI는 확인하지 않았습니다.
- `ruff check .`, `ruff format --check .` (67 files), `pyright`, `uv lock --check`,
  전략·데이터·연구·실행 schema generator의 `--check`가 모두 통과했습니다.
- `uv run --frozen python -m ats.demo --output .local/data-to-paper-20261001 --exercise-data-to-paper`:
  합성 원문 5개 저장/읽기, 만료 행 5개 제거, 장부 재시작 후 UNKNOWN 재전송 차단.
  이 smoke는 기존 native 평가와 저장·장부 경계를 한 실행에서 검사하지만 실제 수집에서 엔진,
  계좌까지 연결된 운영 파이프라인은 아닙니다. 기본 데모/대시보드 산출 형식은 바꾸지 않았습니다.
- 2026-10-01 **14:33:59 UTC** 실제 Qlib/LEAN 구성요소 비교: 5개 이동평균
  `[100, 105, 100, 85, 90]`, 신호 `[false, true, false, false, true]` 일치.
  `certified=false`, `full_engine_evaluations=0`, 실제 계좌/주문 요청 0회.
  영수증: `.local/engine-probes/20261001-bounded.json`, SHA-256
  `56292a0784a90dcca9b7e4c0dc4b753cdc624ea7d0af19e0b12aa7a0dc2c5cc7`.
  Git 제외 로컬 증거이므로 다른 컴퓨터에 자동으로 전달되지 않습니다.
- 실행 이미지 Qlib:
  `sha256:609e42ed67aca2430fbae2aa5d3b588f90c2e8c3d4f63bf046529c85474d74cd`;
  LEAN:
  `sha256:eafe7252562ecd805abf6bdfe1b0242184032bacb700803632ff6a64e3d892d2`.
  컨테이너 내 dependency lock과 저장소 lock의 해시가 일치했습니다.
- 런타임: 네트워크 없음, 호스트 mount 없음, non-root, read-only root,
  capability 제거, no-new-privileges, CPU 1, 메모리 1GB, PID 64, 임시 공간 256MB,
  프로세스 제한 60초. 고정된 합성 검사만 허용하며 임의 생성 전략의 sandbox 인증은 아닙니다.
- 최초 Docker daemon은 정지 상태였습니다. 기존 Docker Desktop을 일반 권한으로 시작했고
  Linux engine 29.8.1을 확인했습니다. 이미지/패키지 다운로드와 빌드는 네트워크를 사용했고,
  실제 피처 계산 시에는 차단했습니다. 전역 패키지 설치, 유료 LEAN CLI, 실제 시장 데이터 다운로드는 없습니다.
- Docker 이미지와 빌드 캐시는 로컬에 남습니다. 실행 컨테이너는 `--rm`으로 제거됐습니다.
  다른 실행 컨테이너가 없음을 확인하고 Docker Desktop은 원래 정지 상태로 복원했습니다.
  재현 명령과 검사 한계는 [연구 구성요소 안내](../../research/README.md)를 확인합니다.
- 마지막 Git 확인에서 동시 작업으로 생성된 `3817b42` (2026-10-01 23:36:52 KST)가
  구현 파일과 사용자 `.vscode/settings.json`을 포함한 것으로 관측됐습니다. 본 에이전트가
  만든 커밋이 아니며 되돌리거나 다시 커밋하지 않았습니다. 이후 문서·테스트 포맷 수정은
  working tree에 남습니다. 해당 커밋의 push/원격 CI/게시 여부는 여기서 확인하지 않았습니다.

## Historical Remaining Implementation and Gates

1. **저장/수집:** 원문에 묶인 정규화 결과·제공 달력은 `archive_kis_batch`로 같은 기한 안에
  보존하고 `restore_kis_batch`에서 원문을 다시 정규화해 대조합니다. 원문과 batch archive는
  별도 트랜잭션이며 검색/발견용 영속 manifest와 수집 job/checkpoint, 기존 artifact resolver와
  snapshot 작성 연결, 실제 스케줄러는 남았습니다. 호출자는 archive 영수증을 보관해야 합니다.
   `pending_daily_run`은 시각 계산이지 작업 이력이나 OS 예약 실행이 아닙니다.
2. **데이터 신뢰:** 달력은 caller가 제공하며 KRX 원본으로 인증하지 않았습니다.
   기업행동은 해석·조정하지 않고 review로 거절합니다. 실제 전체 적격 종목/ETF 과거 목록도 없습니다.
   실제 수집·저장 권리, 범위, 보존 조건, 운영 주기가 확정돼야 실자료를 사용합니다.
3. **보존 한계:** 원문·batch와 각 receipt를 SQLite에 저장합니다. 외부 복제본/백업/파생 자료 삭제,
   상주 purge, 암호화, 인증된 정책 registry는 없습니다. `secure_delete`는 SSD/백업 삭제 보증이 아닙니다.
   동일 행이 남아 있을 때 중복이 보존 기간을 늘리거나 새 관측으로 relabel되는 것은 거절하지만,
   purge 후 동일 원문의 재수집을 판별하는 전역 tombstone은 없습니다. 기존 정책에 대한 허가와
   시각은 신뢰된 caller가 제공해야 하며 정적 경로 검사는 악의적인 파일 교체 경쟁을 막는 sandbox가 아닙니다.
4. **엔진:** 실제 Qlib 연구 backtest, LEAN 전체 엔진, KRX 달력·기업행동·체결·비용 모델,
   다종목/포트폴리오, OOS/스트레스, `EvaluationRequest/Output` 어댑터 연결이 남았습니다.
   지표 라이브러리 성공은 전체 LEAN 실행이나 전략의 독립 인증으로 승격되지 않습니다.
5. **모의주문:** 인증된 계좌/잔고/체결 조회와 KIS paper HTTP 어댑터, 단일 전송 주체,
   실제 대사·복구·취소 전송은 없습니다. 장부는 trusted state로 위험을 다시 검사하고 예약을
   원자적으로 기록할 뿐 외부 주문을 보내지 않습니다. 외부 예약값이 있으면 이중 계산 대신 거절하며
   여러 종목의 매수 예약은 기존 위험 상태 계약에 맞춰 보수적으로 합산합니다.
   filled 이후에는 더 새 계좌 상태를 요구하지만 그 상태의 진실성을 인증하지는 않습니다.
   UNKNOWN halt의 인증된 해제, 지속 손실 중단 서비스, 변조 방지 감사 저장소도 남았습니다.
6. **운영 완료:** 별도 실제 실행 승인, 불변 챔피언의 인간 승인과 최소 20개 실제 모의 세션이
   필요합니다. 현재는 전체 요청 완료가 아닌 검증된 개발 체크포인트입니다. 위 미구현 항목을
   단순 승인 대기로만 표현하거나 자동으로 완료 처리하지 않습니다.

## References

- [현재 상태](status.md)
- [인수인계](project-handoff.md)
- [전체 계획](ats-master-plan.md)
- [KIS 경계와 권리](../sources/kis-market-data.md)
- [실거래 확장과 별도 활성화 조건](live-autotrading-plan.md)
