# Continuous RSI ATS Development

Updated: 2026-10-02
Status: IN PROGRESS; A-E NOT COMPLETE

## Authorization

매일 시세·공시·뉴스·승인된 공개 루머를 수집하고, 연구 결과를 다음 후보 제안에 반영하는
RSI ATS가 목표입니다. 이전 합성 data-to-paper 통합은 출발점이며 이 계획의 완료가 아닙니다.
사용자는 A-E 전체에 대한 로컬 구현·합성 시험·격리된 무료 도구 준비를 승인했습니다.
실제 정보 수집/저장, 계좌/주문, OS 예약 등록, 배포, 커밋/푸시, 유료 API, 권한 확대,
비밀 교체·생성, 실거래는 금지합니다. 기본 운영 정책을 승인 상태로 바꾸지 않습니다.

기존 사용자 변경과 불변 artifact를 보존합니다. 연구는 정책/승인/평가자/브로커 코드를
변경하지 않으며, 인간 승인 없이 champion을 바꾸지 않습니다. 현금·국내 일반 주식/ETF·
장 마감/다음 거래일과 종목 10%·일일 손실 1%·낙폭 15% 중단 경계를 유지합니다.

## Acceptance Matrix

| ID | 완료 대상 | 필요한 실행 근거 | 현재 |
| --- | --- | --- | --- |
| A1 | DART 목록/원문 수집·정정 provenance | 문서 기반 HTTP fixture, 페이지·범위·중복·오류·예산·키 redaction | 목록/ZIP/XML 경로 검증; 정정 원공시 연결은 일부 |
| A2 | KIND/KRX 공개 정보·뉴스 API/RSS·승인 공개 채널 | 제공자별 검증된 규약/권리 범위, 비활성 기본값, 원문/수정/삭제 처리 | Naver/RSS/Atom/공개 export·KRX 기본정보 구현; KIND/다른 서비스·삭제 처리 남음 |
| A3 | 정기·증분 수집 orchestration | watermark/중첩 기간/갭·재시도·경합·재시작·상태·보존 만료 | 영속 스케줄·lease·범위/gap·복구 구현; 운영 예약 미실행 |
| B1 | 시점 보존·기업/종목 연결·중복/재전파 | 정확한 시간과 날짜 정밀도 구분, 과거 별칭, 근거 digest, 누출 음성시험 | 로컬 규칙/시점 검증 구현; 실제 별칭 원천 미확보 |
| B2 | 사건·신뢰도·불확실성·교차검증·정정/반박·feature | 비신뢰 텍스트 격리, 독립 출처 구분, 루머 단독 주문 금지, 시점별 feature | 규칙 기반 feature/반박/재전파 검증; 의미적 정정 연결 일부 |
| C1 | 후보/실험/연구 기억·계보·예산·중복·복구 | 영속 ledger, model/prompt/code/lock/seed/snapshot 바인딩, restart | 합성 campaign 영속화·코드/자료 drift·재개 검증 |
| C2 | 실제 모델 기반 다주기 RSI | 실제 학습/추론, 이전 성과를 바꾸면 다음 제안이 달라지는 실험, 거절/무개선 유지 | 실제 GP 제안/Ridge 학습·4주기·반증/재개 검증 |
| C3 | 조합·팩터·모델 확장 | 허용된 불변 DSL/스키마, 실제 엔진 실행, 위험/브로커 mutation 거절 | 추세/역추세·공식 정보 gate·Ridge 조합 구현; 일반 모델 탐색 아님 |
| D1 | 다종목 portfolio/달력/기업행동/역사 적격성/체결 | 비용·세금·유동성·정지·상장폐지·수정자료·보유/현금 보존 | 다종목 sleeve/현금 검사 구현; 기업행동 처리·KRX 모델 미완료 |
| D2 | walk-forward/OOS/스트레스/탐색 편향 | purging/embargo, fold 고정, test 미접근, 다중검정·regime·capacity | 고정 holdout/purged fold/비용 stress·DSR 진단 구현; 통계·regime·capacity 통합 gate 미완료 |
| D3 | 실제 Qlib·LEAN과 paper 평가 연결 | 두 전체 엔진의 정확한 signal stream/주문/성과 비교, mock broker 구분 | 팩터·Ridge stream의 실제 두 엔진 일치; RSI→paper 후보 연결 미완료 |
| D4 | 알려진 의존성 취약점 | advisory 대조, 허용된 업그레이드 시도·회귀, 미해결 위험 명시 | 5종 패치 버전 고정·엔진 회귀; DotNetZip 경고 잔존 |
| E1 | 인증된 승인/중단/복구·감사/경보 | 기본 거절, 역할/리플레이/만료, 원자적 선택, 인간 승인, risk 분리 | JWT/명령 바인딩/감사/선택 구현; 독립 증거 공급·실제 신원 미연결 |
| E2 | 운영 상태 화면/전체 orchestration | desktop/mobile·접근 제어·실제 화면동작, 수집→반복연구→검증→승인대기 | 합성 UI/다주기 흐름 검증; 실제 운영 미가동 |
| G1 | 현재 전체 품질/문서/재현 | lint/types/test/schema, 실제 모델/엔진 실행 기록, 잔여 상태 구분 | 아래 현재 검사 결과; A-E 전체 완료 아님 |

행을 삭제·축소·개명해 완료 처리하지 않습니다. 지원 경로 일부의 테스트 성공은 해당 행 전체의
완료가 아닙니다. 불가피한 외부 경계는 정확히 기록하고 독립적인 나머지 구현을 계속합니다.
실제 수집/20개 실제 paper 세션은 이번 금지 범위라 실행 증거를 만들지 않습니다.

## Progress Summary

2026-10-02 상태 정리 기준입니다. 현재는 **합성 입력으로 실제 모델·엔진을 실행하는 로컬 연구
통합 단계**이며, 매일 실자료를 수집하고 운영 전략을 개선하는 ATS의 완성이나 수익성 검증이 아닙니다.
이전 할 일 6개는 진행 표시가 뒤처져 있었습니다. 아래 검증된 작업과 남은 작업을 분리하며,
체크된 작업 수를 A-E 완료율로 환산하지 않습니다.

| 검증된 작업 | 확보한 근거 | 해석의 한계 |
| --- | --- | --- |
| 수집·분석 기본 경로 | DART 목록/원문, RSS/Atom, Naver, 공개 Telegram export, KRX 기본정보의 오프라인 처리; 시점·출처·중복·재시작 검사 | 실제 수집 없음; Telegram 상주 수집 아님; 분석은 규칙 기반 |
| 실제 모델 다주기 연구 | 합성 수집 12주기 → 연구 4주기, GP 학습 제안 2회, Ridge 신호, 불변 후보·영속 기억 | 허용된 제한 조합만 지원; 후보 모두 거절, champion 변경 없음 |
| 실제 엔진·표본 외 검증 | Qlib/LEAN 결과 비교, 영속 holdout, purging/embargo, 비용 스트레스; holdout 엔진 결과 8건 | OOS 6개라 DSR 신뢰도 null/INSUFFICIENT_EVIDENCE; deployment_ready=false |
| 합성 운영 화면·제어 | JWT 역할·명령 바인딩·중단/재개·감사, 실제 로컬 장부 표시, desktop/mobile 검사 | 합성 fixture 인증; 실제 운영 신원·독립 보고서 인증·broker 활성화 없음 |

직전 상태 점검에서 정보/RSI/운영 API 집중 테스트 **22 passed, 1 warning**과 저장된 holdout
해시 일치를 재확인했습니다. **677 passed, 2 skipped, 1 warning**은 앞선 전체 실행 기록입니다.
둘을 합산하지 않으며, 이번 정리 작업에서 모델/엔진·전체 테스트를 다시 실행했다고 주장하지 않습니다.
실제 broker 요청 0, 승격 false, A-E 미완료 상태를 유지합니다.

## Work Queue

P1은 모델 탐색 확대보다 먼저 보강할 기반, P2는 그 기반을 이용한 확장입니다. DECISION은
명시적 운영자 결정, FINAL은 개발 후 최종 검증입니다. 이번 요청은 현황·계획 정리이며 아래
개발이나 실제 실행을 새로 시작한 것은 아닙니다. 구현된 기반은 유지하고 남은 부분만 추적합니다.

| ID | 우선순위 / 관련 기준 | 남은 할 일 | 완료 판단 근거 |
| --- | --- | --- | --- |
| W1 | P1 / A1-A3, B1-B2 | KIND·다른 승인 소스의 공식 규약 확인 및 어댑터, 정정 원공시 연결, 삭제/tombstone·파생자료 보존 전파, 과거 별칭·적격성 입력 | 오프라인 정정/삭제/만료/재시작 시험; 정정은 과거 snapshot을 덮어쓰지 않고 만료 자료는 재사용 차단 |
| W2 | P1 / D1-D2 | 거래 달력·기업행동·상장폐지, 공유 현금 포트폴리오, 호가 단위·슬리피지·부분 체결·결제·유동성, 시장 체제·capacity·통계 gate | 현금/보유 보존, 미래 정보 거절, 비용·유동성·급락 시나리오, 두 엔진 비교; 합성과 실자료 검증 구분 |
| W3 | P1 / E1, D3 | 독립 평가 보고서·paper 세션의 진위와 출처 검증, 승인 대상/기간/계좌 바인딩 | 위조·재사용·만료·불일치·동시 승인 거절; 신뢰된 provider의 문자열 주장만으로 승격 불가 |
| W4 | P1 / D4 | DotNetZip 취약 의존성의 제거/대체/상위 버전 가능성 검토 및 회귀 | advisory·의존성 경로·전체 엔진 회귀 근거; 해결 불가 시 잔존 위험과 운영 차단 유지 |
| W5 | P2 / C1-C3, D2 | 허용된 팩터/모델 후보군 확대, 피드백·예산·중복·재개 검증, 전체 탐색 횟수 기록 | 실제 학습/추론·피드백 반증, 고정 holdout의 적응적 재사용 차단, 무개선 시 champion 유지 |
| W6 | P2 / A3, E1-E2 | 상주 orchestration 설계/로컬 구현, 데이터 지연·실패 경보, 재시작·백업 복구, 운영 신원 연결 경계 | 로컬 장애 주입·누락/중복·중단/복구 시험, UI 상태 일치; OS 등록·실제 신원 설정·배포는 별도 승인 |
| W7 | DECISION / D3, E1 | ADR 0004의 별도 challenger paper 시험 권한 모델 채택 여부 결정 | 명시적 결정 기록; 승인 전 champion 검사를 우회하지 않음; 채택 후에도 계약 구현·검증과 실제 실행 승인은 별개 |
| W8 | FINAL / A-E, G1 | 남은 구현 통합 후 수집→연구→검증→리뷰 전체 회귀와 상태 기록 | lint/format/types/lock/schema/tests, 실제 격리 모델·엔진, UI·장애 복구 근거; 미실행·미승인 조건 별도 표시 |

권장 순서는 W1 → W2이며, W3/W4는 이들과 독립적으로 진행할 수 있습니다. 이후 W5/W6와
W8을 진행합니다. W7 미결정은 broker 없는 구현 전체를 막지 않습니다. 이 순서는 기존 승인
기준을 축소하거나 보안·위험 검사를 모델 탐색 뒤로 미루는 의미가 아닙니다.

### Approval Gates

- 소스별 실제 수집/저장: 대상·범위·약관·보존/파생 이용·호출 제한과 명시적 승인이 필요합니다.
- ADR 0004: 계약 채택 결정만으로 계좌 접근·모의주문이 허용되지 않습니다. 실제 paper 시험은
  별도 승인과 독립 위험 검사, 실제 세션 근거가 필요하며 현재 20세션은 확보하지 않았습니다.
- OS 예약, 실제 운영 신원/비밀 설정, 배포, Git 게시, 유료 서비스는 현재 금지 범위를 유지합니다.
  기존 합성 미리보기의 승인을 새 ATS 서비스나 실제 데이터 처리에 재사용하지 않습니다.
- 승격·롤백은 인간 승인 대상이며 실거래 활성화는 이번 작업 범위 밖입니다.

## Further Considerations

1. **수집 범위와 권리:** '모든 찌라시'의 완전 수집을 보장하지 않습니다. 승인된 공개 소스 목록,
   누락/지연 지표와 수집 불가 사유를 관리합니다. 공개 게시물도 보존·재배포 권리가 자동 발생하지 않습니다.
2. **시점과 삭제:** 발표 시점·최초 관측·수정 시점을 분리하고 현재 알게 된 과거 정보를 소급하지
   않습니다. 삭제/보존 만료와 불변 연구 근거가 충돌할 때 원문·파생물 재사용 차단 및 영향받은 평가를
   식별해야 하며, 과거 해시를 조용히 바꾸는 방식은 사용하지 않습니다.
3. **과최적화와 성과:** DSR 계산 가능 여부와 투자 성과 인증은 다릅니다. 충분한 표본, 자기상관,
   시장 체제 변화, 전체 탐색 이력, 현금·비교 전략 대비 비용 차감 성과를 함께 검증해야 합니다.
4. **시장 현실성:** 두 엔진이 일치해도 같은 단순화 가정이 틀릴 수 있습니다. 실제 달력·기업행동·
   거래 정지·유동성·결제 근거를 별도로 확보하고 급락/갭에서는 손실 중단 기준이 손실 상한을 보장하지
   않는다는 점을 운영 정책에 반영해야 합니다.
5. **증거와 권한 분리:** 수집 문서나 모델 출력은 비신뢰 데이터입니다. 연구 agent가 위험 정책,
   broker, 승인 기록을 수정하지 못하게 하고 합성 키/세션을 실제 신원·운영 증거로 인정하지 않습니다.
6. **운영 신뢰성:** 데이터 신선도, 작업 지연·경합·중복, 자원/저장 비용, 백업 복구, 경보 책임자와
   중단 후 재개 절차가 필요합니다. 화면 접속 성공만으로 상주 서비스나 복구 준비가 검증되지는 않습니다.
7. **보안·유지보수:** 격리는 DotNetZip 취약점의 해결이 아닙니다. 의존성 advisory·라이선스·패치
   회귀를 계속 관리하고, 코드/모델/lock 변경 시 새 증거를 만들며 과거 campaign을 재라벨링하지 않습니다.

## Local Implementation Decisions

기존 Python 3.11/HTTPX/Pydantic/SQLite/불변 전략·평가 계약과 격리 Docker engine을 재사용합니다.
KIS 전용 job의 외부 API는 그대로 유지하고 텍스트 수집 상태는 별도로 구현합니다.
모델 실험은 무료 로컬 격리 환경에서 수행하며 Foundry/Entra/Azure 운영 완료를 주장하지 않습니다.
로컬 인증 시험은 외부 운영 ID 서비스 배포·새 자격증명 발급 권한이 아닙니다.
LLM이 없으면 학습형 모델과 규칙 분석의 차이를 명시하며 고정 응답을 AI 실행으로 부르지 않습니다.

## Source Prerequisites

실제 소스는 모두 비활성입니다. 약관 공개와 API 존재는 사용자의 이용/보존 허가가 아닙니다.

| 소스 | 확인한 규약 | 개발 경계 |
| --- | --- | --- |
| DART | 공식 공시검색·이용약관, 2026-10-02 열람 | list.json, last_reprt_at=N, 최대100행/페이지, 오류013/020 구분; 실키·API 호출 없음 |
| KIND/KRX | KRX 종목기본정보 공개 상세/DOCX와 미제공 자료 안내 확인 | 기본정보 어댑터만 검증; KIND/다른 서비스/실제 권리·키 승인 필요 |
| 뉴스 API/RSS | 제공자별 라이선스/재배포/보존 미확정 | 제공자 선택·권리 메타데이터 필수; RSS 공개만으로 본문 재배포 허가 아님 |
| 공개 채널/블로그 | 승인된 채널 목록 없음 | private/초대 채널 제외; 실제 채널 생성/봇 가입/비밀 준비 없음 |

DART는 무료 원칙이나 인증키·접근 환경·호출 허용량을 운영자가 확인해야 합니다.
명시적 원문/파생 보존 기한 없이는 저장하지 않습니다. 목록의 rcept_dt는 날짜뿐이므로
정확한 published_at을 만들어내지 않습니다. 정정은 별도 접수번호이며 기존 원문을 덮어쓰지 않습니다.
공식 문서는 수집이 아닌 API/약관 확인 목적으로만 조회했습니다.

- [DART 공시검색](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001)
- [DART 이용약관](https://opendart.fss.or.kr/intro/terms.do)
- [기존 전체 계획](ats-master-plan.md)
- [이전 로컬 통합](data-to-paper-plan.md)

## Progress Evidence

2026-10-02 재개 후 마지막 전체 실행 기록: **677 passed, 2 skipped, 1 warning**.
건너뜀은 기존 Windows symlink 생성 권한 제한, warning은 Starlette의 HTTPX TestClient
사용 중단 예정 알림입니다. Ruff lint/format (88 files), strict Pyright, lock·4종 schema 검사 통과.
이전 651 passed를 재사용하거나 부분 테스트를 합산한 수치가 아닙니다.

실제 모델/엔진 근거:

- GP/Matern이 과거 실험 성과를 학습해 다음 팩터 벡터를 선택합니다. 고정 응답이 아닙니다.
- Ridge는 각 세션 이전 행만 학습합니다. 실제 container 반증 시험에서 마지막 label을 바꿔도
  이전 예측은 같고, 과거 label을 바꾸면 이후 예측이 바뀌었습니다. training rows는
  `[0,0,0,0,0,5,6,7,8,9]`였습니다.
- Ridge 목표 `[0,0,0,0,0,10,0,0,10,0]`를 실제 Qlib/LEAN에 넣어 체결·평가자산 일치를 확인했습니다.
  평가자산은 `[100000,100000,100000,100000,100000,99899,99996,99996,99895,99992]`입니다.
- 최종 합성 workflow: 공시 수집 12개 주기(주말 catch-up 포함), 연구 4주기, GP 학습 제안 2회,
  각 후보 Ridge 신호와 실제 엔진 2개 평가. 전부 `REJECTED`, `promoted=false`, 실제 broker 요청 0.
  같은 디렉터리 재개는 연구 4주기로 유지됩니다. 실제 수익 개선이나 완성된 RSI ATS 증거는 아닙니다.
- 추가 최종 검증은 `.local/rsi-acceptance-20261002/`에 있습니다. 동일 보고서 해시는 아래와 같고,
  `holdout.json` SHA-256은 `10c4b97efdad6c89558e1e7694fd0dc4da529b39560b8946c474388e33b31293`입니다.
  purged Ridge와 비용 1/2배의 실제 엔진 검증 후 보정 하한 0, deployment_ready=false이며 재실행은
  저장된 결과를 읽습니다. 연구 장부에 holdout 점수를 추가하지 않고 이후 추가 탐색을 차단합니다.
- `.local/rsi-ridge-20261002-final/report.json` SHA-256:
  `9252dfea3c6e5bf92ad590afbad6427050bb73c0640130124e39de016f7c93cc`.
  campaign, 정보/연구/운영 SQLite, `market-snapshot.json`, `market/sha256/`가 함께 남습니다.
- 엔진 image `sha256:cd3ea0ec781369197f2be648b2fbf5ebe3a738e3e8e80eab3305ef7d65d8b724`;
  모델 image `sha256:f9c99989d8f48f8a5e6ef4f925c7e07b7d76bef886204f4a70c8eb188531f267`.
- 별도 합성 OOS는 2fold x 비용 1배/2배 x 두 엔진 = 실제 8회 실행했습니다.
  보정 평균 하한 `-0.0008270405115968488`, `deployment_ready=false`.
  이는 정규 근사/Bonferroni 방식의 개발 검사이며 DSR이나 일반 시계열 유의성 검증을 대신하지 않습니다.
   후속으로 research_validation에 고정 후보/prefix/프로토콜/재시도 예산을 묶은 영속 holdout을
   연결했습니다. 검증 시작 후 같은 campaign의 추가 연구와 다른 후보/프로토콜 재사용은 차단합니다.
   Ridge의 fold 학습은 purged train_end까지만 사용하고 모델 계수를 보존합니다.

최신 provenance 추가 후 실제 실행은 `.local/rsi-current-20261002/`에 있습니다.
report 해시는 위와 동일하고 holdout 해시는
`79d9adf25e2a55fdc65f5759a2832bfdc5ff62069096d6822e3b2551ce4d8b4c`입니다.
모델 image는 `sha256:64fcd851d424c70101c5d8e35e2dc280005ede2fd942129c0d582512db5ba587`이며
전략의 model recipe와 실제 모델 receipt가 signal artifact에 바인딩됩니다.
검증기 코드 해시도 request에 포함해 코드 변경 후 과거 cache를 새 검증으로 재사용하지 않습니다.

운영 화면 근거: 1440x900/390x844에서 가로 넘침 없음, 실제 연구 4행·canvas pixels 확인,
명령 준비/승인 토큰 바인딩 → 중단 → 재개 → 감사 누적을 브라우저에서 검사했습니다.
최신 장부에서 수집 12건/연구 4건 표시, VIEWER의 변경 금지, 잘못된 인증 후 자료·선택값 제거 확인.
최신 서버는 <http://127.0.0.1:8767>에서 위 current 장부를 읽으며 최종 검증의 연구 동결/예약 호출 8회도
표시합니다. 마지막 브라우저 native 클릭은 안정성 대기 timeout이 있어 해당 탭 전환은 DOM click과
visible/aria-selected 검사로 확인했습니다. 앞선 desktop/mobile 스크린샷·중단/재개 시험과 구분합니다.
`/fixture/*`는 공개 합성 키를 쓰는 loopback 데모 전용입니다. 운영 인증 완료나 실제 신원 증명이 아닙니다.

## Remaining Blockers and Work

미완료 항목을 승인 대기로만 바꾸지 않습니다. 특히 D1/D2/D3의 아래 구현은 계속 필요합니다.

1. KRX/KIND: 최초 상세 URL의 HTTP 500은 잘못된 경로 문제였습니다. 공식 포털의 실제 링크와
   공개 DOCX에서 stk_isu_base_info 운영 URL/basDd/AUTH_KEY/OutBlock_1을 확인해 오프라인 어댑터를
   구현했습니다. KRX 전체가 차단된 것은 아닙니다. KIND 공시·다른 KRX 서비스·원천 달력·기업행동은 남습니다.
   2026-06-01 공식 공지는 미제공 자료의 비공식 사설 라이브러리 이용 금지를 명시합니다.
   화면 우회나 pykrx로 채우지 않았으며 실제 이용/보존·서비스별 키 승인은 별도입니다.
2. DART 정정 원공시 연결, 명시적 삭제/tombstone과 모든 파생 자료의 보존 만료 전파, 실제 과거
   별칭·전체 적격 종목 자료가 남습니다. Telegram은 검증된 공개 export 수용이지 상주 bot 수집이 아닙니다.
3. 실제 KRX 기업행동/정지/상장폐지/호가·체결 모델이 남습니다. 미해결 기업행동은 현재 거절합니다.
   portfolio는 독립 sleeve 결과를 합산 검증하며 완전한 공유 주문장/체결 모델이 아닙니다.
4. 영속 holdout과 연구 점수 분리는 구현됐지만 DSR/regime/capacity 통합 gate, 검증 통과 후보의
   실제 paper/shadow 전환과 예산/감사 연결은 남습니다. 기존 champion 조건과 challenger paper
   세션의 승인 경계는 [검토용 ADR 0004](../adr/0004-paper-trial-authorization.md)에 기록했습니다.
   아직 승인하지 않았습니다. 현재 검증으로 A-E 전체를 완료 처리하지 않습니다.
5. PromotionBundle의 verified_reports/paper_sessions는 신뢰된 provider가 제공해야 합니다.
   현재 gate 계약/인간 신원/동시 선택/리플레이는 검사하지만 독립 보고서 서명/원천 자료 인증은 미연결입니다.
   실제 검증된 20세션이 없으며 합성 세션 목록은 운영 증거가 아닙니다. 선택과 broker 활성화는 분리됩니다.
6. 알려진 DotNetZip 1.16.0 GHSA-xhg6-9j5j-w4vf는 미해결입니다. 다른 패치 버전은
   System.Drawing.Common/WinHttpHandler/PKCS 10.0.12, Private.ServiceModel/ServiceModel.Primitives 4.10.3입니다.
   격리·고정 합성 입력은 잔존 취약점의 해결이나 운영 배포 승인이 아닙니다.

실제 수집·계좌/모의주문·예약 등록·배포·커밋/푸시·운영 비밀 생성/교체는 수행하지 않았습니다.

공식 KRX 참조 명세:

- [주식 서비스 목록](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES002_S1.cmd)
- [유가증권 종목기본정보](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES002_S2.cmd?BO_ID=PiwgMdTwmsenXhmqqxuj)
- [미제공 자료 안내](https://openapi.krx.co.kr/contents/OPP/COMM/notice/OPPCOMM001_S2.cmd?bbsSeq=5)

문서만 읽었으며 샘플 테스트/운영 API/인증키 신청은 실행하지 않았습니다.

## Approval Boundary at Resume

ADR 0004의 별도 challenger paper 승인 계약 개발 여부를 질문했으나 구체적인 선택 없이
사용자 부재·자율 진행이라는 응답만 받았습니다. 이를 위험 정책 변경 승인으로 해석하지 않습니다.
ADR은 PROPOSED이며 실제 paper 전환은 차단합니다. `shadow_risk`는 기존 독립 위험 검사를
호출해 미승인 candidate의 CHAMPION 실패를 기록하고, broker 요청/실제 paper 세션은 항상 0입니다.
이 승인 경계와 D1/D2/E1의 미구현 부분을 분리하며 A-E 전체 완료를 선언하지 않습니다.

현재 재개 파일/실행은 source와 문서에 남아 있습니다. 다음 순서는 ADR 결정 후 versioned paper-trial
계약을 별도로 검토하거나, 결정 전에는 DSR/regime/capacity 및 기업행동의 비실행 검증을 계속하는 것입니다.
운영자 결정 없이 기존 champion 필드·위험 정책·실제 주문 권한을 바꾸지 않습니다.

## Statistical Follow-Up

후속으로 SciPy 기반 DSR 진단을 격리 worker와 영속 holdout에 연결했습니다. per-period Sharpe,
Pearson kurtosis, 모든 탐색 횟수를 입력하며 60개 미만 관측·상수 수익률·부족한 탐색 분포에서는
신뢰도를 만들지 않습니다. 이는 비정규 IID 근사이며 자기상관/시장 체제 변화에 대한 인증은 아닙니다.

실제 반증 실행에서 같은 80개 합성 수익률의 신뢰도는 탐색 4회일 때 0.9801022570456746,
100회일 때 0.8880397211315959였고 짧은 표본은 INSUFFICIENT_EVIDENCE였습니다.
전체 실제 GP/Ridge/엔진/holdout 실행 `.local/rsi-dsr-20261002/`에서는 OOS 6개,
confidence=null, INSUFFICIENT_EVIDENCE, deployment_ready=false였습니다.
holdout SHA-256: `5295208f10abfa424ef8673fc066513c2ab70b639a05a3dd247b36c463c19412`.
모델 image: `sha256:2b584a7faa2246679076fb2a1c00397f895051bf2068e0b9f581db195b00f543`.
위 이전 image/검증 기록은 역사적 증거로 남기며 덮어쓰지 않았습니다.

Docker context 메타데이터의 일시적 파일 잠금은 사용자 설정 변경 없이 명시적 로컬 named pipe로
우회해 해결했습니다. Docker Desktop은 이번 재개 시 이미 실행 중이어서 종료하지 않았습니다.
진단 API 정의는 [SciPy skew](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.skew.html),
[SciPy kurtosis](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.kurtosis.html)와
[공개 DSR 입력 규약](https://github.com/polakowo/vectorbt/blob/master/vectorbt/returns/metrics.py)을 대조했습니다.
정규 근사 하한과 DSR은 별도 필드이며 둘을 동일한 통계로 표시하지 않습니다.
