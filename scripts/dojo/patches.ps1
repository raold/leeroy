#Requires -Version 7.2
# This file is part of leeroy, released under the GNU GPL v2. See LICENSE.

<#
.SYNOPSIS
Verifies, or applies, the dojo's mod-playerbots patch series in the isolated playerbots checkout.

.DESCRIPTION
The dojo's own changes to mod-playerbots (the external-controller hook of plan
Task 6) ship in this repository as a reviewed patch series,
patches/dojo/mod-playerbots/*.patch, exported with git format-patch from the
isolated checkout's commits on top of its provenance baseline. They never go
into patches/mod-playerbots/, which scripts/ac-snapshot.sh owns.

Verification compares the series with the layout's roots.playerbots: the
checkout's tracked files must be clean, and its commits since the playerbots
baseline in the provenance manifest must be exactly the uninstalled production
patches followed by the dojo series, in order, by
stable patch id (git patch-id --stable, which ignores line numbers and
whitespace). -Apply first applies the missing tail of the series with
git am --3way when the checkout's commits are a prefix of it, so a fresh
checkout at the baseline gets all of it; it never resets, rewrites or drops a
commit, and refuses anything else.

configure.ps1 and build.ps1 run the verification and stop unless it passes.
The only program this script runs is git.

Exit status: 0 verified, 1 refused or different, 2 unreadable layout or provenance.

.EXAMPLE
pwsh -NoProfile -File scripts/dojo/patches.ps1 -Layout F:/azerothcore/dojo-runtime/instances/dojo-01/layout.json

.EXAMPLE
pwsh -NoProfile -File scripts/dojo/patches.ps1 -Layout layout.json -Apply -Json
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $Layout,
    [switch] $Apply,
    [switch] $Json
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:GIT_OPTIONAL_LOCKS = '0'

$DojoSeries = 'patches/dojo/mod-playerbots'

function Get-DojoFull([string] $Path) {
    return [System.IO.Path]::GetFullPath($Path).Replace('\', '/').TrimEnd('/')
}

function Invoke-DojoGit([string] $Repo, [string[]] $GitArgs) {
    $out = & git -c core.fsmonitor=false -C $Repo @GitArgs 2>&1
    return [pscustomobject]@{ code = $LASTEXITCODE; text = (@($out) -join "`n") }
}

# The stable patch id of one format-patch file, or $null.
function Get-DojoPatchId([string] $Repo, [string] $File) {
    $out = Get-Content -LiteralPath $File -Raw | & git -c core.fsmonitor=false -C $Repo patch-id --stable 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    $line = @($out | Where-Object { $_ }) | Select-Object -First 1
    if (-not $line) { return $null }
    return ("$line".Trim() -split '\s+')[0]
}

# Snapshot exports use either Git's default context or the one-line context
# needed when upstream has reordered adjacent loader declarations. Context is
# not a code change, but Git includes it in a stable patch id.
function Get-DojoCommitPatchId([string] $Repo, [string] $Commit, [int] $Context) {
    $out = & git -c core.fsmonitor=false -C $Repo show --format=email --binary "--unified=$Context" $Commit |
        & git -c core.fsmonitor=false -C $Repo patch-id --stable
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
    return ("$out".Trim() -split '\s+')[0]
}

function Get-DojoHistoryIds([string] $Repo, [string] $File) {
    # Stream large histories without materializing all diffs or feeding each
    # line through PowerShell's object pipeline. Drain both output pipes while
    # writing input, and check the actual Git exit code.
    $start = [System.Diagnostics.ProcessStartInfo]::new('git')
    foreach ($arg in @('-c', 'core.fsmonitor=false', '-C', $Repo, 'patch-id', '--stable')) { $start.ArgumentList.Add($arg) }
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $start
    $inputStream = $null
    try {
        if (-not $process.Start()) { throw 'cannot start Git patch reader' }
        $outputTask = $process.StandardOutput.ReadToEndAsync()
        $errorTask = $process.StandardError.ReadToEndAsync()
        $inputStream = [System.IO.File]::OpenRead($File)
        $inputStream.CopyTo($process.StandardInput.BaseStream)
        $process.StandardInput.Close()
        $process.WaitForExit()
        $output = $outputTask.GetAwaiter().GetResult()
        $null = $errorTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) { throw 'Git patch reader failed' }
        return @($output -split "`n" | Where-Object { $_.Trim() } | ForEach-Object { ($_.Trim() -split '\s+')[0] })
    } finally {
        if ($inputStream) { $inputStream.Dispose() }
        if ($process.Id -and -not $process.HasExited) { $process.Kill(); $process.WaitForExit() }
        $process.Dispose()
    }
}

$result = [ordered]@{ kind = 'leeroy-dojo-patches'; format = 1; ok = $false; series = @(); commits = @(); applied = @();
    problems = [System.Collections.Generic.List[string]]::new() }

function Stop-DojoPatches([int] $Code) {
    if ($Json) {
        $result | ConvertTo-Json -Depth 5
    } else {
        foreach ($p in $result.problems) { Write-Output "FAIL $p" }
        if ($result.ok) {
            Write-Output "ok dojo patches: $(@($result.series).Count) in $DojoSeries match $($result.playerbots) since $($result.baseline.Substring(0, 12))"
        }
    }
    exit $Code
}

try {
    $DojoLayout = Get-Content -LiteralPath $Layout -Raw | ConvertFrom-Json -AsHashtable
    $source = Get-DojoFull $DojoLayout.roots.source
    $playerbots = Get-DojoFull $DojoLayout.roots.playerbots
    $provenancePath = if ($DojoLayout.Contains('provenance')) { [string] $DojoLayout.provenance } else { "$($DojoLayout.roots.data)/manifests/provenance.json" }
    $provenance = Get-Content -LiteralPath $provenancePath -Raw | ConvertFrom-Json -AsHashtable
    $baseline = [string] $provenance.baseline.playerbots.head
} catch {
    $result.problems.Add("cannot read the layout or its provenance manifest: $($_.Exception.Message)")
    Stop-DojoPatches 2
}
if ($baseline -notmatch '^[0-9a-f]{40}$') {
    $result.problems.Add('the provenance manifest records no playerbots baseline commit')
    Stop-DojoPatches 2
}
$result.baseline = $baseline
$result.playerbots = $playerbots
$seriesDir = "$source/$DojoSeries"
$files = @(if (Test-Path -LiteralPath $seriesDir -PathType Container) {
        Get-ChildItem -LiteralPath $seriesDir -Filter '*.patch' -File | Sort-Object Name
    })
if ($files.Count -eq 0) {
    $result.problems.Add("no patch series in $seriesDir")
    Stop-DojoPatches 1
}

$status = Invoke-DojoGit $playerbots @('status', '--porcelain', '--untracked-files=no')
if ($status.code -ne 0) {
    $result.problems.Add("$playerbots is not a git checkout")
    Stop-DojoPatches 1
}
if ($status.text.Trim()) {
    $result.problems.Add("$playerbots has uncommitted changes to tracked files; the series is what builds")
    Stop-DojoPatches 1
}
if ((Invoke-DojoGit $playerbots @('merge-base', '--is-ancestor', $baseline, 'HEAD')).code -ne 0) {
    $result.problems.Add("$playerbots does not descend from the baseline $($baseline.Substring(0, 12))")
    Stop-DojoPatches 1
}
$merges = Invoke-DojoGit $playerbots @('rev-list', '--merges', "$baseline..HEAD")
if ($merges.code -ne 0 -or $merges.text.Trim()) {
    $result.problems.Add('candidate history must be a linear patch series')
    Stop-DojoPatches 1
}

# Keep the production provenance immutable when testing an unreleased module
# candidate. Only the committed release series may precede the dojo hook; an
# arbitrary extra commit still fails the exact ordered comparison below.
$productionDir = "$source/patches/mod-playerbots"
$production = @(if (Test-Path -LiteralPath $productionDir -PathType Container) {
        Get-ChildItem -LiteralPath $productionDir -Filter '*.patch' -File | Sort-Object Name
    })
$missingProduction = [System.Collections.Generic.List[object]]::new()
$productionIds = [System.Collections.Generic.HashSet[string]]::new()
$productionNumbers = [System.Collections.Generic.HashSet[string]]::new()
$baselineIds = $null
$result.production = @()
foreach ($f in $production) {
    if ($f.LinkType -or $f.Name -notmatch '^([0-9]{4})-.+\.patch$' -or
        -not $productionNumbers.Add($Matches[1])) {
        $result.problems.Add('invalid or duplicate production patch filename')
        Stop-DojoPatches 1
    }
    $first = Get-Content -LiteralPath $f.FullName -TotalCount 1
    if ($first -notmatch '^From ([0-9a-f]{40}) Mon Sep 17 00:00:00 2001$') {
        $result.problems.Add("$($f.Name) is not a format-patch entry")
        Stop-DojoPatches 1
    }
    $original = $Matches[1]
    $id = Get-DojoPatchId $playerbots $f.FullName
    if (-not $id -or -not $productionIds.Add($id)) {
        $result.problems.Add('unreadable or duplicate production patch content')
        Stop-DojoPatches 1
    }
    $known = (Invoke-DojoGit $playerbots @('merge-base', '--is-ancestor', $original, $baseline)).code -eq 0
    if ($known) {
        if ((Get-DojoCommitPatchId $playerbots $original 3) -cne $id -and
            (Get-DojoCommitPatchId $playerbots $original 1) -cne $id) {
            $result.problems.Add("$($f.Name) differs from its installed commit")
            Stop-DojoPatches 1
        }
    } else {
        if ($null -eq $baselineIds) {
            $baselineIds = [System.Collections.Generic.HashSet[string]]::new()
            $historyFile = Join-Path ([System.IO.Path]::GetTempPath()) "dojo-patch-history-$PID-$([guid]::NewGuid().ToString('N'))"
            try {
                foreach ($context in @(3, 1)) {
                    $logged = Invoke-DojoGit $playerbots @('log', '--no-merges', '-p', '--binary', "--unified=$context", "--output=$historyFile", $baseline)
                    if ($logged.code -ne 0) { throw 'cannot read baseline history' }
                    foreach ($historyId in (Get-DojoHistoryIds $playerbots $historyFile)) { $null = $baselineIds.Add($historyId) }
                }
            } catch {
                $result.problems.Add('cannot identify installed production patches')
            } finally {
                if (Test-Path -LiteralPath $historyFile -PathType Leaf) { Remove-Item -LiteralPath $historyFile -Force }
            }
            if ($result.problems.Count -gt 0) { Stop-DojoPatches 1 }
        }
        $known = $baselineIds.Contains($id)
    }
    if ($known -and $missingProduction.Count -gt 0) {
        $result.problems.Add('installed production patches are not an ordered prefix')
        Stop-DojoPatches 1
    }
    $result.production += [ordered]@{ file = $f.Name; patch_id = $id; installed = $known }
    if (-not $known) { $missingProduction.Add($f) }
}
$files = @($missingProduction) + $files

$seriesIds = [System.Collections.Generic.List[string]]::new()
$result.series = @(foreach ($f in $files) {
        $id = Get-DojoPatchId $playerbots $f.FullName
        if (-not $id) { $result.problems.Add("$($f.Name) is not a patch git can read") }
        $seriesIds.Add([string] $id)
        [ordered]@{ file = $f.Name; patch_id = $id }
    })
if ($result.problems.Count -gt 0) { Stop-DojoPatches 1 }

function Get-DojoCommitIds {
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) "dojo-patches-$PID-$([guid]::NewGuid().ToString('N'))"
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        $out = Invoke-DojoGit $playerbots @('format-patch', '--quiet', '--no-signature', '-o', $tmp, "$baseline..HEAD")
        if ($out.code -ne 0) { throw "git format-patch failed: $($out.text)" }
        $commits = @((Invoke-DojoGit $playerbots @('rev-list', '--reverse', "$baseline..HEAD")).text -split "`n" | Where-Object { $_ })
        $patches = @(Get-ChildItem -LiteralPath $tmp -Filter '*.patch' -File | Sort-Object Name)
        $list = [System.Collections.Generic.List[object]]::new()
        for ($i = 0; $i -lt $patches.Count; $i++) {
            $commit = if ($i -lt $commits.Count) { $commits[$i] } else { '' }
            $id = Get-DojoPatchId $playerbots $patches[$i].FullName
            $context = 3
            if ($i -lt $seriesIds.Count -and $id -cne $seriesIds[$i] -and $commit) {
                $shortId = Get-DojoCommitPatchId $playerbots $commit 1
                if ($shortId -ceq $seriesIds[$i]) { $id = $shortId; $context = 1 }
            }
            $list.Add([ordered]@{ commit = $commit; patch_id = $id; context = $context })
        }
        return , $list
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$commits = Get-DojoCommitIds
$isPrefix = $commits.Count -le $seriesIds.Count
for ($i = 0; $isPrefix -and $i -lt $commits.Count; $i++) { $isPrefix = $commits[$i].patch_id -eq $seriesIds[$i] }

if ($Apply -and $isPrefix -and $commits.Count -lt $seriesIds.Count) {
    $rest = @($files | Select-Object -Skip $commits.Count | ForEach-Object { $_.FullName })
    $am = Invoke-DojoGit $playerbots (@('am', '--3way', '--quiet') + $rest)
    if ($am.code -ne 0) {
        $null = Invoke-DojoGit $playerbots @('am', '--abort')
        $result.problems.Add("git am could not apply the series: $($am.text)")
        Stop-DojoPatches 1
    }
    $result.applied = @($files | Select-Object -Skip $commits.Count | ForEach-Object { $_.Name })
    $commits = Get-DojoCommitIds
}

$result.commits = @($commits)
$same = $commits.Count -eq $seriesIds.Count
for ($i = 0; $same -and $i -lt $commits.Count; $i++) { $same = $commits[$i].patch_id -eq $seriesIds[$i] }
if (-not $same) {
    $hint = if ($isPrefix -and -not $Apply) { '; -Apply applies the missing tail' } else { '' }
    $result.problems.Add("$playerbots carries $($commits.Count) commits since $($baseline.Substring(0, 12)) that are not the $($seriesIds.Count) patches of the production candidate and $DojoSeries$hint")
    Stop-DojoPatches 1
}
# The gate must stay where the plan puts it: in PlayerbotAI::UpdateAI after the state checks and fall information,
# before the cheat refill, UpdateAIReaction and the engines (checked on every run, like the pin guard).
$aiSource = "$playerbots/src/Bot/PlayerbotAI.cpp"
$gate = 'missing'
if (Test-Path -LiteralPath $aiSource -PathType Leaf) {
    $text = Get-Content -LiteralPath $aiSource -Raw
    $start = $text.IndexOf('void PlayerbotAI::UpdateAI(uint32 elapsed, bool minimal)')
    if ($start -ge 0) {
        $body = $text.Substring($start)
        $at = @{
            fall     = $body.IndexOf('bot->SetFallInformation(0, bot->GetPositionZ());')
            gate     = $body.IndexOf('if (IsExternallyControlled())')
            cheat    = $body.IndexOf('HasCheat(BotCheatMask::health)')
            reaction = $body.IndexOf('UpdateAIReaction(elapsed')
            internal = $body.IndexOf('UpdateAIInternal(elapsed')
        }
        $placed = @($at.Values | Where-Object { $_ -lt 0 }).Count -eq 0 -and
            $at.fall -lt $at.gate -and $at.gate -lt $at.cheat -and $at.cheat -lt $at.reaction -and $at.reaction -lt $at.internal
        $gate = if ($placed) { 'ok' } else { 'misplaced' }
    }
}
$result.gate = $gate
if ($gate -ne 'ok') {
    $result.problems.Add("the external-control gate in PlayerbotAI::UpdateAI is $gate (it must follow the fall information and precede the cheats, UpdateAIReaction and UpdateAIInternal)")
    Stop-DojoPatches 1
}
$result.ok = $true
$result.head = (Invoke-DojoGit $playerbots @('rev-parse', 'HEAD')).text.Trim()
Stop-DojoPatches 0
