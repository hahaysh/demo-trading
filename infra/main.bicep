targetScope = 'subscription'

metadata description = '합성 보고서용 Windows IIS F1 정적 미리보기. 로컬 IaC 작성만 승인되었으며 배포 승인이 아니다.'
metadata operatorGuidance = '''
대상은 Azure for MCT 구독 199bfb5d-fa2d-4765-88cf-016129ccb7de, 테넌트 93b885b6-4629-4327-bacf-a2e5175a8ede이다.
향후 승인된 배포에서도 대상 구독을 명시해야 한다. 기본 MTT 구독을 사용하거나 CLI 기본값을 변경하지 않는다.
approvedResourceGroups 조회는 실제 실행 구독과 테넌트가 일치하지 않으면 실패한다. 모듈 단독 배포는 지원하지 않는다.
Korea Central의 F1 할당량, 수용 능력, 이름 가용성, 정책, 권한, MFA 및 최종 비용은 배포 전 확인 대상이다.
무료 F1은 하루 60 CPU분이며 SLA가 없다. 유료 SKU, 다른 지역, 추가 서비스로 자동 전환하지 않는다.
앱 등록과 자격증명 생성은 수행하지 않는다. 향후 별도 승인 후 단일 테넌트 앱 등록, 콜백 URI 및 소유자 접근을 검증한다.
클라이언트 ID와 비밀값은 기본값 없이 필수 입력이다. 비밀값을 매개변수 파일, 명령 기록, 로그, 출력에 넣지 않는다.
보안 문자열은 App Service 앱 설정으로 전달된다. 저장 시 암호화되지만 설정 읽기 권한자는 조회할 수 있으므로 Key Vault와 동등하지 않다.
Key Vault, 관리 ID, 역할 할당은 승인 범위 밖이므로 생성하지 않는다. 비밀 전달·저장·교체·만료 관리 방식의 승인은 배포 차단 조건이다.
enablePublicIngress=false를 초기 배포에서도 유지한다. 공개 진입을 열기 전에 제어 영역에서 실제 인증·소유자 제한·게시 정책을 확인한다.
별도 배포 승인과 인증 설정 검증 후에만 빈 사이트의 공개 진입을 통제된 검증 목적으로 활성화한다.
익명 사용자, 다른 테넌트 사용자, 같은 테넌트의 비소유자 및 앱 전용 토큰은 거부되고 소유자만 접근하는지 검증한다.
인증 리디렉션·콜백·정적 파일 경로까지 확인하기 전에는 보고서를 업로드하지 않는다. 실패 시 즉시 공개 진입을 다시 차단한다.
SCM/FTP 기본 인증은 계속 비활성화한다. Entra 기반 게시 절차와 공개 진입 차단 시 게시 가능 여부는 별도 검증한다.
배포 산출물은 검토된 합성 전용 index.html과 필요한 검토 완료 플랫폼 설정만 허용한다.
현재 .local/dashboard, 저장소 전체, 소스, KIS 메타데이터, 실제 시세 및 자격증명은 업로드하지 않는다. 산출물 준비·검증은 아직 미완료다.
이 정적 사이트에는 증권사 환경 변수나 백엔드가 없다. Python은 로컬 HTML 생성에만 사용하며 Azure에서 실행하거나 빌드하지 않는다.
F1에서 지원하지 않는 Health Check는 생략한다. 인증된 루트 경로 /는 302를 반환할 수 있어 정상 상태 검사로도 부적합하다.
토큰 저장소와 HTTP 요청·상세 오류·실패 요청 추적을 비활성화하고 진단 수집 리소스를 추가하지 않는다. 향후 검증에서도 토큰을 출력하지 않는다.
컴파일은 문법 검사일 뿐 할당량, API 수용 여부, F1 지원, 인증 동작 또는 배포 성공을 증명하지 않는다.
'''
metadata apiVersionEvidence = '''
Microsoft.Web/sites 및 Microsoft.Web/serverfarms: 호출자가 공급자 조회로 확인한 GA 2026-08-01을 유지한다.
Microsoft.Resources/resourceGroups: 대상 구독을 명시한 공급자 조회에서 GA 2023-07-01을 확인했다.
Microsoft.Web/sites/config: 제공된 MCP 스키마의 2024-11-01 인증 정의를 사용한다.
Microsoft.Web/sites/basicPublishingCredentialsPolicies: Microsoft Learn의 2024-11-01 스키마를 확인했다.
Web 하위 리소스의 2026-08-01 문서 조회는 404였다. 부모의 검증된 버전을 낮추거나 미검증 하위 버전을 추정하지 않는다.
'''

@description('승인된 환경 이름이며 리소스 이름 접두사와 같다.')
@allowed([
  'atsview-dev-ac2a'
])
param environmentName string

@description('승인된 지역. 다른 지역으로 변경하려면 별도 승인이 필요하다.')
@allowed([
  'koreacentral'
])
param location string

@minLength(1)
param sessionId string

@minLength(1)
param deployedBy string

@description('Get-Date -Format "o"로 얻은 실제 IaC 생성 시각.')
@minLength(1)
param createdAt string

@description('별도로 승인·검증한 단일 테넌트 앱 등록의 클라이언트 ID. 기본값이 없다.')
@minLength(36)
@maxLength(36)
param entraApplicationId string

@description('별도 승인된 보안 전달 방식으로만 제공할 인증 비밀값. 파일에 저장하지 않는다.')
@secure()
@minLength(1)
param entraClientSecret string

@description('초기값은 공개 진입 차단. 별도 배포 승인 및 인증 설정 검증 후에만 활성화한다.')
param enablePublicIngress bool = false

var approvedResourceGroups = {
  '199bfb5d-fa2d-4765-88cf-016129ccb7de': {
    '93b885b6-4629-4327-bacf-a2e5175a8ede': 'atsview-rg'
  }
}

var tags = {
  'app-onboard-skill': 'true'
  'app-onboard-session-id': sessionId
  'created-at': createdAt
  environment: environmentName
  'deployed-by': deployedBy
}

resource resourceGroup 'Microsoft.Resources/resourceGroups@2023-07-01' = {
  name: approvedResourceGroups[subscription().subscriptionId][subscription().tenantId]
  location: location
  tags: tags
}

module appService './modules/app-service.bicep' = {
  name: 'atsview-dev-ac2a-app-service'
  scope: resourceGroup
  params: {
    location: location
    tags: tags
    entraApplicationId: entraApplicationId
    entraClientSecret: entraClientSecret
    enablePublicIngress: enablePublicIngress
  }
}
