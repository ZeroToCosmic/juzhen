[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string]$CommitSha,

    [ValidatePattern('^[0-9a-fA-F]{64}$')]
    [string]$ArchiveSha256,

    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-AtomicText {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    $temporaryPath = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        [System.IO.File]::WriteAllText(
            $temporaryPath,
            $Content,
            [System.Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $temporaryPath -Destination $Path -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
}

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

$repositoryUrl = 'https://github.com/Evil0ctal/Douyin_TikTok_Download_API'
$commit = $CommitSha.ToLowerInvariant()
$projectRoot = Split-Path -Parent $PSScriptRoot
$serviceDirectory = Join-Path $projectRoot 'services\tiktok_api'
$vendorDirectory = Join-Path $serviceDirectory 'vendor\Douyin_TikTok_Download_API'
$versionPath = Join-Path $serviceDirectory 'VERSION.json'
$sourceManifestPath = Join-Path $serviceDirectory 'SOURCE-MANIFEST.json'
$runtimeDirectory = Join-Path $serviceDirectory 'runtime'
$runtimeCookieConfig = Join-Path $runtimeDirectory 'tiktok_web_config.yaml'
$archiveUrl = "$repositoryUrl/archive/$commit.zip"
$imageTag = "local/tiktok-api:$commit"

Assert-DockerReady

if ((Test-Path -LiteralPath $vendorDirectory) -and -not $Force) {
    throw "Vendor source already exists at $vendorDirectory. Re-run with -Force only after reviewing the replacement commit."
}

$stagingDirectory = Join-Path ([System.IO.Path]::GetTempPath()) "tiktok-api-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $stagingDirectory | Out-Null

try {
    $archivePath = Join-Path $stagingDirectory 'source.zip'
    try {
        Invoke-WebRequest -Uri $archiveUrl -OutFile $archivePath -UseBasicParsing -TimeoutSec 60
    }
    catch {
        throw "Unable to download the requested GitHub archive ($archiveUrl). Check GitHub access, proxy settings, and the commit SHA."
    }

    $actualArchiveSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash.ToLowerInvariant()
    if ($ArchiveSha256 -and $actualArchiveSha256 -ne $ArchiveSha256.ToLowerInvariant()) {
        throw "Archive SHA-256 mismatch. Expected $ArchiveSha256 but downloaded $actualArchiveSha256."
    }

    $expandedDirectory = Join-Path $stagingDirectory 'expanded'
    Expand-Archive -LiteralPath $archivePath -DestinationPath $expandedDirectory
    $archiveRoots = @(Get-ChildItem -LiteralPath $expandedDirectory -Directory)
    if ($archiveRoots.Count -ne 1) {
        throw 'The GitHub archive did not contain exactly one source directory.'
    }

    $sourceDirectory = $archiveRoots[0].FullName
    if (-not (Test-Path -LiteralPath (Join-Path $sourceDirectory 'LICENSE'))) {
        throw 'The pinned archive does not contain LICENSE; installation stopped before changing the runtime.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $sourceDirectory 'Dockerfile'))) {
        throw 'The pinned archive does not contain Dockerfile; installation stopped before changing the runtime.'
    }

    $candidateSourceDirectory = Join-Path $stagingDirectory 'candidate-source'
    Move-Item -LiteralPath $sourceDirectory -Destination $candidateSourceDirectory
    $candidateSourceManifest = Get-SourceManifest -SourceDirectory $candidateSourceDirectory
    $sourceManifestContent = $candidateSourceManifest | ConvertTo-Json -Depth 4
    $versionContent = [ordered]@{
        commit_sha = $commit
        archive_sha256 = $actualArchiveSha256
        source_repository = $repositoryUrl
        license = 'Apache-2.0'
        license_file = 'LICENSE'
        source_manifest_file = 'SOURCE-MANIFEST.json'
        source_tree_sha256 = $candidateSourceManifest.tree_sha256
        source_file_count = $candidateSourceManifest.file_count
        image_tag = $imageTag
        installed_at_utc = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json

    $candidateRuntimeCookieConfig = Join-Path $stagingDirectory 'tiktok_web_config.yaml'
    $upstreamConfig = Join-Path $candidateSourceDirectory 'crawlers\tiktok\web\config.yaml'
    if (Test-Path -LiteralPath $upstreamConfig) {
        Copy-Item -LiteralPath $upstreamConfig -Destination $candidateRuntimeCookieConfig
    }
    else {
        Write-AtomicText -Path $candidateRuntimeCookieConfig -Content "headers:`n  Cookie: ''`n"
    }

    & docker build -t $imageTag $candidateSourceDirectory
    if ($LASTEXITCODE -ne 0) {
        throw "Docker image build failed for staged source $candidateSourceDirectory. The existing installation was not changed."
    }

    $vendorParent = Split-Path -Parent $vendorDirectory
    New-Item -ItemType Directory -Path $vendorParent -Force | Out-Null
    New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null

    $transactionId = [guid]::NewGuid().ToString('N')
    $previousVendorBackup = "$vendorDirectory.previous-$transactionId"
    $previousVersionBackup = "$versionPath.previous-$transactionId"
    $previousSourceManifestBackup = "$sourceManifestPath.previous-$transactionId"
    $hadVendor = Test-Path -LiteralPath $vendorDirectory
    $hadVersion = Test-Path -LiteralPath $versionPath
    $hadSourceManifest = Test-Path -LiteralPath $sourceManifestPath
    $runtimeCookieConfigCreated = $false
    $transactionSucceeded = $false
    try {
        if ($hadVendor) {
            Move-Item -LiteralPath $vendorDirectory -Destination $previousVendorBackup
        }
        if ($hadVersion) {
            Move-Item -LiteralPath $versionPath -Destination $previousVersionBackup
        }
        if ($hadSourceManifest) {
            Move-Item -LiteralPath $sourceManifestPath -Destination $previousSourceManifestBackup
        }

        Move-Item -LiteralPath $candidateSourceDirectory -Destination $vendorDirectory
        if (-not (Test-Path -LiteralPath $runtimeCookieConfig)) {
            Move-Item -LiteralPath $candidateRuntimeCookieConfig -Destination $runtimeCookieConfig
            $runtimeCookieConfigCreated = $true
        }
        Write-AtomicText -Path $sourceManifestPath -Content $sourceManifestContent
        Write-AtomicText -Path $versionPath -Content $versionContent
        $transactionSucceeded = $true
    }
    catch {
        Write-Warning 'Restoring the previous pinned TikTok API installation after a failed replacement.'
        if (Test-Path -LiteralPath $vendorDirectory) {
            Remove-Item -LiteralPath $vendorDirectory -Recurse -Force
        }
        if ($hadVendor -and (Test-Path -LiteralPath $previousVendorBackup)) {
            Move-Item -LiteralPath $previousVendorBackup -Destination $vendorDirectory
        }
        if (Test-Path -LiteralPath $versionPath) {
            Remove-Item -LiteralPath $versionPath -Force
        }
        if ($hadVersion -and (Test-Path -LiteralPath $previousVersionBackup)) {
            Move-Item -LiteralPath $previousVersionBackup -Destination $versionPath
        }
        if (Test-Path -LiteralPath $sourceManifestPath) {
            Remove-Item -LiteralPath $sourceManifestPath -Force
        }
        if ($hadSourceManifest -and (Test-Path -LiteralPath $previousSourceManifestBackup)) {
            Move-Item -LiteralPath $previousSourceManifestBackup -Destination $sourceManifestPath
        }
        if ($runtimeCookieConfigCreated -and (Test-Path -LiteralPath $runtimeCookieConfig)) {
            Remove-Item -LiteralPath $runtimeCookieConfig -Force
        }
        throw
    }
    finally {
        if ($transactionSucceeded) {
            foreach ($backupPath in @($previousVendorBackup, $previousVersionBackup, $previousSourceManifestBackup)) {
                if (Test-Path -LiteralPath $backupPath) {
                    Remove-Item -LiteralPath $backupPath -Recurse -Force
                }
            }
        }
    }

    Write-Host "Installed pinned TikTok API source $commit with archive SHA-256 $actualArchiveSha256."
}
finally {
    if (Test-Path -LiteralPath $stagingDirectory) {
        Remove-Item -LiteralPath $stagingDirectory -Recurse -Force
    }
}
