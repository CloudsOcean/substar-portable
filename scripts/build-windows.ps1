param(
    [string]$Python = '',
    [string]$E2EProjectId = '',
    [switch]$SkipTests,
    [switch]$SkipArchive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = if ($Python) { $Python } elseif ($env:SUBSTAR_BUILD_PYTHON) { $env:SUBSTAR_BUILD_PYTHON } else { 'python' }
$DistRoot = Join-Path $RepoRoot 'dist'
$BundleRoot = Join-Path $DistRoot 'Substar'
$EmbeddedPython = Join-Path $RepoRoot 'runtime\python\python.exe'
$EmbeddedFfmpeg = Join-Path $RepoRoot 'runtime\ffmpeg\bin'
$BundlePython = Join-Path $BundleRoot 'runtime\python\python.exe'
$ArchivePath = Join-Path $DistRoot 'Substar-Windows-x64-portable.zip'
$ChecksumPath = "$ArchivePath.sha256"
$TestBaseTemp = Join-Path $RepoRoot 'build\pytest-release'

function Assert-DirectChild([string]$Parent, [string]$Child) {
    $parentPath = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\')
    $childPath = [System.IO.Path]::GetFullPath($Child)
    if ([System.IO.Path]::GetDirectoryName($childPath).TrimEnd('\') -ne $parentPath) {
        throw "Unsafe build target outside direct parent: $childPath"
    }
}

function Copy-RequiredFile([string]$RelativePath) {
    $source = Join-Path $RepoRoot $RelativePath
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Missing required release file: $RelativePath" }
    $destination = Join-Path $BundleRoot $RelativePath
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Force
}

function Copy-RequiredDirectory([string]$RelativePath) {
    $source = Join-Path $RepoRoot $RelativePath
    if (-not (Test-Path -LiteralPath $source -PathType Container)) { throw "Missing required release directory: $RelativePath" }
    $destination = Join-Path $BundleRoot $RelativePath
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Recurse -Force
}

Push-Location -LiteralPath $RepoRoot
try {
    & $Python scripts/system_map.py --check
    if ($LASTEXITCODE -ne 0) { throw "System map validation failed with exit code $LASTEXITCODE." }
    & $Python scripts/build_windows_icon.py --check
    if ($LASTEXITCODE -ne 0) { throw "Icon validation failed with exit code $LASTEXITCODE." }
    if (-not $SkipTests) {
        & $Python -m pytest tests -q -p no:cacheprovider --basetemp $TestBaseTemp
        if ($LASTEXITCODE -ne 0) { throw "Unit tests failed with exit code $LASTEXITCODE." }
    }
    if (-not (Test-Path -LiteralPath $EmbeddedPython -PathType Leaf)) { throw 'runtime\python\python.exe is missing.' }
    foreach ($binary in @('ffmpeg.exe', 'ffprobe.exe')) {
        if (-not (Test-Path -LiteralPath (Join-Path $EmbeddedFfmpeg $binary) -PathType Leaf)) { throw "runtime\ffmpeg\bin\$binary is missing." }
    }

    New-Item -ItemType Directory -Path $DistRoot -Force | Out-Null
    Assert-DirectChild $DistRoot $BundleRoot
    if (Test-Path -LiteralPath $BundleRoot) { Remove-Item -LiteralPath $BundleRoot -Recurse -Force }
    New-Item -ItemType Directory -Path $BundleRoot -Force | Out-Null

    foreach ($directory in @('substar_core', 'web', 'prompts', 'schemas', 'assets', 'runtime\python', 'runtime\ffmpeg', 'docs\architecture')) {
        Copy-RequiredDirectory $directory
    }
    foreach ($file in @(
        'app.py', 'launcher.py', 'portable_manifest.json', 'requirements-release.txt',
        '启动_Substar.cmd', '停止_Substar.cmd', '只检测环境.cmd', '便携版说明.txt',
        'README.md', 'CHANGELOG.md', 'SECURITY.md', 'PRIVACY.md', 'THIRD_PARTY_NOTICES.md', 'LICENSE'
    )) { Copy-RequiredFile $file }

    $sourceCommit = if ($env:GITHUB_SHA -match '^[0-9a-fA-F]{40}$') {
        $env:GITHUB_SHA.ToLowerInvariant()
    } else {
        (& git -C $RepoRoot rev-parse HEAD).Trim().ToLowerInvariant()
    }
    if ($sourceCommit -notmatch '^[0-9a-f]{40}$') { throw 'Unable to resolve the release source commit.' }
    $bundleManifestPath = Join-Path $BundleRoot 'portable_manifest.json'
    $bundleManifest = Get-Content -Raw -LiteralPath $bundleManifestPath | ConvertFrom-Json
    $bundleManifest.source_commit = $sourceCommit
    $bundleManifestJson = $bundleManifest | ConvertTo-Json -Depth 20
    [System.IO.File]::WriteAllText(
        $bundleManifestPath,
        $bundleManifestJson + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false)
    )
    foreach ($worker in @(
        'run_transcription_worker.py', 'run_segmentation_worker.py',
        'run_semantic_segmentation.py', 'segmentation_support.py',
        'run_flash_map_pro_editor.py', 'run_global_planner_ab.py',
        'run_production_translation.py', 'run_editor_model_request.py'
    )) { Copy-RequiredFile "scripts\$worker" }

    foreach ($cache in Get-ChildItem -LiteralPath $BundleRoot -Directory -Recurse -Force | Where-Object { $_.Name -eq '__pycache__' }) {
        Remove-Item -LiteralPath $cache.FullName -Recurse -Force
    }
    foreach ($compiled in Get-ChildItem -LiteralPath $BundleRoot -File -Recurse -Force | Where-Object { $_.Extension -in @('.pyc', '.pyo') }) {
        Remove-Item -LiteralPath $compiled.FullName -Force
    }

    $requiredVersions = @{}
    foreach ($line in Get-Content -LiteralPath (Join-Path $RepoRoot 'requirements-release.txt')) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
        $name, $version = $trimmed -split '==', 2
        $requiredVersions[$name] = $version
    }
    $versionProbe = 'import importlib.metadata as m,json,sys; names=sys.argv[1:]; print(json.dumps({n:m.version(n) for n in names},sort_keys=True))'
    $installedJson = & $BundlePython -c $versionProbe @($requiredVersions.Keys)
    if ($LASTEXITCODE -ne 0) { throw 'Embedded Python dependency probe failed.' }
    $installed = $installedJson | ConvertFrom-Json
    foreach ($name in $requiredVersions.Keys) {
        if ([string]$installed.$name -ne [string]$requiredVersions[$name]) {
            throw "Embedded dependency mismatch for ${name}: expected $($requiredVersions[$name]), got $($installed.$name)"
        }
    }

    & $BundlePython launcher.py --smoke-import
    if ($LASTEXITCODE -ne 0) { throw 'Transparent launcher smoke import failed.' }
    $MaterialFixture = Join-Path $RepoRoot 'tests\fixtures\segmentation_material_v1.json'
    $MaterialSmokeOutput = Join-Path $RepoRoot 'build\transparent-segmentation-material-smoke'
    if (Test-Path -LiteralPath $MaterialSmokeOutput) { Remove-Item -LiteralPath $MaterialSmokeOutput -Recurse -Force }
    New-Item -ItemType Directory -Path $MaterialSmokeOutput -Force | Out-Null
    & $BundlePython (Join-Path $BundleRoot 'scripts\run_semantic_segmentation.py') $MaterialFixture `
        --output-dir $MaterialSmokeOutput --route semantic --grouping-model contract-smoke `
        --source-kind asr --source-asset-id transparent-release-smoke --validate-material-only
    if ($LASTEXITCODE -ne 0) { throw 'Transparent segmentation material contract failed.' }

    # Smoke imports may regenerate bytecode after the initial copy cleanup.
    foreach ($cache in Get-ChildItem -LiteralPath $BundleRoot -Directory -Recurse -Force | Where-Object { $_.Name -eq '__pycache__' }) {
        Remove-Item -LiteralPath $cache.FullName -Recurse -Force
    }
    foreach ($compiled in Get-ChildItem -LiteralPath $BundleRoot -File -Recurse -Force | Where-Object { $_.Extension -in @('.pyc', '.pyo') }) {
        Remove-Item -LiteralPath $compiled.FullName -Force
    }

    $BundleData = Join-Path $BundleRoot 'data'
    if (Test-Path -LiteralPath $BundleData) {
        Assert-DirectChild $BundleRoot $BundleData
        Remove-Item -LiteralPath $BundleData -Recurse -Force
    }
    if (Test-Path -LiteralPath $BundleData) { throw 'Transparent bundle contains user data after smoke cleanup.' }

    $BuildRoot = Join-Path $RepoRoot 'build'
    $PathSmokeParent = Join-Path $BuildRoot '透明 路径'
    $PathSmokeBundle = Join-Path $PathSmokeParent 'Substar'
    New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
    Assert-DirectChild $BuildRoot $PathSmokeParent
    if (Test-Path -LiteralPath $PathSmokeParent) { Remove-Item -LiteralPath $PathSmokeParent -Recurse -Force }
    New-Item -ItemType Directory -Path $PathSmokeParent -Force | Out-Null
    Copy-Item -LiteralPath $BundleRoot -Destination $PathSmokeBundle -Recurse -Force
    $PathSmokePython = Join-Path $PathSmokeBundle 'runtime\python\python.exe'
    & $PathSmokePython (Join-Path $PathSmokeBundle 'launcher.py') --smoke-import
    if ($LASTEXITCODE -ne 0) { throw 'Unicode/space path launcher smoke failed.' }
    & $PathSmokePython (Join-Path $PathSmokeBundle 'scripts\run_semantic_segmentation.py') $MaterialFixture `
        --output-dir (Join-Path $PathSmokeParent 'worker-output') --route semantic `
        --grouping-model contract-smoke --source-kind asr `
        --source-asset-id path-portability-smoke --validate-material-only
    if ($LASTEXITCODE -ne 0) { throw 'Unicode/space path Worker smoke failed.' }
    Remove-Item -LiteralPath $PathSmokeParent -Recurse -Force

    $productionRoots = @('app.py', 'launcher.py', 'substar_core', 'web', 'prompts', 'schemas', 'scripts')
    $hashes = [ordered]@{}
    foreach ($root in $productionRoots) {
        $source = Join-Path $RepoRoot $root
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            $hashes[$root.Replace('\','/')] = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
            continue
        }
        foreach ($file in Get-ChildItem -LiteralPath (Join-Path $BundleRoot $root) -File -Recurse | Sort-Object FullName) {
            $relative = [System.IO.Path]::GetRelativePath($BundleRoot, $file.FullName).Replace('\','/')
            $sourceFile = Join-Path $RepoRoot $relative
            if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) { throw "Bundle contains a production file absent from source: $relative" }
            $sourceHash = (Get-FileHash -LiteralPath $sourceFile -Algorithm SHA256).Hash.ToLowerInvariant()
            $bundleHash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($sourceHash -ne $bundleHash) { throw "Production file changed during packaging: $relative" }
            $hashes[$relative] = $bundleHash
        }
    }

    $verification = [ordered]@{
        schema_version = 'substar.release-verification.v1'
        package_layout = 'transparent-source-runtime'
        generated_at = [DateTimeOffset]::Now.ToString('o')
        python = (& $BundlePython -c 'import sys;print(sys.version.split()[0])')
        dependencies = $installed
        e2e_project_id = $E2EProjectId
        production_hashes = $hashes
        gates = [ordered]@{
            system_map = 'passed'; unit_tests = if ($SkipTests) { 'skipped' } else { 'passed' }
            launcher_smoke = 'passed'; segmentation_material_contract = 'passed'
            no_user_data = 'passed'; production_source_equivalence = 'passed'
            unicode_space_path = 'passed'
            real_video_e2e = if ($E2EProjectId) { 'passed' } else { 'not_run' }
        }
    }
    $verification | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $BundleRoot 'release-verification.json') -Encoding utf8

    $AllowedTopLevel = @(
        'app.py', 'launcher.py', 'substar_core', 'web', 'prompts', 'schemas', 'scripts', 'assets',
        'runtime', 'docs', 'portable_manifest.json', 'requirements-release.txt', 'release-verification.json',
        '启动_Substar.cmd', '停止_Substar.cmd', '只检测环境.cmd', '便携版说明.txt', 'README.md',
        'CHANGELOG.md', 'SECURITY.md', 'PRIVACY.md', 'THIRD_PARTY_NOTICES.md', 'LICENSE'
    )
    $unexpected = @(Get-ChildItem -LiteralPath $BundleRoot -Force | Where-Object { $_.Name -notin $AllowedTopLevel } | ForEach-Object Name)
    if ($unexpected.Count -gt 0) { throw "Unexpected bundle top-level entries: $($unexpected -join ', ')" }

    if (-not $SkipArchive) {
        if (Test-Path -LiteralPath $ArchivePath) { Remove-Item -LiteralPath $ArchivePath -Force }
        Compress-Archive -LiteralPath $BundleRoot -DestinationPath $ArchivePath -CompressionLevel Optimal
        $digest = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
        Set-Content -LiteralPath $ChecksumPath -Value "$digest  $(Split-Path $ArchivePath -Leaf)" -Encoding ascii
        Write-Host "Archived $ArchivePath"
        Write-Host "SHA256 $digest"
    }
    Write-Host "Built transparent bundle $BundleRoot"
}
finally { Pop-Location }
