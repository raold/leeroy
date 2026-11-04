#Requires -Version 7.2
# This file is part of leeroy, released under the GNU GPL v2. See LICENSE.

<#
.SYNOPSIS
Configures the dojo's own CMake build tree for the isolated core.

.DESCRIPTION
Runs preflight.ps1 on the layout and stops unless it passes, then configures
roots.build from roots.core (whose modules are exactly mod-playerbots and
mod-dojo, linked to the isolated checkouts) with Visual Studio 2022, x64, the
RelWithDebInfo configuration only, static scripts and modules, both servers
and no tools (the dojo reads the live server's extracted Data read-only).
Boost, MySQL and OpenSSL come from the layout's "toolchain" section when it
names them.

A missing build tree is configured fresh. An existing one is reconfigured in
place only when its cache names roots.core as its source and was made by the
same generator; this script never copies, moves or edits a cache, and never
touches the live build. The production ac-build.sh is not used.

-Variant disabled (the default) builds what any core containing mod-dojo would
get: Dojo.Enable = 1 is refused at start. -Variant training sets
AC_ENABLE_DOJO=ON, which mod-dojo.cmake allows only beside mod-playerbots.

Afterwards it checks that the generated script loader registers exactly
mod-playerbots and mod-dojo and that the variant reached the cache and the
mod-dojo runtime sources, then writes roots.build/dojo-configure.json. The
CMake output goes to roots.build/dojo-logs/. Nothing is built or started.

Exit status: 0 configured, 1 refused or failed.

.EXAMPLE
pwsh -NoProfile -File scripts/dojo/configure.ps1 -Layout F:/azerothcore/dojo-runtime/instances/dojo-01/layout.json

.EXAMPLE
pwsh -NoProfile -File scripts/dojo/configure.ps1 -Layout layout.json -Variant training
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $Layout,
    [ValidateSet('disabled', 'training')][string] $Variant = 'disabled'
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:GIT_OPTIONAL_LOCKS = '0'

$DojoGenerator = 'Visual Studio 17 2022'
$DojoModules = @('mod_dojo', 'mod_playerbots')

function Stop-DojoConfigure([string] $Message) {
    Write-Output "configure: $Message"
    exit 1
}

function Get-DojoCacheValue([string] $Cache, [string] $Name) {
    if (-not (Test-Path -LiteralPath $Cache -PathType Leaf)) { return $null }
    foreach ($line in [System.IO.File]::ReadLines($Cache)) {
        if ($line -match "^$([regex]::Escape($Name)):[A-Z]+=(.*)$") { return $Matches[1] }
    }
    return $null
}

function Get-DojoHead([string] $Path) {
    $head = & git -c core.fsmonitor=false -C $Path rev-parse HEAD 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return "$head".Trim()
}

if (-not $IsWindows) { Stop-DojoConfigure 'the dojo build is Windows-only (Visual Studio 2022)' }

# --- the preflight must pass first ---

$preflight = Join-Path $PSScriptRoot 'preflight.ps1'
$reportText = & pwsh -NoProfile -NonInteractive -File $preflight -Layout $Layout -Json
$preflightCode = $LASTEXITCODE
$report = $null
try { $report = ($reportText -join "`n") | ConvertFrom-Json -AsHashtable } catch { $report = $null }
if ($null -eq $report -or $preflightCode -ne 0 -or -not $report.ok) {
    if ($null -ne $report) {
        foreach ($c in $report.checks) { if ($c.status -eq 'FAIL') { Write-Output "FAIL $($c.check): $($c.detail)" } }
    }
    Stop-DojoConfigure "preflight did not pass (exit $preflightCode); nothing configured"
}

$DojoLayout = Get-Content -LiteralPath $Layout -Raw | ConvertFrom-Json -AsHashtable
$core = [System.IO.Path]::GetFullPath($DojoLayout.roots.core).Replace('\', '/').TrimEnd('/')
$build = [System.IO.Path]::GetFullPath($DojoLayout.roots.build).Replace('\', '/').TrimEnd('/')
$toolchain = if ($DojoLayout.Contains('toolchain') -and $DojoLayout.toolchain -is [System.Collections.IDictionary]) { $DojoLayout.toolchain } else { @{} }
$boost = if ($toolchain.Contains('boost')) { [string] $toolchain.boost } else { $null }
if (-not $boost -or -not (Test-Path -LiteralPath (Join-Path $boost 'boost') -PathType Container)) {
    Stop-DojoConfigure "layout toolchain.boost ('$boost') is not a Boost root"
}
$boost = $boost.Replace('\', '/').TrimEnd('/')

# --- an existing tree is reused only when it is this core's, from this generator ---

$cache = "$build/CMakeCache.txt"
$fresh = -not (Test-Path -LiteralPath $cache -PathType Leaf)
if (-not $fresh) {
    $generator = Get-DojoCacheValue $cache 'CMAKE_GENERATOR'
    if ($generator -ne $DojoGenerator) {
        Stop-DojoConfigure "$cache was made by '$generator', not $DojoGenerator; configure a fresh build root instead"
    }
}

$cmakeArgs = [System.Collections.Generic.List[string]]::new()
$cmakeArgs.AddRange([string[]] @('-S', $core, '-B', $build))
if ($fresh) { $cmakeArgs.AddRange([string[]] @('-G', $DojoGenerator, '-A', 'x64')) }
$cmakeArgs.AddRange([string[]] @(
        '-DCMAKE_CONFIGURATION_TYPES=RelWithDebInfo',
        '-DSCRIPTS=static', '-DMODULES=static', '-DAPPS_BUILD=all', '-DTOOLS_BUILD=none',
        '-DBUILD_TESTING=OFF', '-DUSE_COREPCH=ON', '-DUSE_SCRIPTPCH=ON',
        "-DCMAKE_INSTALL_PREFIX=$build/install",
        "-DBOOST_ROOT=$boost",
        "-DAC_ENABLE_DOJO=$(if ($Variant -eq 'training') { 'ON' } else { 'OFF' })"))
if ($toolchain.Contains('mysql') -and $toolchain.mysql) {
    $mysql = ([string] $toolchain.mysql).Replace('\', '/').TrimEnd('/')
    $cmakeArgs.AddRange([string[]] @("-DMYSQL_INCLUDE_DIR=$mysql/include", "-DMYSQL_LIBRARY=$mysql/lib/libmysql.lib"))
}
if ($toolchain.Contains('openssl') -and $toolchain.openssl) {
    $cmakeArgs.Add("-DOPENSSL_ROOT_DIR=$(([string] $toolchain.openssl).Replace('\', '/').TrimEnd('/'))")
}

# The user environment may name another Boost (a stale BOOST_ROOT); the layout's wins for this process tree.
$env:BOOST_ROOT = $boost
$env:Boost_ROOT = $boost

$logs = "$build/dojo-logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$stamp = [DateTime]::Now.ToString('yyyyMMdd_HHmmss')
$log = "$logs/configure-$stamp-$Variant.log"
Write-Output "configure: $Variant variant, $(if ($fresh) { 'fresh tree' } else { 'existing tree' }) $build -> $log"
$started = [DateTime]::UtcNow
[string[]] $argv = $cmakeArgs.ToArray()
& cmake @argv *> $log
$code = $LASTEXITCODE
if ($code -ne 0) {
    Get-Content -LiteralPath $log -Tail 30
    Stop-DojoConfigure "cmake exited $code; see $log"
}

# --- what the configure produced ---

$problems = [System.Collections.Generic.List[string]]::new()
$cacheSource = Get-DojoCacheValue $cache 'CMAKE_HOME_DIRECTORY'
$cacheSourceFull = if ($cacheSource) { [System.IO.Path]::GetFullPath($cacheSource).Replace('\', '/').TrimEnd('/') } else { '' }
if (-not [string]::Equals($cacheSourceFull, $core, [System.StringComparison]::OrdinalIgnoreCase)) {
    $problems.Add("the cache's source is '$cacheSource', not $core")
}
$flag = Get-DojoCacheValue $cache 'AC_ENABLE_DOJO'
$wantFlag = if ($Variant -eq 'training') { 'ON' } else { 'OFF' }
if ($flag -ne $wantFlag) { $problems.Add("AC_ENABLE_DOJO is '$flag' in the cache, not $wantFlag") }

$loader = "$build/modules/gen_scriptloader/static/ModulesLoader.cpp"
$registered = @()
if (Test-Path -LiteralPath $loader -PathType Leaf) {
    $registered = @([regex]::Matches((Get-Content -LiteralPath $loader -Raw), '(?m)^\s+Add(\w+)Scripts\(\);') |
            ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)
    if (($registered -join ',') -ne (($DojoModules | Sort-Object) -join ',')) {
        $problems.Add("the script loader registers [$($registered -join ', ')], not [$($DojoModules -join ', ')]")
    }
} else {
    $problems.Add("no generated script loader at $loader")
}

$project = "$build/modules/modules.vcxproj"
$wantDefine = "DOJO_TRAINING_BUILD=$(if ($Variant -eq 'training') { 1 } else { 0 })"
$otherDefine = "DOJO_TRAINING_BUILD=$(if ($Variant -eq 'training') { 0 } else { 1 })"
$projectText = if (Test-Path -LiteralPath $project -PathType Leaf) { Get-Content -LiteralPath $project -Raw } else { '' }
if (-not $projectText.Contains($wantDefine) -or $projectText.Contains($otherDefine)) {
    $problems.Add("$project does not give the mod-dojo runtime sources $wantDefine")
}

if ($problems.Count -gt 0) {
    foreach ($p in $problems) { Write-Output "FAIL $p" }
    Stop-DojoConfigure "the configured tree is not the dojo's; see $log"
}

$receipt = [ordered]@{
    kind        = 'leeroy-dojo-configure'
    format      = 1
    created_utc = $started.ToString('yyyy-MM-ddTHH:mm:ssZ')
    seconds     = [math]::Round(([DateTime]::UtcNow - $started).TotalSeconds, 1)
    layout      = [System.IO.Path]::GetFullPath($Layout).Replace('\', '/')
    instance    = $DojoLayout.instance
    variant     = $Variant
    fresh       = $fresh
    core        = [ordered]@{ path = $core; head = Get-DojoHead $core }
    playerbots  = [ordered]@{ path = $DojoLayout.roots.playerbots; head = Get-DojoHead $DojoLayout.roots.playerbots }
    source      = [ordered]@{ path = $DojoLayout.roots.source; head = Get-DojoHead $DojoLayout.roots.source }
    build       = $build
    registered  = $registered
    cmake       = "$(& cmake --version | Select-Object -First 1)".Trim()
    arguments   = $cmakeArgs
    log         = $log
}
$receipt | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath "$build/dojo-configure.json" -Encoding utf8NoBOM
Write-Output "configure: ok ($Variant; loader registers $($registered -join ', ')); wrote $build/dojo-configure.json"
exit 0
