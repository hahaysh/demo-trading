# Isolated Component Probes

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

- 입력은 두 프로그램 내부의 동일한 합성 상수입니다. 저장된 실제 KIS batch나
  point-in-time snapshot을 이 프로그램들에 연결하는 어댑터는 아직 없습니다.
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