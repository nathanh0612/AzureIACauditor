param location string = 'westus2'

@description('Deployment environment. Drives the Environment tag.')
@allowed([
  'dev'
  'test'
  'prod'
])
param environment string = 'dev'

@description('Owner of the resources (team or individual). Required tag.')
@minLength(1)
param owner string

@description('Cost center for chargeback. Required tag.')
@minLength(1)
param costCenter string

@description('VM size. Restricted to approved general-purpose SKUs; E/M series are not permitted.')
@allowed([
  'Standard_B2s'
  'Standard_D2s_v5'
])
param vmSize string = 'Standard_B2s'

var requiredTags = {
  Environment: environment
  Owner: owner
  CostCenter: costCenter
}

resource badStorage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: 'stauditdemo001'
  location: location
  tags: requiredTags
  sku: {
    name: 'Standard_GRS'
  }
  kind: 'StorageV2'
  properties: {
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
  }
}

resource oversizedVm 'Microsoft.Compute/virtualMachines@2023-09-01' = {
  name: 'vm-dev-oversized'
  location: location
  tags: requiredTags
  properties: {
    hardwareProfile: {
      vmSize: vmSize
    }
    securityProfile: {
      encryptionAtHost: true
    }
    storageProfile: {
      osDisk: {
        createOption: 'FromImage'
        managedDisk: {
          storageAccountType: 'StandardSSD_LRS'
        }
      }
    }
  }
}
