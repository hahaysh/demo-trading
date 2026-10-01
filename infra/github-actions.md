# Synthetic Preview CI/CD

Last verified: 2026-10-01

## Scope and Status

사용자가 승인한 범위는 **GitHub Actions 자동 배포와 OIDC 연결**뿐입니다.
실제 데이터 수집, 전략·위험 정책, 주문, AI 연구 등 나머지 ATS 작업은 검토 전까지 진행하지 않습니다.

- PR과 모든 push에서 Linux/Windows 품질 검사를 수행합니다.
- Windows 작업은 Azure 인증 없이 새 합성 보고서를 생성하고 `index.html` 한 개의 ZIP을 만듭니다.
- 두 운영체제의 품질 검사가 모두 성공한 `main` push 또는 `main` 수동 실행만 배포할 수 있습니다.
- 배포 작업은 같은 실행의 커밋·ZIP·HTML 해시를 재확인한 뒤 OIDC로 Azure에 로그인합니다.
- 기존 웹 앱에만 게시하며 인프라 생성·로그인용 비밀 발급·SKU 변경을 실행하지 않습니다.
- 사용자는 다른 테넌트 실계정 검사 미수행 위험을 **이 CI가 생성하는 합성 미리보기의 반복 게시**에만
  적용하도록 승인했습니다. 해당 검사는 계속 `NOT_TESTED`이며 실제 데이터·운영 ATS·실거래에는 적용하지 않습니다.

워크플로와 도구는 로컬에 구현했고 원격 OIDC·GitHub 환경·변수·권한을 실제 구성 및 재조회했습니다.
**커밋·푸시와 원격 Actions 실행은 수행하지 않았습니다.** 실제 OIDC 토큰 교환부터 게시까지의
최초 원격 실행은 관련 파일을 함께 `main`에 반영한 뒤 확인해야 합니다.
기존 사이트 콘텐츠·인증 설정은 이번 연결 작업에서 변경하지 않았습니다.

## Workflow

정의: [quality.yml](../.github/workflows/quality.yml)

| 작업 | 권한과 조건 |
| --- | --- |
| `quality` | `contents: read`만 사용. Azure 로그인·OIDC 권한 없음. PR 코드를 배포하지 않음 |
| `deploy_preview` | 품질 검사 성공, 원본 저장소, `main`, push/수동 실행, 배포 스위치 true 필요 |
| `ats-preview` 환경 | 배포 브랜치는 `main` 하나만 허용. 선택한 자동 배포 방식에 따라 별도 reviewer 승인 없음 |
| OIDC | 배포 작업에만 `id-token: write`. 비밀 또는 publish profile 없이 단기 토큰 사용 |
| 동시 실행 | 같은 사이트 배포는 직렬화, 진행 중 배포 취소 안 함. 배포 전 최신 main과 비교해 오래된 커밋 제외 |
| 산출물 | 같은 workflow run의 SHA·run attempt별 artifact, 7일 보관. ZIP·manifest만 전달 |

액션 참조는 확인한 커밋 SHA로 고정했고 checkout의 인증 정보 영속 저장은 끕니다.
`pull_request_target`이나 PR artifact를 받는 `workflow_run` 배포를 사용하지 않습니다.

## Azure Identity

| 항목 | 값 |
| --- | --- |
| 저장소 / 불변 ID | `hahaysh/demo-trading` / `1397617688` |
| 소유자 불변 ID | `24690076` |
| Azure 구독 | `199bfb5d-fa2d-4765-88cf-016129ccb7de` / Azure for MCT |
| 테넌트 | `93b885b6-4629-4327-bacf-a2e5175a8ede` |
| 배포 전용 앱 | `atsview-dev-ac2a-github` |
| Client ID | `26b6297f-6ec0-4c20-955f-f4fdc4ced65e` |
| Application object ID | `9d013ef3-e8cf-4ffb-b2e0-5a92d613d7e1` |
| Service principal ID | `3b28fbee-15de-4aff-a445-f96e93b59813` |
| 역할 | Website Contributor / `de139f84-1756-47ae-9be6-808fbbe84772` |
| 할당 범위 | `/subscriptions/199bfb5d-fa2d-4765-88cf-016129ccb7de/resourceGroups/atsview-rg/providers/Microsoft.Web/sites/app-atsview-dev-ac2a` |
| 역할 할당 ID | `b9691cf7-f2fa-4b0a-9601-93ba48c9b9c4` |

이 역할은 ZIP 게시만이 아니라 사이트 구성 변경 권한도 포함합니다. 구독·리소스 그룹 전체에는
부여하지 않았지만, `main`과 워크플로·게시 스크립트를 변경할 수 있는 주체는 신뢰 경계입니다.
해당 변경을 반드시 검토하고, 필요하면 별도 승인으로 브랜치 보호를 강화합니다.

새 배포 ID의 password·certificate·Graph permission은 모두 0개로 확인했습니다.
기존 웹사이트 로그인용 앱과는 별개이며, **로그인용 비밀의 2026-10-31 만료는 그대로 남습니다.**

GitHub OIDC subject 설정은 다음 순서입니다.

```json
{"use_default":false,"include_claim_keys":["repo","context","ref","workflow_ref","event_name"]}
```

불변 저장소 prefix는 `repo:hahaysh@24690076/demo-trading@1397617688`입니다.
Azure 연합 자격증명은 issuer `https://token.actions.githubusercontent.com`,
audience `api://AzureADTokenExchange`이며, subject는 아래 두 값만 허용합니다.

```text
repo:hahaysh@24690076/demo-trading@1397617688:environment:ats-preview:ref:refs/heads/main:workflow_ref:hahaysh/demo-trading/.github/workflows/quality.yml@refs/heads/main:event_name:push
repo:hahaysh@24690076/demo-trading@1397617688:environment:ats-preview:ref:refs/heads/main:workflow_ref:hahaysh/demo-trading/.github/workflows/quality.yml@refs/heads/main:event_name:workflow_dispatch
```

다른 저장소·브랜치·태그·PR 이벤트·워크플로·환경에는 토큰 교환을 허용하지 않습니다.
저장소 수준 OIDC 형식이 바뀌었으므로 미래의 다른 OIDC 작업은 별도로 신뢰 조건을 설계해야 합니다.

## GitHub Settings

- Repository variable: `ATS_PREVIEW_DEPLOY_ENABLED=true`. 구현 파일을 올려야 배포 job이 생깁니다.
- Environment: `ats-preview`; branch policy: `main` / branch only.
- Environment variables: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`는 위 비밀이 아닌 ID입니다.
- 클라이언트 비밀·publish profile·증권사 키를 GitHub Secrets에 추가하지 않았습니다.
- 설정 확인 시각: 2026-10-01 11:13:48 UTC. 실제 Actions 런타임 검증 시각은 아직 없습니다.

## Publication Gates

[prepare-preview.ps1](prepare-preview.ps1)은 새 출력 폴더만 허용하며 합성 모드, 승격·운영 배포
플래그 false, KIS evidence null, 한국어 HTML 및 네트워크 차단 CSP를 검사합니다.
ZIP에는 `index.html` 하나만 포함하고 manifest는 ZIP 밖에 둡니다.

[publish-preview.ps1](publish-preview.ps1)은 기본값이 **오프라인 검사**입니다.
`-Execute`도 원본 저장소의 main push/수동 GitHub 실행과 동일 SHA일 때만 허용됩니다.
커밋·해시·파일 개수·크기·ZIP entry·reparse 경로와 합성 보고서 조건을 재검사합니다.

자동 사후 검사는 실제 인증 설정, 본인 allowlist, issuer/audience, HTTPS/TLS, 기본 게시 인증
차단, Entra SCM으로 읽은 HTML 해시, 익명 접근 거부를 확인합니다. 사용자 비밀번호·MFA를
자동화하거나 OIDC 배포 ID를 사이트 콘텐츠 허용 목록에 추가하지 않습니다.
**본인 로그인·다른 사용자 로그인·화면의 수동 회귀 검사를 매 실행마다 수행했다는 뜻은 아닙니다.**
인증 변경이나 새 데이터 범위가 있으면 기존 반복 게시 승인과 구분해 별도 검토합니다.

정책 차이·업로드 실패·해시 불일치·익명 차단 실패 시 공개 진입을 `Disabled`로 되돌립니다.
워크플로의 후속 cleanup도 불완전한 게시를 차단하도록 시도합니다. 러너 강제 종료·네트워크
단절로 cleanup 자체가 실행되지 못할 수 있으므로 실패 알림과 운영자 확인은 여전히 필요합니다.
다음 실행은 차단된 사이트를 자동으로 열지 않고 실패합니다. 복구는 원인 해결과 별도 승인 후 수행합니다.

## Validation

로컬의 새 합성 실행 -> HTML/ZIP 생성과 최종 23개 패키지·정책 허용/거부 검사를 통과했습니다.
실제 Azure 인증 응답을 새 검사 함수로 읽기 전용 검증했고, 원격 환경·OIDC·사이트 범위 역할을
재조회했습니다. YAML 구조·트리거·권한 분리·액션 SHA 고정도 검사했습니다.
VS Code가 새 환경·변수의 이전 목록을 캐시하면 문맥 경고가 표시될 수 있습니다.
실제 GitHub 설정 조회 결과와 구분하며 원격 실행 성공으로 해석하지 않습니다.

로컬 검증 예시입니다. 기존 출력 경로를 덮어쓰지 말고 새 이름을 사용합니다.

```powershell
uv run --frozen python -m ats.demo --output .local/ci-check
./infra/prepare-preview.ps1 -ReportPath .local/ci-check/report.json -OutputDirectory .local/azure-preview/ci-check -SourceCommit (git rev-parse HEAD)
./infra/test-preview-ci.ps1 -PackageDirectory .local/azure-preview/ci-check/package -ExpectedCommit (git rev-parse HEAD)
```

현재 필요한 인프라·dashboard 파일 일부도 미커밋 상태이므로 workflow만 따로 올리지 않습니다.
참조 파일·의존성을 함께 검토해 커밋·푸시한 다음 최초 main 실행의 OIDC 로그인, 게시, 검증 결과를
확인합니다. 인프라 전체 배포 스크립트나 앱 등록·자격증명 생성은 재실행하지 않습니다.

## Pause and Recovery

자동 배포를 일시 중지하려면 Repository variable `ATS_PREVIEW_DEPLOY_ENABLED`를 `false`로
변경합니다. 이미 진행 중인 배포는 이 변경만으로 취소되지 않으므로 해당 실행을 따로 확인합니다.
현재 게시된 보고서와 사이트는 이 변수만 바꿔도 삭제되거나 차단되지 않습니다.
권한 폐기·사이트 복구·로그인 비밀 교체는 각각 별도 승인 범위입니다.
