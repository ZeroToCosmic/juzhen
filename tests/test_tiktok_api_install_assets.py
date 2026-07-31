from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_compose_is_loopback_only_pinned_and_mounts_ignored_cookie_config():
    compose = (ROOT / "services" / "tiktok_api" / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "127.0.0.1:53281:80" in compose
    assert "latest" not in compose.lower()
    assert "./runtime/tiktok_web_config.yaml" in compose
    assert "/app/crawlers/tiktok/web/config.yaml:ro" in compose
    assert "services/tiktok_api/vendor/" in ignored
    assert "services/tiktok_api/runtime/" in ignored


def test_installer_requires_a_full_40_character_commit_sha():
    installer = (ROOT / "scripts" / "install_tiktok_api.ps1").read_text(encoding="utf-8")

    assert "[Parameter(Mandatory = $true)]" in installer
    assert "[ValidatePattern('^[0-9a-fA-F]{40}$')]" in installer
    assert "[string]$CommitSha" in installer


def test_start_script_refuses_vendor_content_that_differs_from_external_manifest():
    installer = (ROOT / "scripts" / "install_tiktok_api.ps1").read_text(encoding="utf-8")
    starter = (ROOT / "scripts" / "start_tiktok_api.ps1").read_text(encoding="utf-8")

    assert "SOURCE-MANIFEST.json" in installer
    assert "source_tree_sha256" in installer
    assert "function Get-SourceManifest" in starter
    assert "$actualSourceManifest.tree_sha256 -ne $sourceManifest.tree_sha256" in starter
    assert "Vendor source contents do not match the install-time manifest" in starter
    assert ".tiktok-api-source.json" not in starter


def test_forced_replacement_builds_staged_source_before_transactional_swap():
    installer = (ROOT / "scripts" / "install_tiktok_api.ps1").read_text(encoding="utf-8")

    assert "& docker build -t $imageTag $candidateSourceDirectory" in installer
    assert "$previousVendorBackup" in installer
    assert "Move-Item -LiteralPath $vendorDirectory -Destination $previousVendorBackup" in installer
    assert "Move-Item -LiteralPath $candidateSourceDirectory -Destination $vendorDirectory" in installer
    assert "Restoring the previous pinned TikTok API installation" in installer
    assert installer.index("& docker build -t $imageTag $candidateSourceDirectory") < installer.index(
        "Move-Item -LiteralPath $vendorDirectory -Destination $previousVendorBackup"
    )


def test_both_source_manifest_builders_include_hidden_and_system_files():
    for script_name in ("install_tiktok_api.ps1", "start_tiktok_api.ps1"):
        script = (ROOT / "scripts" / script_name).read_text(encoding="utf-8")

        assert "Get-ChildItem -LiteralPath $SourceDirectory -File -Recurse -Force" in script


def test_start_script_refuses_invalid_or_inconsistent_manifest_file_counts():
    starter = (ROOT / "scripts" / "start_tiktok_api.ps1").read_text(encoding="utf-8")

    assert "$versionFileCount -notmatch '^[1-9][0-9]*$'" in starter
    assert "$sourceManifestFileCount -notmatch '^[1-9][0-9]*$'" in starter
    assert "$versionFileCount -ne $sourceManifestFileCount" in starter
    assert "source_file_count is invalid or does not match SOURCE-MANIFEST.json" in starter
