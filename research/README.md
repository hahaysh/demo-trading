# Isolated Engine Development

## RSI Workflow

A-E 전체 개발은 [현재 계획](../docs/plans/rsi-development-plan.md)의 행별 상태를 확인합니다.
아래는 합성 수집·실제 학습·실제 엔진·운영 장부를 연결하는 검증 명령이며 실제 수집/주문을 실행하지 않습니다.

```powershell
./research/engines/build.ps1 -SecurityRebuild
docker build -t ats-rsi:dev research/rsi
$engine = docker image inspect ats-backtest-engines:dev --format '{{.Id}}'
$model = docker image inspect ats-rsi:dev --format '{{.Id}}'
$leanLock = 'research/engines/security-probe/locks/runner.lock.json'
uv run --frozen python -m ats.rsi_workflow --root .local/my-rsi --engine-image $engine --proposer-image $model --signal-model LINEAR_SEARCH --case research/rsi/market-case.json --lean-lock $leanLock --cycles 4
uv run --frozen python -m ats.research_validation --root .local/my-rsi --market research/rsi/holdout-case.json --sequence 1 --lean-lock $leanLock
uv run --frozen python -m ats.operator --root .local/my-rsi --research .local/my-rsi/research.sqlite3 --port 8768
```

같은 root와 동일 runtime/model/case는 재개할 수 있습니다. 코드/시장 자료가 바뀌면 기존 campaign을
수정하지 말고 새 버전을 만들어야 합니다. holdout 시작 후에는 같은 campaign의 추가 탐색이 거절됩니다.
학습 점수는 research_trials, 최종 검증은 research_validation에 별도로 저장합니다.
LINEAR_SEARCH는 허용된 4개 파라미터에서 Ridge/ElasticNet을 선택하며 부모의 변경 수 예산을 유지합니다.
분석 manifest와 원문이 철회·만료됐으면 연구·검증 경계에서 재사용을 거절합니다.
OOS 하한·DSR·fold 내부 block bootstrap은 별도 진단입니다. 짧은 표본은 근거 부족으로 남기고
기본 검증 정책은 DRAFT라 리뷰 게이트도 통과하지 않습니다. 합성 달력은 실제 KRX 달력이 아닙니다.

`-SecurityRebuild`는 공식 LEAN commit을 해시로 고정해 Compression/Common/Engine을
ProDotNetZip 1.19.0으로 재빌드합니다. 네 프로젝트의 lock, 원본 라이선스와 변경 표식을 유지하고
출력의 원래 DotNetZip 참조 부재 및 정상/경로 탈출 ZIP 회귀를 검사합니다. 다른 취약점의 부재나
전체 운영 보안을 인증하지 않습니다. 옵션 없는 과거 빌드·이미지는 DotNetZip 경고가 남으므로 구분합니다.
재빌드 runtime에는 반드시 위 `$leanLock`을 사용합니다. 이름이 같은 태그도 실제 image ID를 다시 확인합니다.

운영 화면은 localhost 전용 합성 인증 서버입니다. `/fixture/session`은 공개된 시험용 키의 임시
토큰을 반환하고 `/fixture/sign`은 시험 명령을 바인딩합니다. 실제 비밀이나 계좌를 넣지 마세요.
이 모드는 인터넷에 게시하거나 운영 인증으로 사용하면 안 됩니다. 실제 app factory는 별도의
trusted issuer/public key/identity 목록과 promotion provider 및 독립 `SignedPromotionVerifier`를 요구합니다.
서명·현재 run/evaluation 바이트·계좌·관측 기간을 검증하며, 실제 관측자 신뢰 설정이나 실제 20세션을
이 데모가 대신하지 않습니다. 화면의 현재 수집 지연은 과거 성공 기록과 별도로 표시합니다.
화면의 검토 수락은 전략 승격이 아니며, 선택된 전략도 broker를 자동 활성화하지 않습니다.
미해결 paper 시험 권한은 [제안 ADR](../docs/adr/0004-paper-trial-authorization.md)에 있으며 미승인입니다.

기존 단일 종목/지표 probe의 과거 증거를 현재 RSI 전체 완료로 해석하지 않습니다.

`evaluate_shared_portfolio`는 두 엔진이 하나의 현금 계정에서 다종목을 처리하는 별도 경로입니다.
명시적 슬리피지·tick 반올림·장 시작 capacity와 합성 raw-target 분할/순배당을 지원합니다.
capacity는 목표의 일부 실행 모델이지 실제 broker 부분체결 통지 시험이 아닙니다. 결제 세션 제약은
`HOST_PREFLIGHT_ONLY`입니다. 자동 팩터의 기본 RAW는 기업행동 입력을 거절하며 명시적으로
`FactorComposition(price_adjustment="TOTAL_RETURN")`을 선택할 수 있습니다. 연구·학습에서는
부모의 고정 `signal.total_return_adjustment=1`을 요구하며 원시 체결 가격을 변경하지 않습니다.
`serve_information`은 중단 신호와 수명/회차 예산을 가진 임베딩용 로컬 서비스 함수입니다.
OS 예약 등록이나 실제 소스 활성화는 하지 않습니다. 데이터 전용 backup/restore는 최신 삭제 장부를
요구하며, 운영·주문 장부를 과거로 되돌리는 범용 복구 기능이 아닙니다.

`withdraw_item`은 원문/분석 파생물을 철회하고 해당 소스의 신규 수집을 격리합니다. 해시가 다른
바이트 재유입도 차단하며 백업 복구에 최신 격리를 병합합니다. 자동 해제는 하지 않습니다.
DART 거래소 공시는 `exchange-filings` scope와 `exchange_only=True`를 함께 지정하며,
KIND 직접 API로 표시하지 않습니다. KRX 기본정보는 고정된 `stk/ksq/knx_isu_base_info`의
세 서비스만 허용하며 실제 이용·보존 허가나 역사적 투자 적격성을 자동 인정하지 않습니다.

## Full Pipeline

2026-10-02에 전체 엔진 연결을 추가했습니다. 기존 지표 probe와 별개로 Qlib의 실제
`SimulatorExecutor`와 LEAN의 실제 `Engine.Run`/파일 피드/백테스트 브로커를 호출합니다.
아래 명령은 합성 KIS 형식 자료의 job·manifest·snapshot을 만들고 두 엔진의 기존
`EvaluationRequest/Output` 어댑터를 실행한 뒤 MockTransport 모의주문을 검사합니다.
실제 데이터·계좌·주문·배포·전략 승격은 수행하지 않습니다.

준비물: Python 3.11/프로젝트 uv 환경, PowerShell 7, 실행 중인 로컬 Linux Docker engine.
의존성 빌드에는 공개 패키지 다운로드가 필요하며 전략 실행 container는 네트워크가 없습니다.
저장소 루트에서 새 출력 디렉터리를 지정합니다.

```powershell
./research/engines/build.ps1
$image = docker image inspect ats-backtest-engines:dev --format '{{.Id}}'
uv run --frozen python -m ats.pipeline_demo --output .local/my-full-pipeline --image $image
```

생성된 `report.json`, 두 evaluation JSON, SQLite, `evaluations/sha256/`의 입력·결과를
함께 확인합니다. worker 코드/lock/input/image/strategy/snapshot/output을 바인딩하고,
엔진 체결·비용·평가자산을 호스트에서 독립 계산으로 대조합니다. 공통 image를 두 엔진에
사용해 기존 불변 전략의 runtime 계약을 유지합니다. 영수증은 서명된 인증서가 아닙니다.

지원 사례는 합성 단일 종목 TREND, 고정 수량, 전일 종가 신호·다음 시가 체결입니다.
수수료 0.1%, 매도 총비용 0.3%, 슬리피지 0, 현금 benchmark, 합성 달력을 명시합니다.
관측은 각 세션 다음 날 06:00 KST로 두며 과거 가격을 과거에 알았다고 소급하지 않습니다.
이 테스트의 OOS 필드는 고정 전략/no-training 합성 프로토콜 수치입니다. 실자료 성과 인증,
다종목 학습/최적화 또는 실제 KRX 체결 모델 완성을 뜻하지 않습니다.

런타임은 host mount/네트워크 없이 UID 65532, read-only, capability 제거, CPU 1,
메모리 2GB, PID 128, tmpfs 256MB, 내부 120초/호스트 150초로 제한합니다. adapter는 로컬
Docker socket과 image ID만 사용하고 자동 pull을 금지합니다. 빌드 중간 태그는 image ID에서
만들고 일치를 확인합니다. image/캐시는 로컬에 남습니다.

**보안 경고:** LEAN 전이 의존성에 NuGet high/critical 취약점 경고가 있습니다.
네트워크 차단 합성 개발 실행이 이를 해결하지 않으며 이 이미지를 운영에 사용하지 마세요.
상세 패키지·한계는 [개발 기록](../docs/plans/data-to-paper-plan.md)의 Current Limits에 있습니다.
기존 CI에 무거운 engine image 빌드를 자동 추가하지 않았고 cloud에 게시하지 않았습니다.

## Component Probes

실제 Qlib 피처와 LEAN 지표 라이브러리를 고정된 합성 일봉 5개로 호출하는 로컬 검사입니다.
Qlib 연구 backtest나 LEAN 전체 엔진, KRX 체결 모델, 표본 외 평가, 전략 인증은 아닙니다.
`EvaluationResult`·승격 기록을 만들지 않으며 실제 시세나 계좌·키를 입력받지 않습니다.

## Run

준비물: 실행 중인 Linux Docker engine과 PowerShell 7. 처음 이미지를 만드는 동안에는
공개 이미지/PyPI/NuGet 다운로드 네트워크와 로컬 디스크 공간이 필요합니다. 빌드 이후 실제
계산 컨테이너는 네트워크·호스트 mount 없이 실행합니다. 프로젝트의 Python 환경은 바꾸지 않습니다.

저장소 루트에서 실행합니다. 기존 영수증은 덮어쓰지 않으므로 새 출력 파일을 지정합니다.

```powershell
docker build --tag ats-qlib-smoke:0.9.7 research/qlib
docker build --target probe --tag ats-lean-indicator-smoke:2.5.18127 research/lean
./research/check-probes.ps1 -OutputFile .local/engine-probes/my-new-result.json
```

검사기는 태그를 local image ID로 해석한 뒤 그 ID를 실행하고, 실제 컨테이너의 의존성 lock
해시와 저장소 lock을 비교합니다. 5개 행·신호·버전·비인증 표시가 일치해야 영수증을 씁니다.
각 컨테이너는 non-root, network none, read-only root, capability 없음,
no-new-privileges, CPU 1, 메모리 1GB, PID 64, 임시 공간 256MB, 시간 60초로 제한합니다.
영수증의 선언은 외부 서명/attestation이 아니며 로컬 Docker daemon·이미지·검사 코드가 신뢰된다는
전제입니다. 임의 프로그램에 대한 보안 인증, 브로커 실행 환경 또는 VM 수준 격리는 아닙니다.

`qlib/requirements.lock`은 `pyqlib==0.9.7`, `numpy==1.26.4`, `pandas==2.2.3`에서
해시를 포함해 생성했습니다. LEAN은 `QuantConnect.Indicators==2.5.18127`과 NuGet lock을
사용합니다. 기본 ATS의 `uv.lock`/CI 의존성에는 넣지 않았습니다. 이 검사는 기존 CI에 자동
추가하지 않았으며, 이번 로컬 이미지들을 클라우드에 게시하지 않았습니다.

의도적인 의존성 변경 시에만 lock을 다시 생성하고 diff·실제 실행을 검토합니다.

```powershell
uv pip compile research/qlib/requirements.in --python-version 3.11 --python-platform x86_64-unknown-linux-gnu --generate-hashes --no-annotate --no-header --output-file research/qlib/requirements.lock
docker build --target lock --output type=local,dest=research/lean research/lean
```

## Limits

- 이 과거 probe의 입력은 내부 합성 상수입니다. 위 Full Pipeline의 snapshot 연결과 구분합니다.
- Qlib는 합성 binary provider의 close와 2일 평균만 읽습니다. 실제 KRX 가격 정밀도,
  조정주가·기업행동·warmup·수수료·거래 달력의 올바름을 입증하지 않습니다.
- LEAN은 무료 오픈소스 지표 구성요소의 `SimpleMovingAverage`를 호출합니다.
  LEAN CLI/계정/유료 조직을 사용하지 않으며 전체 engine/launcher도 실행하지 않습니다.
- 두 프로그램의 평균 `[100, 105, 100, 85, 90]`과 신호가 같아도 엔진별 포트폴리오
  수익률·OOS 통계·체결이 같다는 증거는 아닙니다.
- 로컬 이미지/빌드 캐시는 남고, 실행 컨테이너는 제거됩니다. 다른 사용자 컨테이너나
  이미지를 `prune`하는 정리는 하지 않습니다.

현재 실행 증거와 남은 개발은 [연속 개발 계획](../docs/plans/data-to-paper-plan.md)에 있습니다.

## References

- [Qlib 공식 안내](https://github.com/microsoft/qlib)
- [Qlib 0.9.7 패키지](https://pypi.org/project/pyqlib/0.9.7/)
- [LEAN 소스 빌드 안내](https://github.com/QuantConnect/Lean/blob/master/readme.md)
- [LEAN 지표 패키지](https://www.nuget.org/packages/QuantConnect.Indicators/2.5.18127)
