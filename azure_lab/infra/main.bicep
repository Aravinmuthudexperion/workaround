// A client-style Azure estate: two products from two different teams plus a platform-owned Azure OpenAI
// account, all in one resource group. Written the way client teams usually build things - nothing in here
// exists to help ARC Tower; the point is to see what ARC's Azure connector can work out on its own.
//
//   shared    oai-arclab-*        Azure OpenAI, one model deployment per product (used by both -> "shared")
//   resumex   func-resumex-*      Function App (Flex Consumption) - the starting point
//             plan-resumex-*      Flex Consumption plan
//             stresumex*          Storage (resumes + function deployment package)
//             kv-resumex-*        Key Vault (screening rubric)
//             appi-/law-resumex-* Application Insights + Log Analytics (traces)
//   warranty  app-warranty-*      Web App (App Service F1, free) - the starting point
//             plan-warranty-*     App Service plan F1
//             cosmos-warranty-*   Cosmos DB serverless (claims)
//             stwarranty*         Storage (claim archive)
//             appi-/law-warranty-* Application Insights + Log Analytics (traces)
//
// The two teams differ on purpose, as real teams do:
//   resumex  - managed identity everywhere, tags application/team/costCenter on every resource
//   warranty - keys and connection strings in app settings, tag Project=Warranty-Claims on some resources only
//   platform - the OpenAI account has no product tag; both apps use it
targetScope = 'resourceGroup'

@description('Region for the products.')
param location string = resourceGroup().location

@description('Region for the shared Azure OpenAI account (must offer the chat model below).')
param openAiLocation string = location

@description('Chat model deployed for both products.')
param chatModel string = 'gpt-4.1-mini'
param chatModelVersion string = '2025-04-14'

@description('Capacity of each product deployment, in thousands of tokens per minute.')
param chatCapacity int = 10

@description('Key the warranty API expects in the x-api-key header.')
@secure()
param warrantyApiKey string

var sfx = take(uniqueString(resourceGroup().id), 5)
var resumexTags = { application: 'ResumeX', team: 'hr-tech', costCenter: 'HR-1042', environment: 'lab' }
var warrantyTags = { Project: 'Warranty-Claims', environment: 'lab' }
var funcName = 'func-resumex-${sfx}'
var webName = 'app-warranty-${sfx}'
var deployContainer = 'deployments'

var roles = {
  blobDataOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  openAiUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
}

// --- shared: Azure OpenAI ------------------------------------------------------------------

resource openAi 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: 'oai-arclab-${sfx}'
  location: openAiLocation
  kind: 'OpenAI'
  sku: { name: 'S0' }
  tags: { team: 'platform', environment: 'lab' }
  properties: {
    customSubDomainName: 'oai-arclab-${sfx}'
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false   // the warranty app uses an API key
  }
}

resource resumexChat 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: openAi
  name: 'resumex-chat'
  sku: { name: 'GlobalStandard', capacity: chatCapacity }
  properties: { model: { format: 'OpenAI', name: chatModel, version: chatModelVersion } }
}

resource warrantyChat 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: openAi
  name: 'warranty-chat'
  sku: { name: 'GlobalStandard', capacity: chatCapacity }
  properties: { model: { format: 'OpenAI', name: chatModel, version: chatModelVersion } }
  dependsOn: [ resumexChat ]   // one deployment at a time per account
}

// --- resumex: Function App + Storage + Key Vault + App Insights ----------------------------

resource resumexLogs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'law-resumex-${sfx}'
  location: location
  tags: resumexTags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: json('0.1') }   // protects the credits
  }
}

resource resumexInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-resumex-${sfx}'
  location: location
  kind: 'web'
  tags: resumexTags
  properties: { Application_Type: 'web', WorkspaceResourceId: resumexLogs.id }
}

resource resumexStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stresumex${sfx}'
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  tags: resumexTags
  properties: { minimumTlsVersion: 'TLS1_2', allowBlobPublicAccess: false, supportsHttpsTrafficOnly: true }
}

resource resumexBlobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: resumexStorage
  name: 'default'
}

resource resumexDeployContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: resumexBlobs
  name: deployContainer
}

resource resumesContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: resumexBlobs
  name: 'resumes'
}

resource resumexVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv-resumex-${sfx}'
  location: location
  tags: resumexTags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    softDeleteRetentionInDays: 7
  }
}

resource rubric 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: resumexVault
  name: 'screening-rubric'
  properties: {
    value: 'Score 0-100. Weigh: relevant experience 40, skills match 30, impact/results 20, clarity 10. Flag gaps over 12 months.'
  }
}

resource resumexPlan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'plan-resumex-${sfx}'
  location: location
  kind: 'functionapp'
  tags: resumexTags
  sku: { name: 'FC1', tier: 'FlexConsumption' }
  properties: { reserved: true }
}

resource func 'Microsoft.Web/sites@2024-04-01' = {
  name: funcName
  location: location
  kind: 'functionapp,linux'
  tags: resumexTags
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: resumexPlan.id
    httpsOnly: true
    siteConfig: {
      minTlsVersion: '1.2'
      appSettings: [
        { name: 'AzureWebJobsStorage__accountName', value: resumexStorage.name }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: resumexInsights.properties.ConnectionString }
        { name: 'OTEL_SERVICE_NAME', value: 'resumex' }
        { name: 'AZURE_OPENAI_ENDPOINT', value: openAi.properties.endpoint }
        { name: 'AZURE_OPENAI_DEPLOYMENT', value: resumexChat.name }
        { name: 'KEY_VAULT_URL', value: resumexVault.properties.vaultUri }
        { name: 'RESUME_STORAGE_URL', value: resumexStorage.properties.primaryEndpoints.blob }
      ]
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${resumexStorage.properties.primaryEndpoints.blob}${deployContainer}'
          authentication: { type: 'SystemAssignedIdentity' }
        }
      }
      scaleAndConcurrency: { maximumInstanceCount: 40, instanceMemoryMB: 2048 }
      runtime: { name: 'python', version: '3.11' }
    }
  }
}

resource funcStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resumexStorage.id, func.id, roles.blobDataOwner)
  scope: resumexStorage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobDataOwner)
    principalId: func.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource funcVaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resumexVault.id, func.id, roles.keyVaultSecretsUser)
  scope: resumexVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
    principalId: func.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource funcOpenAiRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(openAi.id, func.id, roles.openAiUser)
  scope: openAi
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.openAiUser)
    principalId: func.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// --- warranty: Web App + Cosmos DB + Storage + App Insights ---------------------------------

resource warrantyLogs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'law-warranty-${sfx}'
  location: location
  tags: warrantyTags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: json('0.1') }
  }
}

resource warrantyInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-warranty-${sfx}'
  location: location
  kind: 'web'
  tags: warrantyTags
  properties: { Application_Type: 'web', WorkspaceResourceId: warrantyLogs.id }
}

resource warrantyStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stwarranty${sfx}'
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: { minimumTlsVersion: 'TLS1_2', allowBlobPublicAccess: false, supportsHttpsTrafficOnly: true }
}

resource warrantyBlobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: warrantyStorage
  name: 'default'
}

resource claimArchive 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: warrantyBlobs
  name: 'claim-archive'
}

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: 'cosmos-warranty-${sfx}'
  location: location
  kind: 'GlobalDocumentDB'
  tags: warrantyTags
  properties: {
    databaseAccountOfferType: 'Standard'
    locations: [ { locationName: location, failoverPriority: 0, isZoneRedundant: false } ]
    capabilities: [ { name: 'EnableServerless' } ]   // pay per request: pennies for a lab
    consistencyPolicy: { defaultConsistencyLevel: 'Session' }
  }
}

resource cosmosDb 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: cosmos
  name: 'warranty'
  properties: { resource: { id: 'warranty' } }
}

resource claims 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: cosmosDb
  name: 'claims'
  properties: { resource: { id: 'claims', partitionKey: { paths: [ '/product' ], kind: 'Hash' } } }
}

resource warrantyPlan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'plan-warranty-${sfx}'
  location: location
  kind: 'linux'
  sku: { name: 'F1', tier: 'Free' }
  properties: { reserved: true }
}

resource web 'Microsoft.Web/sites@2024-04-01' = {
  name: webName
  location: location
  kind: 'app,linux'
  tags: warrantyTags
  properties: {
    serverFarmId: warrantyPlan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      alwaysOn: false
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appCommandLine: 'gunicorn -w 1 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000 --timeout 120 app:app'
      appSettings: [
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: warrantyInsights.properties.ConnectionString }
        { name: 'OTEL_SERVICE_NAME', value: 'warranty' }
        { name: 'AZURE_OPENAI_ENDPOINT', value: openAi.properties.endpoint }
        { name: 'AZURE_OPENAI_API_KEY', value: openAi.listKeys().key1 }
        { name: 'AZURE_OPENAI_DEPLOYMENT', value: warrantyChat.name }
        { name: 'COSMOS_ENDPOINT', value: cosmos.properties.documentEndpoint }
        { name: 'COSMOS_KEY', value: cosmos.listKeys().primaryMasterKey }
        { name: 'ARCHIVE_STORAGE_CONNECTION_STRING', value: 'DefaultEndpointsProtocol=https;AccountName=${warrantyStorage.name};AccountKey=${warrantyStorage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}' }
        { name: 'API_KEY', value: warrantyApiKey }
      ]
    }
  }
}

// --- outputs (read by scripts/deploy.sh) ------------------------------------------------------

output suffix string = sfx
output openAiName string = openAi.name
output resumexFunctionName string = func.name
output resumexUrl string = 'https://${func.properties.defaultHostName}'
output resumexWorkspaceId string = resumexLogs.properties.customerId
output resumexVaultName string = resumexVault.name
output warrantyWebAppName string = web.name
output warrantyUrl string = 'https://${web.properties.defaultHostName}'
output warrantyWorkspaceId string = warrantyLogs.properties.customerId
