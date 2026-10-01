[CmdletBinding()]
param(
    [switch]$Execute,
    [switch]$ResumeApprovedApplication,
    [guid]$ApprovedCredentialKeyId = '8b53da28-c1c8-47ab-913e-3c588f0f260e'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$VerbosePreference = 'SilentlyContinue'
$DebugPreference = 'SilentlyContinue'
$subscriptionId = '199bfb5d-fa2d-4765-88cf-016129ccb7de'
$tenantId = '93b885b6-4629-4327-bacf-a2e5175a8ede'
$ownerId = 'adb9b0df-9bb9-4822-b938-e6b88343b5eb'
$groupName = 'atsview-rg'
$siteName = 'app-atsview-dev-ac2a'
$appName = 'atsview-dev-ac2a-auth'
$deploymentName = 'app-onboard-deploy-ac2a35ca'
$root = Split-Path $PSScriptRoot -Parent
$stage = 'local-validation'
$armToken = $null
$graphToken = $null
$headers = $null
$password = $null
$parameters = $null
$profile = $null

function Write-Evidence([string]$Event, [hashtable]$Details) {
    [ordered]@{ utc = [DateTimeOffset]::UtcNow.ToString('o'); event = $Event; details = $Details } |
        ConvertTo-Json -Depth 5 -Compress | Write-Output
}

function Assert-PreviewChanges([object[]]$Changes, [string[]]$AllowedIds, [string[]]$RequiredIds) {
    if ($Changes.Count -eq 0) { throw 'What-if returned no changes' }
    $resolvedIds = @()
    foreach ($change in $Changes) {
        if ([string]$change.ChangeType -eq 'Ignore') { continue }
        $resourceId = $change.FullyQualifiedResourceId
        if ($resourceId -notin $AllowedIds -or [string]$change.ChangeType -notin @('Create','NoChange')) {
            throw 'What-if contains unexpected changes; deployment blocked'
        }
        $resolvedIds += $resourceId
    }
    foreach ($requiredId in $RequiredIds) {
        if ($requiredId -notin $resolvedIds) { throw 'What-if did not resolve all planned parent resources' }
    }
}

try {
    Import-Module Az.Accounts -RequiredVersion 2.16.0
    Import-Module Az.Resources -RequiredVersion 6.16.0
    $compiled = az bicep build --file (Join-Path $PSScriptRoot 'main.bicep') --stdout
    if ($LASTEXITCODE -ne 0) { throw 'Bicep compilation failed' }
    $template = ($compiled -join "`n") | ConvertFrom-Json -AsHashtable
    $parameterFile = Get-Content (Join-Path $PSScriptRoot 'main.parameters.json') -Raw | ConvertFrom-Json -AsHashtable
    $parameters = @{}
    foreach ($entry in $parameterFile.parameters.GetEnumerator()) { $parameters[$entry.Key] = $entry.Value.value }
    $module = @($template.resources | Where-Object type -EQ 'Microsoft.Resources/deployments')[0].properties.template
    $plan = @($module.resources | Where-Object type -EQ 'Microsoft.Web/serverfarms')[0]
    $site = @($module.resources | Where-Object type -EQ 'Microsoft.Web/sites')[0]
    if ($parameters.location -ne 'koreacentral' -or $parameters.enablePublicIngress -ne $false -or
        $plan.sku.name -ne 'F1' -or $plan.properties.reserved -ne $false -or
        $site.name -ne $siteName -or $template.parameters.entraClientSecret.type -ne 'secureString' -or
        $parameters.ContainsKey('entraClientSecret') -or @($template.resources).Count -ne 2 -or
        @($module.resources).Count -ne 5) { throw 'Approved template boundary mismatch' }
    $htmlPath = Join-Path $root '.local/azure-preview/preview-001/site/index.html'
    if ((Get-FileHash $htmlPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne
        'a401c70ddf5c6002d519194c0c059a673a0c49a8f02b05cb81517977491ee48a') { throw 'Artifact changed' }
    Write-Evidence 'local-validation-passed' @{ execute = [bool]$Execute; upload = $false }
    if (-not $Execute) { return }

    $stage = 'authenticate'
    Disable-AzContextAutosave -Scope Process | Out-Null
    $armToken = az account get-access-token --subscription $subscriptionId --resource https://management.azure.com/ --query accessToken -o tsv
    if ($LASTEXITCODE -ne 0 -or -not $armToken) { throw 'ARM token unavailable' }
    $graphToken = az account get-access-token --tenant $tenantId --resource https://graph.microsoft.com/ --query accessToken -o tsv
    if ($LASTEXITCODE -ne 0 -or -not $graphToken) { throw 'Graph token unavailable' }
    $headers = @{ Authorization = "Bearer $graphToken" }
    $me = Invoke-RestMethod -Uri 'https://graph.microsoft.com/v1.0/me?$select=id,userPrincipalName' -Headers $headers -TimeoutSec 30
    if ($me.id -ne $ownerId) { throw 'Owner mismatch' }
    $profile = Connect-AzAccount -AccessToken $armToken -AccountId $me.userPrincipalName -Tenant $tenantId -Subscription $subscriptionId -Scope Process -SkipContextPopulation
    if ($profile.Context.Subscription.Id -ne $subscriptionId -or $profile.Context.Tenant.Id -ne $tenantId) { throw 'Azure context mismatch' }
    $existing = Invoke-RestMethod -Uri 'https://graph.microsoft.com/v1.0/applications?$filter=displayName%20eq%20%27atsview-dev-ac2a-auth%27&$select=id&$top=2' -Headers $headers -TimeoutSec 30
    if ($ResumeApprovedApplication) {
        if (@($existing.value).Count -ne 1 -or $existing.value[0].id -ne 'bcac715a-5bad-4831-9812-16b4fedccd75') {
            throw 'Approved recovery application mismatch'
        }
        $application = Invoke-RestMethod -Uri 'https://graph.microsoft.com/v1.0/applications/bcac715a-5bad-4831-9812-16b4fedccd75?$select=id,appId,signInAudience,web,passwordCredentials' -Headers $headers -TimeoutSec 30
        if ($application.appId -ne 'f9b51430-79ef-4686-9e39-affb1a844935' -or
            $application.signInAudience -ne 'AzureADMyOrg' -or
            @($application.web.redirectUris).Count -ne 1 -or
            $application.web.redirectUris[0] -ne "https://$siteName.azurewebsites.net/.auth/login/aad/callback" -or
            @($application.passwordCredentials).Count -ne 1 -or
            $application.passwordCredentials[0].keyId -ne $ApprovedCredentialKeyId.ToString()) {
            throw 'Recovery identity or credential state changed; stop without reissuing'
        }
        $stage = 'revoke-approved-unused-credential'
        $null = Invoke-RestMethod -Method Post -Uri "https://graph.microsoft.com/v1.0/applications/$($application.id)/removePassword" -Headers $headers -ContentType 'application/json' -Body (@{keyId=$ApprovedCredentialKeyId.ToString()} | ConvertTo-Json) -TimeoutSec 30
        Write-Evidence 'unused-credential-revoked' @{ keyId = $ApprovedCredentialKeyId.ToString(); applicationReused = $true }
    } else {
    if (@($existing.value).Count -ne 0) { throw 'Existing app requires explicit recovery; automatic credential reissue prohibited' }
    $stage = 'create-application'
    Write-Evidence 'application-create-started' @{ displayName = $appName; tenant = $tenantId }
    $appBody = @{
        displayName = $appName
        signInAudience = 'AzureADMyOrg'
        isFallbackPublicClient = $false
        api = @{ requestedAccessTokenVersion = 2 }
        web = @{
            redirectUris = @("https://$siteName.azurewebsites.net/.auth/login/aad/callback")
            implicitGrantSettings = @{ enableIdTokenIssuance = $true; enableAccessTokenIssuance = $false }
        }
        tags = @('ats-preview', 'ac2a35ca-c195-4461-b02b-2cba5d0ed8d8')
    } | ConvertTo-Json -Depth 6
    $application = Invoke-RestMethod -Method Post -Uri 'https://graph.microsoft.com/v1.0/applications' -Headers $headers -ContentType 'application/json' -Body $appBody -TimeoutSec 30
    Write-Evidence 'application-created' @{ objectId = $application.id; clientId = $application.appId }

    $stage = 'create-service-principal'
    $principal = Invoke-RestMethod -Method Post -Uri 'https://graph.microsoft.com/v1.0/servicePrincipals' -Headers $headers -ContentType 'application/json' -Body (@{appId=$application.appId} | ConvertTo-Json) -TimeoutSec 30
    Write-Evidence 'service-principal-created' @{ objectId = $principal.id; azureRolesAssigned = $false }
    }

    $stage = 'issue-credential'
    $expiry = [DateTimeOffset]::UtcNow.AddDays(30).ToString('o')
    $passwordBody = @{passwordCredential=@{displayName='ats-preview-30-days';endDateTime=$expiry}} | ConvertTo-Json -Depth 3
    $password = Invoke-RestMethod -Method Post -Uri "https://graph.microsoft.com/v1.0/applications/$($application.id)/addPassword" -Headers $headers -ContentType 'application/json' -Body $passwordBody -TimeoutSec 30
    if (-not $password.secretText) { throw 'Credential issuance returned no usable secret' }
    Write-Evidence 'credential-issued' @{ keyId = $password.keyId; expiresUtc = $password.endDateTime; secretPersistedLocally = $false }
    $parameters.entraApplicationId = $application.appId
    $parameters.entraClientSecret = [string]$password.secretText
    if ($template.parameters.entraClientSecret.type -ne 'secureString' -or
        $parameters.entraClientSecret -isnot [string]) { throw 'ARM secure parameter transport mismatch' }

    $stage = 'what-if'
    Write-Evidence 'what-if-started' @{ deploymentName = $deploymentName }
    $preview = Get-AzSubscriptionDeploymentWhatIfResult -Name $deploymentName -Location koreacentral -TemplateObject $template -TemplateParameterObject $parameters -ResultFormat FullResourcePayloads -SkipTemplateParameterPrompt -DefaultProfile $profile
    if ([string]$preview.Status -ne 'Succeeded') { throw 'What-if did not succeed' }
    $groupId = "/subscriptions/$subscriptionId/resourceGroups/$groupName"
    $siteId = "$groupId/providers/Microsoft.Web/sites/$siteName"
    $planId = "$groupId/providers/Microsoft.Web/serverfarms/asp-atsview-dev-ac2a"
    $allowed = @($groupId, $planId, $siteId, "$siteId/config/authsettingsV2",
        "$siteId/basicPublishingCredentialsPolicies/scm", "$siteId/basicPublishingCredentialsPolicies/ftp",
        "$groupId/providers/Microsoft.Resources/deployments/atsview-dev-ac2a-app-service")
    $changes = @($preview.Changes)
    Write-Evidence 'what-if-returned' @{ status = [string]$preview.Status; changes = @($changes | ForEach-Object { @{resourceId=$_.FullyQualifiedResourceId;changeType=[string]$_.ChangeType} }) }
    Assert-PreviewChanges -Changes $changes -AllowedIds $allowed -RequiredIds @($groupId,$planId,$siteId)
    Write-Evidence 'what-if-passed' @{ changeCount = $changes.Count }

    $stage = 'deploy-ingress-disabled'
    $portal = 'https://portal.azure.com/#view/Microsoft_Azure_Resources/DeploymentDetails.MenuView/~/overview/id/' +
        [Uri]::EscapeDataString("/subscriptions/$subscriptionId/providers/Microsoft.Resources/deployments/$deploymentName")
    Write-Evidence 'deployment-started' @{ deploymentName = $deploymentName; portal = $portal; publicIngress = $false }
    $deployment = New-AzSubscriptionDeployment -Name $deploymentName -Location koreacentral -TemplateObject $template -TemplateParameterObject $parameters -SkipTemplateParameterPrompt -DefaultProfile $profile
    Write-Evidence 'deployment-returned' @{ deploymentName = $deploymentName; provisioningState = [string]$deployment.ProvisioningState; upload = $false }
    if ([string]$deployment.ProvisioningState -ne 'Succeeded') { throw 'Deployment did not succeed' }
} catch {
    Write-Evidence 'execution-blocked' @{ stage = $stage; exceptionType = $_.Exception.GetType().FullName; errorId = $_.FullyQualifiedErrorId; responseWithheld = $true; upload = $false }
    throw 'Preview deployment stopped; inspect sanitized stage evidence. Credentials must not be reissued automatically.'
} finally {
    if ($null -ne $parameters) { $parameters.Remove('entraClientSecret') }
    $password = $null
    $headers = $null
    $graphToken = $null
    $armToken = $null
    $profile = $null
}