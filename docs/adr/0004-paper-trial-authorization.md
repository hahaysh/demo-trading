# Separate Paper Trial Authorization

- Status: PROPOSED - NOT APPROVED OR ACTIVATED
- Date: 2026-10-02
- Related: [RSI A-E plan](../plans/rsi-development-plan.md)

## Context

현행 OrderIntent/독립 위험 검사에는 승인된 champion 일치 검사가 있습니다.
승격 정책에는 최소 20개 paper 세션과 인간 승인이 필요합니다. challenger 검증을 연결할 때
candidate를 champion이라고 임의 표시하거나 합성 paper 기록을 운영 실적으로 인정하면
이 두 경계를 우회하게 됩니다. 이번 실행 권한은 실제 계좌/모의주문을 명시적으로 금지합니다.

## Proposed Decision

실제 broker 호출 없이 검토할 제안입니다. 채택·별도 실행 승인 전까지 기존 코드/정책을 유지합니다.

1. 불변 candidate digest, 독립 평가 증거, 모의 계좌 별칭, 유효 기간을 묶은 별도
   PaperTrialAuthorization을 사람이 승인합니다. 연구 agent는 이를 발급하거나 승인하지 못합니다.
2. 기존 champion 계약을 재해석하지 않고 별도 버전의 paper-only 승인/주문 계약과 위험 검사를 둡니다.
   연구 candidate의 paper 허가는 champion 승격·운영 전략 교체·실거래 권한을 주지 않습니다.
3. 현금·실보유 모의잔고·종목 10%·손실 1%·낙폭 15%·신선도·중복·계좌·실행 시각 검사를
   독립적으로 유지합니다. 추가 고정 투자금이나 일일 매수액/주문 건수 상한을 도입하지 않습니다.
4. 실제 paper 세션/체결/위험·복구 결과는 broker 근거와 함께 별도 보존합니다. 합성 결과와
   실제 세션을 섞지 않습니다. 승격에는 기존 최소 세션·성과·인간 승인 조건을 유지합니다.
5. 미해결 주문, 만료, 중단 시 신규 전송을 차단합니다. 별도 권한 없이 자동 재개·청산·정책 완화를 하지 않습니다.

## Alternatives

- broker 연결 없는 shadow 평가를 계속하고 실제 paper 전환을 보류: 현재 허용 범위입니다.
- candidate를 기존 champion 필드에 대입: 승인 의미를 바꾸므로 채택하지 않습니다.
- 20개 합성 세션으로 승격 gate 충족: 실제 운영 증거가 아니므로 채택하지 않습니다.

## Required Decision

별도 paper 시험 권한 모델을 채택할지 운영자가 결정해야 합니다. 채택하더라도 실제 계좌 접근·
모의주문 실행은 별도 승인 대상입니다. 이 문서 작성으로 정책이나 주문 권한은 활성화되지 않습니다.
