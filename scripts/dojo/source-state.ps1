# This file is part of leeroy, released under the GNU GPL v2. See LICENSE.
# Shared by configure/build: a receipt must describe committed sources, not
# merely the HEAD that happened to exist beside an uncommitted experiment.
function Get-DojoCleanRevisions([System.Collections.IDictionary] $Roots) {
    $result = [ordered]@{}
    $names = @('source', 'core', 'playerbots')
    if ($Roots.Contains('dungeonclear')) { $names += 'dungeonclear' }
    foreach ($name in $names) {
        $root = [string] $Roots[$name]
        if (-not $root) { throw "missing $name source root" }
        $status = @(& git -c core.fsmonitor=false -C $root status --porcelain --untracked-files=normal 2>$null)
        if ($LASTEXITCODE -ne 0) { throw "cannot inspect $name source tree" }
        if ($status.Count -ne 0) { throw "$name source tree is dirty; commit the candidate before building" }
        $head = & git -c core.fsmonitor=false -C $root rev-parse HEAD 2>$null
        if ($LASTEXITCODE -ne 0 -or "$head".Trim() -notmatch '^[0-9a-f]{40}$') {
            throw "cannot identify $name source revision"
        }
        $result[$name] = "$head".Trim()
    }
    return $result
}

function Assert-DojoSameRevisions([System.Collections.IDictionary] $Before,
                                 [System.Collections.IDictionary] $After) {
    $names = @('source', 'core', 'playerbots')
    if (($Before -and $Before.Contains('dungeonclear')) -or ($After -and $After.Contains('dungeonclear'))) {
        $names += 'dungeonclear'
    }
    foreach ($name in $names) {
        if (-not $Before -or -not $After -or -not $Before[$name] -or $Before[$name] -cne $After[$name]) {
            throw "$name revision changed or lacks clean-source evidence; configure and build the candidate again"
        }
    }
}

function Test-DojoTrustedBuild([string] $Build) {
    # A failed/interrupted/unattested build must not lend its object files to a
    # later receipt, even if somebody reverted its source edits before retrying.
    if (Test-Path -LiteralPath "$Build/dojo-build.pending.json") { return $false }
    try {
        $previous = Get-Content -LiteralPath "$Build/dojo-build.json" -Raw | ConvertFrom-Json -AsHashtable
        foreach ($name in @('source', 'core', 'playerbots')) {
            if ($previous.clean_revisions[$name] -notmatch '^[0-9a-f]{40}$') { return $false }
        }
        if ($previous.exit_code -ne 0 -or @($previous.problems).Count -ne 0) { return $false }
        $hash = (Get-FileHash -LiteralPath "$Build/bin/RelWithDebInfo/worldserver.exe" -Algorithm SHA256).Hash.ToLowerInvariant()
        return $hash -ceq $previous.artifacts['worldserver.exe'].sha256
    } catch { return $false }
}
