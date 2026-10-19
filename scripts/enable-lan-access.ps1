$ErrorActionPreference = 'Stop'

$ruleName = 'ChromeManager-LAN-8765'
$existing = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
if ($existing) {
    Set-NetFirewallRule -Name $ruleName -Enabled True -Direction Inbound `
        -Action Allow -Profile Any -Protocol TCP -LocalPort 8765 -RemoteAddress LocalSubnet
} else {
    New-NetFirewallRule -Name $ruleName -DisplayName 'ChromeManager LAN TCP 8765' `
        -Enabled True -Direction Inbound -Action Allow -Profile Any `
        -Protocol TCP -LocalPort 8765 -RemoteAddress LocalSubnet | Out-Null
}
