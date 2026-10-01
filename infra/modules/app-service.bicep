targetScope = 'resourceGroup'

@allowed([
  'koreacentral'
])
param location string

param tags object

@minLength(36)
@maxLength(36)
param entraApplicationId string

@secure()
@minLength(1)
param entraClientSecret string

param enablePublicIngress bool = false

var tenantId = '93b885b6-4629-4327-bacf-a2e5175a8ede'
var ownerObjectId = 'adb9b0df-9bb9-4822-b938-e6b88343b5eb'
var clientSecretSettingName = 'MICROSOFT_PROVIDER_AUTHENTICATION_SECRET'

resource appServicePlan 'Microsoft.Web/serverfarms@2026-08-01' = {
  name: 'asp-atsview-dev-ac2a'
  location: location
  tags: tags
  kind: 'app'
  sku: {
    name: 'F1'
    tier: 'Free'
  }
  properties: {
    reserved: false
  }
}

resource site 'Microsoft.Web/sites@2026-08-01' = {
  name: 'app-atsview-dev-ac2a'
  location: location
  tags: tags
  kind: 'app'
  properties: {
    serverFarmId: appServicePlan.id
    reserved: false
    httpsOnly: true
    publicNetworkAccess: enablePublicIngress ? 'Enabled' : 'Disabled'
    siteConfig: {
      use32BitWorkerProcess: true
      alwaysOn: false
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      ftpsState: 'Disabled'
      remoteDebuggingEnabled: false
      httpLoggingEnabled: false
      detailedErrorLoggingEnabled: false
      requestTracingEnabled: false
      defaultDocuments: [
        'index.html'
      ]
      appSettings: [
        {
          name: clientSecretSettingName
          value: entraClientSecret
        }
        {
          name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
          value: 'false'
        }
        {
          name: 'ENABLE_ORYX_BUILD'
          value: 'false'
        }
      ]
    }
  }
}

resource authentication 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: site
  name: 'authsettingsV2'
  properties: {
    platform: {
      enabled: true
      runtimeVersion: '~1'
    }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureActiveDirectory'
      excludedPaths: []
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: entraApplicationId
          clientSecretSettingName: clientSecretSettingName
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [
            entraApplicationId
          ]
          defaultAuthorizationPolicy: {
            allowedPrincipals: {
              identities: [
                ownerObjectId
              ]
            }
          }
        }
      }
    }
    login: {
      tokenStore: {
        enabled: false
      }
    }
    httpSettings: {
      requireHttps: true
    }
  }
}

resource scmPublishingPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: site
  name: 'scm'
  properties: {
    allow: false
  }
}

resource ftpPublishingPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: site
  name: 'ftp'
  properties: {
    allow: false
  }
}
