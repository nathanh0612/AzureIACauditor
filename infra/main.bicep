param location string = 'westus2'

resource badStorage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: 'stauditdemo001'
  location: location
  sku: {
    name: 'Standard_GRS'
  }
  kind: 'StorageV2'
  properties: {
    supportsHttpsTrafficOnly: false
    allowBlobPublicAccess: true
    minimumTlsVersion: 'TLS1_0'
  }
}

resource oversizedVm 'Microsoft.Compute/virtualMachines@2023-09-01' = {
  name: 'vm-dev-oversized'
  location: location
  properties: {
    hardwareProfile: {
      vmSize: 'Standard_E16s_v5'
    }
    storageProfile: {
      osDisk: {
        createOption: 'FromImage'
        managedDisk: {
          storageAccountType: 'Standard_LRS'
        }
      }
    }
  }
}
