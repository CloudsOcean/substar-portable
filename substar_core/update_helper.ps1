param([Parameter(Mandatory=$true)][string]$PlanPath, [Parameter(Mandatory=$true)][int]$LauncherPid)
$ErrorActionPreference = 'Stop'
$plan = Get-Content -LiteralPath $PlanPath -Raw -Encoding UTF8 | ConvertFrom-Json
$install = [IO.Path]::GetFullPath($plan.install).TrimEnd('\')
$stage = [IO.Path]::GetFullPath($plan.staged).TrimEnd('\')
$backup = [IO.Path]::GetFullPath($plan.backup).TrimEnd('\')
function ChildPath([string]$base, [string]$name) {
    $target = [IO.Path]::GetFullPath((Join-Path $base $name))
    if (-not $target.StartsWith($base + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe update path' }
    return $target
}
# Validate every recursive move/copy target before any mutation.
$allowed = @('app.py','launcher.py','substar_core','web','prompts','schemas','scripts','assets','runtime','docs',
 'portable_manifest.json','requirements-release.txt','release-verification.json','启动_Substar.cmd','停止_Substar.cmd',
 '只检测环境.cmd','便携版说明.txt','README.md','CHANGELOG.md','SECURITY.md','PRIVACY.md','THIRD_PARTY_NOTICES.md','LICENSE')
foreach ($name in $plan.roots) {
    if ($name -notin $allowed) { throw 'Unexpected update component' }
    $null = ChildPath $install $name
    $null = ChildPath $stage $name
    $null = ChildPath $backup $name
}
Wait-Process -Id $LauncherPid -ErrorAction SilentlyContinue -Timeout 60
if (Get-Process -Id $LauncherPid -ErrorAction SilentlyContinue) { throw 'Launcher did not exit' }
$moved = @()
$copied = @()
try {
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    foreach ($name in $plan.roots) {
        $target = ChildPath $install $name
        $source = ChildPath $stage $name
        if (Test-Path -LiteralPath $target) {
            Move-Item -LiteralPath $target -Destination (ChildPath $backup $name)
            $moved += $name
        }
        if (Test-Path -LiteralPath $source) {
            $copied += $name
            Copy-Item -LiteralPath $source -Destination $target -Recurse -Force
        }
    }
    $pythonExe = ChildPath $install 'runtime\python\python.exe'
    $launcherFile = ChildPath $install 'launcher.py'
    $check = Start-Process -FilePath $pythonExe -ArgumentList @('"'+$launcherFile+'"','--smoke-import') -WindowStyle Hidden -PassThru
    if (-not $check.WaitForExit(60000)) { $check.Kill(); throw 'New version validation timed out' }
    if ($check.ExitCode -ne 0) { throw 'New version validation failed' }
    @{status='succeeded';version=$plan.version;backup=$backup} | ConvertTo-Json | Set-Content -LiteralPath $plan.outcome -Encoding UTF8
} catch {
    $failure = $_.Exception.Message
    foreach ($name in $copied) {
        $target = ChildPath $install $name
        if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
    }
    foreach ($name in $moved) { Move-Item -LiteralPath (ChildPath $backup $name) -Destination (ChildPath $install $name) }
    @{status='rolled_back';error=$failure} | ConvertTo-Json | Set-Content -LiteralPath $plan.outcome -Encoding UTF8
} finally {
    Remove-Item -LiteralPath $PlanPath -ErrorAction SilentlyContinue
}
$pythonExe = ChildPath $install 'runtime\python\python.exe'
$launcherFile = ChildPath $install 'launcher.py'
Start-Process -FilePath $pythonExe -ArgumentList @('"'+$launcherFile+'"') -WorkingDirectory $install -WindowStyle Hidden
