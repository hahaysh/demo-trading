# 읽기 전용 Azure 미리보기 인프라

2026-10-01 별도 승인 후 **Korea Central Windows F1에 본인 전용 합성 보고서를 게시**했습니다.
[합성 보고서 열기](https://app-atsview-dev-ac2a.azurewebsites.net/).
다른 계정의 세션 때문에 거부되면 [계정 다시 선택](https://app-atsview-dev-ac2a.azurewebsites.net/.auth/login/aad?prompt=select_account)에서 본인 계정을 선택합니다.
본인 200, 비소유자 403, 익명·잘못된 토큰 401, 앱 전용 토큰 403 및 데스크톱·모바일 표시를 확인했습니다.
다른 테넌트 실계정 검사는 미수행이며 사용자가 현재 합성 미리보기에만 명시적으로 위험을 수용했습니다.
실주문은 활성화하지 않았습니다. 유료 요금제나 다른 지역으로 자동 전환하지 않습니다.

## Target

| 항목 | 값 |
| --- | --- |
| 구독 | Azure for MCT / `199bfb5d-fa2d-4765-88cf-016129ccb7de` |
| 지역 | `koreacentral` |
| 리소스 그룹 | `atsview-rg` |
| App Service Plan | `asp-atsview-dev-ac2a` / Windows F1 |
| 웹 앱 | `app-atsview-dev-ac2a` |

F1은 무료 범위의 개발·검증용이며 하루 60 CPU분 제한과 SLA 부재를 고려해야 합니다.
현재 템플릿에는 유료 DB·컨테이너·로그 수집 서비스·Key Vault·관리 ID를 추가하지 않았습니다.
운영 자동매매 시스템의 배포 구성이 아닙니다.

## GitHub Actions

[자동 배포 구성과 운영 안내](github-actions.md)를 참고합니다. PR은 검증만 수행하고,
main 품질 검사 통과 후 OIDC로 합성 미리보기만 배포하도록 구현했습니다.
GitHub 환경·연합 신뢰·웹 앱 범위 권한은 구성했지만 워크플로는 아직 미커밋이므로
최초 원격 Actions 실행과 OIDC 기반 실제 배포는 확인되지 않았습니다.
이번 작업에서는 기존 사이트를 재배포하거나 로그인용 비밀을 교체하지 않았습니다.

## Files

- `main.bicep`: 승인된 구독·테넌트와 리소스 그룹, 모듈 연결.
- `modules/app-service.bicep`: 고정 F1·Windows·한국 중부, HTTPS/TLS, 본인 전용 인증 설정.
- `main.parameters.json`: 비밀이 없는 배포 대상 정보. 그대로는 필수 인증 매개변수가 없어 배포되지 않습니다.
- `prepare-preview.ps1`: 기존 합성 보고서만 담은 HTML을 새 로컬 폴더에 생성·검사합니다.
- `deploy-preview.ps1`: 기본 실행은 로컬 검사만 수행합니다. `-Execute`는 클라우드를 변경하므로
  승인 없이 재실행하지 않습니다. 기존 앱 발견 시 중단하며 복구·자격증명 교체는 별도 승인 대상입니다.

## Authentication Gate

`entraApplicationId`와 `entraClientSecret`은 기본값 없는 필수 매개변수입니다.
전용 단일 테넌트 앱 등록과 30일 자격증명을 승인 후 생성했습니다.
실제 issuer·client ID·본인 object ID 허용 목록과 로그인·거부 동작을 확인했습니다.
사용자 계정 구분은 본인 확인에 근거하며 신원 클레임을 직접 대조하지는 않았습니다.

비밀 입력은 Bicep `secureString`으로 전달되며 출력·예제 매개변수 파일에 넣지 않습니다.
승인된 방식은 프로세스 메모리에서 ARM으로 전달하고 App Service의 암호화된 앱 설정에
저장하는 것입니다. 설정 조회 권한자는 값을 볼 수 있으므로 Key Vault와 동등하지 않습니다.
현재 비밀 만료는 **2026-10-31 09:28:37 UTC**입니다. 로컬 파일·명령 인수·출력에 값을
남기지 않으며 자동 교체하지 않습니다. 만료 전 교체와 폐기는 별도 승인이 필요합니다.

`enablePublicIngress`의 초기 배포 기본값은 `false`를 유지합니다. 현재 운영 상태는 승인된
사후 검증을 마친 `Enabled`이지만, 인증 필수와 본인 허용 목록으로 콘텐츠 접근을 제한합니다.
다른 테넌트 검사는 `NOT_TESTED`로 유지하며 통과한 것으로 표시하지 않습니다. 이후 변경도
검증 실패 시 진입을 차단하고 SCM·FTP 기본 인증을 켜는 우회 방식은 사용하지 않습니다.

## Local Staging

저장소 루트에서, 존재하지 않는 새 출력 경로를 지정합니다. 기존 Python 환경을 사용하며
설치·Azure 접속·증권사 연결은 하지 않습니다.

```powershell
./infra/prepare-preview.ps1 -ReportPath .local/predeploy-20261001-final/report.json -OutputDirectory .local/azure-preview/preview-001
```

성공 결과의 `artifactDirectory`에는 `index.html`만 있습니다. `--evidence`를 전달하지 않아
과거 KIS 영수증도 포함하지 않습니다. 저장소나 `.local` 전체를 압축·업로드하지 마세요.
이 검사는 합성 표시와 데이터 구조·해시를 확인할 뿐, 운영자가 잘못 지정한 입력의 진위를
증명하지는 않습니다. 출력물을 검토한 뒤 별도 배포 승인을 받아야 합니다.

## Verification

```powershell
az bicep build --file infra/main.bicep --stdout > $null
./infra/deploy-preview.ps1
```

문법 검사는 실제 할당량·생성 가능 여부·인증 동작을 증명하지 않습니다. 설치된 Bicep에
최신 API 타입 정보가 없으면 경고를 남겨야 하며, 이를 숨기려고 API 버전을 임의로 낮추지 않습니다.
별도 승인 전에는 Azure 배포 명령, 앱 등록 생성, 자격증명 생성이나 HTML 업로드를 실행하지 않습니다.
