[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$codexConfigPath = Join-Path $env:USERPROFILE '.codex\config.toml'
if (-not (Test-Path -LiteralPath $codexConfigPath -PathType Leaf)) {
    throw "Codex config not found: $codexConfigPath"
}

$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupPath = "$codexConfigPath.backup-$timestamp"
Copy-Item -LiteralPath $codexConfigPath -Destination $backupPath

$configText = [System.IO.File]::ReadAllText($codexConfigPath)
if ($configText -match '(?m)^model\s*=.*$') {
    $configText = [regex]::Replace(
        $configText,
        '(?m)^model\s*=.*$',
        'model = "gpt-5.6-luna"',
        1
    )
} else {
    $configText = "model = `"gpt-5.6-luna`"`r`n$configText"
}

if ($configText -match '(?m)^model_reasoning_effort\s*=.*$') {
    $configText = [regex]::Replace(
        $configText,
        '(?m)^model_reasoning_effort\s*=.*$',
        'model_reasoning_effort = "high"',
        1
    )
} else {
    $configText = "model_reasoning_effort = `"high`"`r`n$configText"
}

$utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText($codexConfigPath, $configText, $utf8WithoutBom)

$modelLine = Select-String -LiteralPath $codexConfigPath -Pattern '^model\s*=' | Select-Object -First 1
$reasoningLine = Select-String -LiteralPath $codexConfigPath -Pattern '^model_reasoning_effort\s*=' | Select-Object -First 1

Write-Host "Configured: $($modelLine.Line)"
Write-Host "Configured: $($reasoningLine.Line)"
Write-Host "Backup: $backupPath"
Write-Host 'Restart Codex and create a new task for the default-model change to take effect.'
