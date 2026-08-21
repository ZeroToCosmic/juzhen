[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-SourceManifest {
    param([Parameter(Mandatory = $true)][string]$SourceDirectory)

    $normalizedRoot = $SourceDirectory.TrimEnd('\') + '\'
    $fileIndex = @{}
    Get-ChildItem -LiteralPath $SourceDirectory -File -Recurse -Force | ForEach-Object {
        $relativePath = $_.FullName.Substring($normalizedRoot.Length).Replace('\', '/').ToLowerInvariant()
        if ($fileIndex.ContainsKey($relativePath)) {
            throw "The source directory contains paths that are not distinct on Windows: $relativePath"
        }
        $fileIndex[$relativePath] = $_.FullName
    }

    $relativePaths = [string[]]@($fileIndex.Keys)
    [System.Array]::Sort($relativePaths, [System.StringComparer]::Ordinal)
    $files = [System.Collections.Generic.List[object]]::new()
    $hashLines = [System.Collections.Generic.List[string]]::new()
    foreach ($relativePath in $relativePaths) {
        $fullPath = $fileIndex[$relativePath]
        $fileHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $fullPath).Hash.ToLowerInvariant()
        $size = (Get-Item -LiteralPath $fullPath).Length
        $files.Add([pscustomobject][ordered]@{
            path = $relativePath
            sha256 = $fileHash
            size = $size
        })
        $hashLines.Add("$relativePath`t$fileHash`t$size")
    }

    $hashInput = [System.Text.Encoding]::UTF8.GetBytes(([string]::Join("`n", [string[]]$hashLines) + "`n"))
    $treeHashBytes = [System.Security.Cryptography.SHA256]::Create().ComputeHash($hashInput)
    $treeHash = -join ($treeHashBytes | ForEach-Object { $_.ToString('x2') })
    return [pscustomobject][ordered]@{
        schema_version = 1
        algorithm = 'sha256'
        tree_sha256 = $treeHash
        file_count = $files.Count
        files = @($files)
    }
}

function Assert-DockerReady {
    try {
        $docker = Get-Command docker -ErrorAction Stop
    }
    catch {
        throw 'Docker client was not found. Install Docker Desktop, then reopen PowerShell.'
    }

    try {
        & $docker.Source version --format '{{.Server.Version}}' | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw 'docker version returned a non-zero exit code.'
        }
    }
    catch {
        throw 'Docker Engine is unavailable. Start Docker Desktop and verify this Windows user can access it.'
    }
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$serviceDirectory = Join-Path $projectRoot 'services\tiktok_api'
$vendorDirectory = Join-Path $serviceDirectory 'vendor\Douyin_TikTok_Download_API'
$versionPath = Join-Path $serviceDirectory 'VERSION.json'
$sourceManifestPath = Join-Path $serviceDirectory 'SOURCE-MANIFEST.json'
$composePath = Join-Path $serviceDirectory 'docker-compose.yml'
$runtimeCookieConfig = Join-Path $serviceDirectory 'runtime\tiktok_web_config.yaml'
$healthBaseUrl = 'http://127.0.0.1:53281'
$healthUrl = "$healthBaseUrl/docs"

if (-not (Test-Path -LiteralPath $versionPath)) {
    throw "VERSION.json is missing. Run scripts/install_tiktok_api.ps1 with a reviewed 40-character commit SHA first."
}
if (-not (Test-Path -LiteralPath $vendorDirectory) -or -not (Test-Path -LiteralPath $sourceManifestPath)) {
    throw "Pinned vendor source or its install-time manifest is missing. Re-run scripts/install_tiktok_api.ps1."
}
if (-not (Test-Path -LiteralPath (Join-Path $vendorDirectory 'LICENSE'))) {
    throw "Pinned vendor source is missing LICENSE. Re-run scripts/install_tiktok_api.ps1."
}
if (-not (Test-Path -LiteralPath $runtimeCookieConfig)) {
    throw "Runtime TikTok Cookie configuration is missing. Re-run scripts/install_tiktok_api.ps1 to generate it."
}

try {
    $version = Get-Content -LiteralPath $versionPath -Raw | ConvertFrom-Json
    $sourceManifest = Get-Content -LiteralPath $sourceManifestPath -Raw | ConvertFrom-Json
}
catch {
    throw "VERSION.json or SOURCE-MANIFEST.json is invalid JSON. Re-run scripts/install_tiktok_api.ps1."
}

$versionFileCountProperty = $version.PSObject.Properties['source_file_count']
$sourceManifestFileCountProperty = $sourceManifest.PSObject.Properties['file_count']
if ($null -eq $versionFileCountProperty -or $null -eq $sourceManifestFileCountProperty) {
    throw "VERSION.json source_file_count is invalid or does not match SOURCE-MANIFEST.json. Refusing to start; re-run scripts/install_tiktok_api.ps1."
}
$versionFileCount = [string]$versionFileCountProperty.Value
$sourceManifestFileCount = [string]$sourceManifestFileCountProperty.Value
if ($versionFileCount -notmatch '^[1-9][0-9]*$' -or
    $sourceManifestFileCount -notmatch '^[1-9][0-9]*$' -or
    $versionFileCount -ne $sourceManifestFileCount) {
    throw "VERSION.json source_file_count is invalid or does not match SOURCE-MANIFEST.json. Refusing to start; re-run scripts/install_tiktok_api.ps1."
}

if ($version.commit_sha -notmatch '^[0-9a-fA-F]{40}$' -or
    $version.source_manifest_file -ne 'SOURCE-MANIFEST.json' -or
    $version.source_tree_sha256 -notmatch '^[0-9a-fA-F]{64}$' -or
    $sourceManifest.tree_sha256 -notmatch '^[0-9a-fA-F]{64}$' -or
    $version.source_tree_sha256 -ne $sourceManifest.tree_sha256) {
    throw "VERSION.json does not match the install-time source manifest. Refusing to start; re-run scripts/install_tiktok_api.ps1."
}

$expectedImageTag = "local/tiktok-api:$($version.commit_sha.ToLowerInvariant())"
if ($version.image_tag -ne $expectedImageTag) {
    throw "VERSION.json image tag does not match its pinned commit. Refusing to start; re-run scripts/install_tiktok_api.ps1."
}

$actualSourceManifest = Get-SourceManifest -SourceDirectory $vendorDirectory
if ($actualSourceManifest.tree_sha256 -ne $sourceManifest.tree_sha256 -or
    $actualSourceManifest.file_count -ne $sourceManifest.file_count) {
    throw "Vendor source contents do not match the install-time manifest. Refusing to start; re-run scripts/install_tiktok_api.ps1."
}

Assert-DockerReady
$env:TIKTOK_API_IMAGE_TAG = $version.image_tag
& docker compose -f $composePath up -d --no-build
if ($LASTEXITCODE -ne 0) {
    throw 'Docker Compose could not start the pinned TikTok API service. Review the Docker output above.'
}

$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    try {
        $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 5
        if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) {
            Write-Host "TikTok API is healthy at $healthBaseUrl."
            exit 0
        }
    }
    catch {
        # Docker may still be starting the service.
    }
    Start-Sleep -Seconds 2
}

throw "TikTok API did not become healthy at $healthBaseUrl within 30 seconds. Run 'docker compose -f $composePath logs' for details."
