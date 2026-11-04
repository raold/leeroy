#Requires -Version 7.2
# This file is part of leeroy, released under the GNU GPL v2. See LICENSE.

<#
.SYNOPSIS
Builds the dojo's isolated core and checks what got linked.

.DESCRIPTION
Runs preflight.ps1 on the layout and stops unless it passes, then builds
roots.build (configured by configure.ps1, whose receipt must match the cache)
in RelWithDebInfo with a true global compiler cap: -m:N plus MultiToolTask,
EnforceProcessCountAcrossBuilds and CL_MPCount=N, as ac-build.sh does, and no
MSBuild node reuse. -Jobs defaults to 4 and may not exceed 16, the memory limit
of this machine's precompiled headers. While it builds it samples the cl.exe
processes descended from the build and reports the peak.

Never interrupt a build mid-compile: MSBuild records a file as built before
cl.exe finishes, so a killed build leaves stale or missing objects (CLAUDE.md,
build trap 4). Stopping this script does not stop the build it started.

After a successful build it verifies the link rather than trusting the exit
code: worldserver.exe and authserver.exe exist; modules.lib defines
Addmod_dojoScripts, AddSC_dojo_world and Addmod_playerbotsScripts;
worldserver.pdb names all three; worldserver.exe carries the build-variant tag
of the configured variant and not the other one; and the copied
mod_dojo.conf.dist keeps Dojo.Enable = 0. The receipt goes to
roots.build/dojo-build.json and roots.data/builds/. Nothing is started.

Exit status: 0 built and verified, 1 refused, failed or not verified.

.EXAMPLE
pwsh -NoProfile -File scripts/dojo/build.ps1 -Layout F:/azerothcore/dojo-runtime/instances/dojo-01/layout.json
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $Layout,
    [ValidateRange(1, 16)][int] $Jobs = 4
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:GIT_OPTIONAL_LOCKS = '0'

$DojoSymbols = [ordered]@{
    Addmod_dojoScripts       = '?Addmod_dojoScripts@@YAXXZ'
    AddSC_dojo_world         = '?AddSC_dojo_world@@YAXXZ'
    Addmod_playerbotsScripts = '?Addmod_playerbotsScripts@@YAXXZ'
}
$DojoVariantTags = @{
    training = 'mod-dojo build variant: training (AC_ENABLE_DOJO=ON)'
    disabled = 'mod-dojo build variant: disabled (AC_ENABLE_DOJO=OFF)'
}

function Stop-DojoBuild([string] $Message) {
    Write-Output "build: $Message"
    exit 1
}

function Get-DojoCacheValue([string] $Cache, [string] $Name) {
    if (-not (Test-Path -LiteralPath $Cache -PathType Leaf)) { return $null }
    foreach ($line in [System.IO.File]::ReadAllLines($Cache)) {
        if ($line -match "^$([regex]::Escape($Name)):[A-Z]+=(.*)$") { return $Matches[1] }
    }
    return $null
}

function Get-DojoHead([string] $Path) {
    $head = & git -c core.fsmonitor=false -C $Path rev-parse HEAD 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return "$head".Trim()
}

if (-not $IsWindows) { Stop-DojoBuild 'the dojo build is Windows-only (Visual Studio 2022)' }

# Which of the ASCII needles occur in a file, read in chunks so a large PDB never sits in memory whole.
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
public static class DojoScan
{
    public static string[] Find(string path, string[] needles)
    {
        var found = new List<string>();
        var patterns = new List<byte[]>();
        int longest = 1;
        foreach (var n in needles) { var b = Encoding.ASCII.GetBytes(n); patterns.Add(b); longest = Math.Max(longest, b.Length); }
        var buffer = new byte[(16 << 20) + longest];
        var done = new bool[needles.Length];
        using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete, 1 << 20))
        {
            int carry = 0;
            while (true)
            {
                int read = stream.Read(buffer, carry, buffer.Length - carry);
                if (read <= 0) break;
                int length = carry + read;
                var window = new ReadOnlySpan<byte>(buffer, 0, length);
                for (int i = 0; i < patterns.Count; i++)
                    if (!done[i] && window.IndexOf(patterns[i]) >= 0) { done[i] = true; found.Add(needles[i]); }
                carry = Math.Min(longest - 1, length);
                Buffer.BlockCopy(buffer, length - carry, buffer, 0, carry);
            }
        }
        return found.ToArray();
    }
}
'@

# --- the preflight must pass, and the tree must be the one configure.ps1 made ---

$preflight = Join-Path $PSScriptRoot 'preflight.ps1'
$reportText = & pwsh -NoProfile -NonInteractive -File $preflight -Layout $Layout -Json
$preflightCode = $LASTEXITCODE
$report = $null
try { $report = ($reportText -join "`n") | ConvertFrom-Json -AsHashtable } catch { $report = $null }
if ($null -eq $report -or $preflightCode -ne 0 -or -not $report.ok) {
    if ($null -ne $report) {
        foreach ($c in $report.checks) { if ($c.status -eq 'FAIL') { Write-Output "FAIL $($c.check): $($c.detail)" } }
    }
    Stop-DojoBuild "preflight did not pass (exit $preflightCode); nothing built"
}
if (@($report.checks | Where-Object { $_.check -eq 'build cache' -and $_.status -eq 'ok' }).Count -ne 1) {
    Stop-DojoBuild 'the build root is not configured from the dojo core; run configure.ps1 first'
}

$DojoLayout = Get-Content -LiteralPath $Layout -Raw | ConvertFrom-Json -AsHashtable
$build = [System.IO.Path]::GetFullPath($DojoLayout.roots.build).Replace('\', '/').TrimEnd('/')
$data = [System.IO.Path]::GetFullPath($DojoLayout.roots.data).Replace('\', '/').TrimEnd('/')
$cache = "$build/CMakeCache.txt"
$configured = $null
try { $configured = Get-Content -LiteralPath "$build/dojo-configure.json" -Raw | ConvertFrom-Json -AsHashtable } catch { $configured = $null }
if ($null -eq $configured -or $configured.kind -ne 'leeroy-dojo-configure') {
    Stop-DojoBuild "$build/dojo-configure.json is missing; run configure.ps1 first"
}
$variant = [string] $configured.variant
$flag = Get-DojoCacheValue $cache 'AC_ENABLE_DOJO'
if (-not $DojoVariantTags.ContainsKey($variant) -or $flag -ne $(if ($variant -eq 'training') { 'ON' } else { 'OFF' })) {
    Stop-DojoBuild "the cache says AC_ENABLE_DOJO=$flag but configure.ps1 recorded '$variant'; run configure.ps1 again"
}

# --- the build: a child in this console (no new window), its output in a log, its compilers counted ---

$logs = "$build/dojo-logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$stamp = [DateTime]::Now.ToString('yyyyMMdd_HHmmss')
$log = "$logs/build-$stamp-$variant.log"
$errLog = "$logs/build-$stamp-$variant.stderr.log"
$buildArgs = "--build `"$build`" --config RelWithDebInfo -- -m:$Jobs -nodeReuse:false " +
    "-p:UseMultiToolTask=true -p:EnforceProcessCountAcrossBuilds=true -p:CL_MPCount=$Jobs -v:minimal"
Write-Output "build: $variant variant, $Jobs jobs -> $log"
$started = [DateTime]::UtcNow
$child = Start-Process -FilePath 'cmake' -ArgumentList $buildArgs -NoNewWindow -PassThru `
    -RedirectStandardOutput $log -RedirectStandardError $errLog
$null = $child.Handle   # keeps the exit code readable after the process ends

$peak = 0
$peakAll = 0
$peakLink = 0
$samples = 0
while (-not $child.HasExited) {
    try {
        $procs = @(Get-CimInstance Win32_Process -Property ProcessId, ParentProcessId, Name -ErrorAction Stop)
        $children = @{}
        foreach ($p in $procs) {
            $parent = [int] $p.ParentProcessId
            if (-not $children.ContainsKey($parent)) { $children[$parent] = [System.Collections.Generic.List[object]]::new() }
            $children[$parent].Add($p)
        }
        $cl = 0
        $link = 0
        $queue = [System.Collections.Generic.Queue[int]]::new()
        $seen = [System.Collections.Generic.HashSet[int]]::new()
        $queue.Enqueue($child.Id)
        while ($queue.Count -gt 0) {
            $id = $queue.Dequeue()
            if (-not $seen.Add($id) -or -not $children.ContainsKey($id)) { continue }
            foreach ($c in $children[$id]) {
                if ($c.Name -eq 'cl.exe') { $cl++ } elseif ($c.Name -eq 'link.exe') { $link++ }
                $queue.Enqueue([int] $c.ProcessId)
            }
        }
        $all = @($procs | Where-Object { $_.Name -eq 'cl.exe' }).Count
        $peak = [Math]::Max($peak, $cl)
        $peakLink = [Math]::Max($peakLink, $link)
        $peakAll = [Math]::Max($peakAll, $all)
        $samples++
    } catch {
        # A sample that cannot be taken is skipped; the build itself is never touched.
    }
    Start-Sleep -Seconds 3
}
$child.WaitForExit()
$code = $child.ExitCode
$seconds = [math]::Round(([DateTime]::UtcNow - $started).TotalSeconds, 1)
$errors = @(Select-String -LiteralPath $log -Pattern 'error [A-Z]+[0-9]+' -ErrorAction SilentlyContinue).Count
Write-Output ("build: exit $code after {0:N0} s, {1} compiler errors; peak {2} cl.exe in this build ({3} on the machine), {4} link.exe, {5} samples" -f
    $seconds, $errors, $peak, $peakAll, $peakLink, $samples)

# --- what got linked ---

$bin = "$build/bin/RelWithDebInfo"
$problems = [System.Collections.Generic.List[string]]::new()
$artifacts = [ordered]@{}
foreach ($name in 'worldserver.exe', 'authserver.exe', 'worldserver.pdb') {
    $path = "$bin/$name"
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        $item = Get-Item -LiteralPath $path
        $artifacts[$name] = [ordered]@{
            bytes        = $item.Length
            modified_utc = $item.LastWriteTimeUtc.ToString('yyyy-MM-ddTHH:mm:ssZ')
            rebuilt      = $item.LastWriteTimeUtc -ge $started.AddSeconds(-5)
            sha256       = if ($name.EndsWith('.exe')) { (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() } else { $null }
        }
    } else {
        $problems.Add("$path is missing")
    }
}

$symbols = [ordered]@{}
$modulesLib = "$build/modules/RelWithDebInfo/modules.lib"
$dumpbin = $null
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
if (Test-Path -LiteralPath $vswhere) {
    $dumpbin = @(& $vswhere -latest -products * -find 'VC/Tools/MSVC/**/bin/Hostx64/x64/dumpbin.exe' 2>$null) | Select-Object -First 1
}
if ($code -eq 0) {
    if (-not $dumpbin -or -not (Test-Path -LiteralPath $modulesLib -PathType Leaf)) {
        $problems.Add("cannot list $modulesLib (dumpbin: '$dumpbin')")
    } else {
        $members = (& $dumpbin /nologo /linkermember:1 $modulesLib) -join "`n"
        foreach ($key in $DojoSymbols.Keys) {
            $symbols["modules.lib $key"] = $members.Contains($DojoSymbols[$key])
            if (-not $symbols["modules.lib $key"]) { $problems.Add("$modulesLib does not define $key") }
        }
    }
    if (Test-Path -LiteralPath "$bin/worldserver.pdb" -PathType Leaf) {
        $inPdb = [DojoScan]::Find("$bin/worldserver.pdb", [string[]] @($DojoSymbols.Values))
        foreach ($key in $DojoSymbols.Keys) {
            $symbols["worldserver.pdb $key"] = $DojoSymbols[$key] -in $inPdb
            if (-not $symbols["worldserver.pdb $key"]) { $problems.Add("worldserver.pdb does not name $key") }
        }
    }
    if (Test-Path -LiteralPath "$bin/worldserver.exe" -PathType Leaf) {
        $tags = [DojoScan]::Find("$bin/worldserver.exe", [string[]] @($DojoVariantTags.Values))
        $other = if ($variant -eq 'training') { 'disabled' } else { 'training' }
        $symbols["worldserver.exe has the $variant tag"] = $DojoVariantTags[$variant] -in $tags
        $symbols["worldserver.exe lacks the $other tag"] = $DojoVariantTags[$other] -notin $tags
        if (-not $symbols["worldserver.exe has the $variant tag"]) { $problems.Add("worldserver.exe lacks the $variant variant tag") }
        if (-not $symbols["worldserver.exe lacks the $other tag"]) { $problems.Add("worldserver.exe carries the $other variant tag") }
    }
    $dist = "$bin/configs/modules/mod_dojo.conf.dist"
    $distOff = (Test-Path -LiteralPath $dist -PathType Leaf) -and
        [bool] (Select-String -LiteralPath $dist -Pattern '^\s*Dojo\.Enable\s*=\s*0\s*$' -Quiet)
    $symbols['mod_dojo.conf.dist Dojo.Enable = 0'] = $distOff
    if (-not $distOff) { $problems.Add("$dist is missing or does not keep Dojo.Enable = 0") }
} else {
    $problems.Add("cmake --build exited $code")
}

$receipt = [ordered]@{
    kind            = 'leeroy-dojo-build'
    format          = 1
    created_utc     = $started.ToString('yyyy-MM-ddTHH:mm:ssZ')
    seconds         = $seconds
    instance        = $DojoLayout.instance
    variant         = $variant
    jobs            = $Jobs
    exit_code       = $code
    compiler_errors = $errors
    peak_cl         = $peak
    peak_cl_machine = $peakAll
    peak_link       = $peakLink
    samples         = $samples
    core            = Get-DojoHead $DojoLayout.roots.core
    playerbots      = Get-DojoHead $DojoLayout.roots.playerbots
    source          = Get-DojoHead $DojoLayout.roots.source
    artifacts       = $artifacts
    verified        = $symbols
    problems        = $problems
    log             = $log
}
$json = $receipt | ConvertTo-Json -Depth 5
$json | Set-Content -LiteralPath "$build/dojo-build.json" -Encoding utf8NoBOM
New-Item -ItemType Directory -Force -Path "$data/builds" | Out-Null
$json | Set-Content -LiteralPath "$data/builds/build-$stamp-$variant.json" -Encoding utf8NoBOM

if ($problems.Count -gt 0) {
    foreach ($p in $problems) { Write-Output "FAIL $p" }
    if ($code -ne 0) {
        if (Select-String -LiteralPath $log -Pattern 'C3859|C1076' -Quiet) {
            Write-Output 'build: C3859/C1076 is MSVC out of memory for the precompiled header; build again with fewer -Jobs'
        }
        Select-String -LiteralPath $log -Pattern 'error [A-Z]+[0-9]+: .*' | Select-Object -First 20 | ForEach-Object { $_.Line.Trim() }
    }
    Stop-DojoBuild "not verified; receipt $build/dojo-build.json"
}
foreach ($key in $symbols.Keys) { Write-Output "ok   $key`: $($symbols[$key])" }
Write-Output "build: ok ($variant variant linked and verified); receipt $data/builds/build-$stamp-$variant.json"
exit 0
