// Monthly budget on the lab resource group: emails at 50 % and 90 % actual spend and at 100 % forecast.
// Amount is in the subscription's billing currency (INR for an Indian free account).
targetScope = 'resourceGroup'

param amount int = 1000
param contactEmail string
param startDate string = '${utcNow('yyyy-MM')}-01'

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: 'arc-lab-monthly'
  properties: {
    category: 'Cost'
    amount: amount
    timeGrain: 'Monthly'
    timePeriod: { startDate: startDate }
    notifications: {
      actual50: { enabled: true, operator: 'GreaterThan', threshold: 50, thresholdType: 'Actual', contactEmails: [ contactEmail ] }
      actual90: { enabled: true, operator: 'GreaterThan', threshold: 90, thresholdType: 'Actual', contactEmails: [ contactEmail ] }
      forecast100: { enabled: true, operator: 'GreaterThan', threshold: 100, thresholdType: 'Forecasted', contactEmails: [ contactEmail ] }
    }
  }
}
