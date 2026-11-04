#Requires -Version 7.2
# This file is part of leeroy, released under the GNU GPL v2. See LICENSE.

<#
.SYNOPSIS
Checks a dojo training layout without starting anything.

.DESCRIPTION
Reads a layout file (see layout.example.json) and reports one ok/FAIL/note line
per check: source, core, playerbots, build, runtime and data roots, module
linkage, the training marker, the provenance manifest, database targets, asset
availability, requested ports and toolchains. It never connects to a database,
starts a server or changes a file, except with -WriteProvenance, which records
the baseline commits and dirty-file fingerprints the layout's checkouts derive
from. Database passwords and users are never printed.

Exit status: 0 when nothing failed, 1 when a check failed, 2 when the layout
cannot be read.

.EXAMPLE
pwsh -NoProfile -File scripts/dojo/preflight.ps1 -Layout F:/azerothcore/dojo-runtime/instances/dojo-01/layout.json

.EXAMPLE
pwsh -NoProfile -File scripts/dojo/preflight.ps1 -Layout layout.json -WriteProvenance
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $Layout,
    [switch] $WriteProvenance,
    [switch] $Json
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:GIT_OPTIONAL_LOCKS = '0'   # git status must not refresh another checkout's index

$DojoMarker = 'leeroy-dojo-training'
$DojoIdPattern = '^[A-Za-z0-9._:-]{1,64}$'
$DojoChecks = [System.Collections.Generic.List[object]]::new()
$DojoCompare = if ($IsWindows) { [System.StringComparison]::OrdinalIgnoreCase } else { [System.StringComparison]::Ordinal }

function Add-DojoCheck([string] $Status, [string] $Check, [string] $Detail) {
    $DojoChecks.Add([ordered]@{ status = $Status; check = $Check; detail = $Detail })
}

function Get-DojoValue($Table, [string] $Path, $Default = $null) {
    $node = $Table
    foreach ($key in $Path.Split('.')) {
        if ($node -isnot [System.Collections.IDictionary] -or -not $node.Contains($key)) { return $Default }
        $node = $node[$key]
    }
    if ($null -eq $node) { return $Default }
    return $node
}

function ConvertTo-DojoPath([string] $Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return $null }
    $full = [System.IO.Path]::GetFullPath($Path).Replace('\', '/')
    if ($full.Length -gt 1 -and -not ($full.Length -eq 3 -and $full[1] -eq ':')) { $full = $full.TrimEnd('/') }
    return $full
}

# The path with every junction or symbolic link along it replaced by its final target.
function Resolve-DojoPath([string] $Path) {
    $p = ConvertTo-DojoPath $Path
    if (-not $p) { return $null }
    $parts = $p.Split('/')
    $current = $parts[0] + '/'
    for ($i = 1; $i -lt $parts.Count; $i++) {
        if ($parts[$i] -eq '') { continue }
        $next = [System.IO.Path]::Combine($current, $parts[$i])
        if (Test-Path -LiteralPath $next) {
            $item = Get-Item -LiteralPath $next -Force
            if ($item.LinkType -in 'Junction', 'SymbolicLink') {
                $target = $item.ResolveLinkTarget($true)
                if ($null -ne $target) { $next = $target.FullName }
            }
        }
        $current = $next
    }
    return ConvertTo-DojoPath $current
}

function Test-DojoSame([string] $A, [string] $B) {
    return $A -and $B -and [string]::Equals($A, $B, $DojoCompare)
}

function Test-DojoUnder([string] $Child, [string] $Parent) {
    if (-not $Child -or -not $Parent) { return $false }
    if (Test-DojoSame $Child $Parent) { return $true }
    $prefix = if ($Parent.EndsWith('/')) { $Parent } else { $Parent + '/' }
    return $Child.StartsWith($prefix, $DojoCompare)
}

function Test-DojoLink([string] $Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    return (Get-Item -LiteralPath $Path -Force).LinkType -in 'Junction', 'SymbolicLink'
}

# AzerothCore .conf: Key = "value". Values are returned whole; callers never print secret ones.
function Read-DojoConf([string] $Path) {
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $values }
    foreach ($line in [System.IO.File]::ReadLines($Path)) {
        if ($line -match '^\s*([A-Za-z0-9_.]+)\s*=\s*(.*?)\s*$') {
            $value = $Matches[2]
            if ($value.Length -ge 2 -and $value.StartsWith('"') -and $value.EndsWith('"')) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            $values[$Matches[1]] = $value
        }
    }
    return $values
}

function Get-DojoEndpoint([string] $HostName, $Port) {
    $h = $HostName.Trim().ToLowerInvariant()
    if ($h -in 'localhost', '127.0.0.1', '::1', '.', '[::1]') { $h = 'loopback' }
    return '{0}:{1}' -f $h, $Port
}

function Test-DojoLoopback([string] $HostName) {
    return $HostName -in 'localhost', '127.0.0.1', '::1'
}

# "host;port;user;password;schema" -> host, port, user and schema; the password is dropped here.
function ConvertFrom-DojoDbInfo([string] $Value, [string] $Source) {
    $parts = $Value.Split(';')
    if ($parts.Count -lt 5) { return $null }
    $port = 0
    if (-not [int]::TryParse($parts[1], [ref] $port)) { return $null }
    return [ordered]@{ source = $Source; host = $parts[0]; port = $port; user = $parts[2]; schema = $parts[4] }
}

function Read-DojoDbTargets([string] $ConfigDir, [string] $Label) {
    $targets = [System.Collections.Generic.List[object]]::new()
    if (-not $ConfigDir) { return , $targets }
    $files = [ordered]@{
        'worldserver.conf'         = @('LoginDatabaseInfo', 'WorldDatabaseInfo', 'CharacterDatabaseInfo')
        'authserver.conf'          = @('LoginDatabaseInfo')
        'modules/playerbots.conf'  = @('Playerbots.DatabaseInfo', 'AiPlayerbot.DatabaseInfo')
    }
    foreach ($file in $files.Keys) {
        $conf = Read-DojoConf (Join-Path $ConfigDir $file)
        foreach ($key in $files[$file]) {
            if ($conf.ContainsKey($key)) {
                $target = ConvertFrom-DojoDbInfo $conf[$key] "$Label $file $key"
                if ($null -ne $target) { $targets.Add($target) }
            }
        }
    }
    return , $targets
}

# True when Path is the top of a git checkout, not a folder inside some other repository.
function Test-DojoGitRoot([string] $Path) {
    $top = & git -C $Path rev-parse --show-toplevel 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $top) { return $false }
    return Test-DojoSame (Resolve-DojoPath "$top".Trim()) (Resolve-DojoPath $Path)
}

function Get-DojoRepoState([string] $Path) {
    $state = [ordered]@{ path = $Path; exists = [bool]($Path -and (Test-Path -LiteralPath $Path)) }
    if (-not $state.exists) { return $state }
    if (-not (Test-DojoGitRoot $Path)) { $state.git = $false; return $state }
    $head = & git -C $Path rev-parse HEAD 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $head) { $state.git = $false; return $state }
    $state.git = $true
    $state.head = "$head".Trim()
    $state.branch = "$(& git -C $Path rev-parse --abbrev-ref HEAD 2>$null)".Trim()
    $remotes = [ordered]@{}
    foreach ($name in @(& git -C $Path remote 2>$null)) {
        if (-not $name) { continue }
        $url = "$(& git -C $Path remote get-url $name 2>$null)".Trim()
        $remotes[$name] = $url -replace '://[^@/]+@', '://'
    }
    $state.remotes = $remotes
    $raw = (& git -C $Path status --porcelain=v1 -z --untracked-files=all 2>$null) -join "`n"
    $entries = @($raw.Split([char]0) | Where-Object { $_ })
    $dirty = [System.Collections.Generic.List[object]]::new()
    $limit = 500
    $count = 0
    for ($i = 0; $i -lt $entries.Count; $i++) {
        $entry = $entries[$i]
        if ($entry.Length -lt 4) { continue }
        $code = $entry.Substring(0, 2)
        $rel = $entry.Substring(3)
        if ($code -match '[RC]') { $i++ }   # -z puts a rename's source path in the next entry
        $count++
        if ($dirty.Count -ge $limit) { continue }
        $full = Join-Path $Path $rel
        $fingerprint = if (Test-Path -LiteralPath $full -PathType Leaf) {
            'sha256:' + (Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash.ToLowerInvariant()
        } elseif (Test-Path -LiteralPath $full -PathType Container) { 'directory' } else { 'absent' }
        $dirty.Add([ordered]@{ status = $code; path = $rel; fingerprint = $fingerprint })
    }
    $state.dirty_count = $count
    $state.dirty_truncated = $count -gt $dirty.Count
    $state.dirty = $dirty
    return $state
}

function Write-DojoReport([int] $Code) {
    $fails = @($DojoChecks | Where-Object { $_.status -eq 'FAIL' }).Count
    if ($Json) {
        [ordered]@{
            ok     = ($fails -eq 0 -and $Code -eq 0)
            layout = $Layout
            failed = $fails
            checks = $DojoChecks
        } | ConvertTo-Json -Depth 6
    } else {
        foreach ($c in $DojoChecks) { '{0,-4} {1}: {2}' -f $c.status, $c.check, $c.detail }
        if ($Code -eq 0) { if ($fails -eq 0) { 'preflight: ok' } else { "preflight: $fails failed" } }
    }
}

# --- the layout ---

$DojoLayout = $null
try {
    $DojoLayout = Get-Content -LiteralPath $Layout -Raw | ConvertFrom-Json -AsHashtable
} catch {
    Add-DojoCheck 'FAIL' 'layout' "cannot read $Layout`: $($_.Exception.Message)"
}
if ($DojoLayout -isnot [System.Collections.IDictionary]) {
    if ($DojoChecks.Count -eq 0) { Add-DojoCheck 'FAIL' 'layout' "$Layout is not a JSON object" }
    Write-DojoReport 2
    exit 2
}
$Layout = ConvertTo-DojoPath $Layout

$roots = [ordered]@{}
$resolved = [ordered]@{}
foreach ($name in 'source', 'core', 'playerbots', 'build', 'runtime', 'data') {
    $raw = Get-DojoValue $DojoLayout "roots.$name"
    if (-not $raw -or -not [System.IO.Path]::IsPathFullyQualified([string] $raw)) {
        Add-DojoCheck 'FAIL' "root $name" "missing or not an absolute path: '$raw'"
        $roots[$name] = $null
        $resolved[$name] = $null
        continue
    }
    $roots[$name] = ConvertTo-DojoPath $raw
    $resolved[$name] = Resolve-DojoPath $raw
}
$configsRaw = Get-DojoValue $DojoLayout 'roots.configs' $(if ($roots.runtime) { "$($roots.runtime)/configs" } else { $null })
$configs = Resolve-DojoPath $configsRaw

$prod = [ordered]@{}
foreach ($name in 'install_root', 'core', 'build', 'configs', 'modules') {
    $prod[$name] = Resolve-DojoPath (Get-DojoValue $DojoLayout "production.$name")
}
$protected = @(@(Get-DojoValue $DojoLayout 'production.protected' @()) | ForEach-Object { Resolve-DojoPath $_ } | Where-Object { $_ })
$provenancePath = Resolve-DojoPath (Get-DojoValue $DojoLayout 'provenance' $(if ($roots.data) { "$($roots.data)/manifests/provenance.json" } else { $null }))

if (-not $prod.install_root -or -not $prod.core -or -not $prod.build -or -not $prod.configs) {
    Add-DojoCheck 'FAIL' 'production' 'production.install_root, core, build and configs must name the live install so it can be refused'
}

# --- provenance: written on request, then the run stops ---

if ($WriteProvenance) {
    if (-not $provenancePath) {
        Add-DojoCheck 'FAIL' 'provenance' 'no provenance path (layout "provenance" or roots.data)'
        Write-DojoReport 0
        exit 1
    }
    $prodPlayerbots = if ($prod.core) { "$($prod.core)/modules/mod-playerbots" } else { $null }
    $manifest = [ordered]@{
        kind          = 'leeroy-dojo-provenance'
        format        = 1
        created_utc   = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
        marker        = $DojoMarker
        instance      = Get-DojoValue $DojoLayout 'instance'
        layout        = $Layout
        layout_sha256 = (Get-FileHash -LiteralPath $Layout -Algorithm SHA256).Hash.ToLowerInvariant()
        baseline      = [ordered]@{
            install_root = Get-DojoRepoState $prod.install_root
            core         = Get-DojoRepoState $prod.core
            playerbots   = Get-DojoRepoState $prodPlayerbots
        }
        isolated      = [ordered]@{
            source     = Get-DojoRepoState $resolved.source
            core       = Get-DojoRepoState $resolved.core
            playerbots = Get-DojoRepoState $resolved.playerbots
        }
    }
    $dir = Split-Path -Parent $provenancePath
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $tmp = "$provenancePath.tmp"
    $manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $tmp -Encoding utf8NoBOM
    Move-Item -LiteralPath $tmp -Destination $provenancePath -Force
    foreach ($group in 'baseline', 'isolated') {
        foreach ($name in $manifest[$group].Keys) {
            $s = $manifest[$group][$name]
            $detail = if (-not $s.exists) { "not present ($($s.path))" }
                elseif (-not $s.git) { "not a git checkout ($($s.path))" }
                else { "$($s.branch) $($s.head.Substring(0, 12)), $($s.dirty_count) dirty ($($s.path))" }
            Add-DojoCheck 'ok' "provenance $group $name" $detail
        }
    }
    Add-DojoCheck 'ok' 'provenance' "wrote $provenancePath"
    Write-DojoReport 0
    exit 0
}

# --- roots ---

$prodChecks = [ordered]@{
    'production core'    = $prod.core
    'production build'   = $prod.build
    'production configs' = $prod.configs
    'production modules' = $prod.modules
}
foreach ($name in $roots.Keys) {
    $r = $resolved[$name]
    if (-not $r) { continue }
    $problems = [System.Collections.Generic.List[string]]::new()
    if (Test-DojoSame $r $prod.install_root) { $problems.Add('is the live install checkout') }
    foreach ($label in $prodChecks.Keys) {
        if (Test-DojoUnder $r $prodChecks[$label]) { $problems.Add("is inside the $label ($($prodChecks[$label]))") }
    }
    foreach ($p in $protected) {
        if (Test-DojoUnder $r $p) { $problems.Add("is inside protected $p") }
    }
    if (($name -in 'build', 'runtime', 'data') -and (Test-DojoUnder $r $resolved.source)) {
        $problems.Add('is inside the source checkout')
    }
    $shown = if (Test-DojoSame $r $roots[$name]) { $r } else { "$($roots[$name]) -> $r" }
    if ($problems.Count -gt 0) {
        Add-DojoCheck 'FAIL' "root $name" "$shown $($problems -join '; ')"
    } elseif (-not (Test-Path -LiteralPath $r -PathType Container)) {
        $status = if ($name -in 'build', 'data') { 'note' } else { 'FAIL' }
        Add-DojoCheck $status "root $name" "$shown does not exist yet"
    } else {
        Add-DojoCheck 'ok' "root $name" $shown
    }
}

if ($resolved.source -and (Test-Path -LiteralPath $resolved.source)) {
    foreach ($sub in 'modules/mod-dojo', 'dojo/leeroy_dojo') {
        if (-not (Test-Path -LiteralPath "$($resolved.source)/$sub" -PathType Container)) {
            Add-DojoCheck 'FAIL' 'source layout' "$($resolved.source) has no $sub"
        }
    }
}

if ($resolved.build) {
    $cache = "$($resolved.build)/CMakeCache.txt"
    if (Test-Path -LiteralPath $cache -PathType Leaf) {
        $cacheSource = $null
        foreach ($line in [System.IO.File]::ReadLines($cache)) {
            if ($line -match '^CMAKE_HOME_DIRECTORY:INTERNAL=(.*)$') { $cacheSource = Resolve-DojoPath $Matches[1]; break }
        }
        if (Test-DojoSame $cacheSource $resolved.core) {
            Add-DojoCheck 'ok' 'build cache' "configured from $cacheSource"
        } else {
            Add-DojoCheck 'FAIL' 'build cache' "$cache was configured from '$cacheSource', not the dojo core; configure a fresh tree"
        }
    } else {
        Add-DojoCheck 'note' 'build cache' 'not configured yet'
    }
}

# --- configuration target ---

$trainingConf = @{}
if ($configs) {
    if ((Test-DojoUnder $configs $prod.configs) -or (Test-DojoUnder $configs $prod.build)) {
        Add-DojoCheck 'FAIL' 'config target' "$configs is a production configuration directory"
    } else {
        $trainingConf = Read-DojoConf "$configs/worldserver.conf"
        if ($trainingConf.Count -eq 0) {
            Add-DojoCheck 'note' 'config target' "$configs/worldserver.conf not provisioned yet"
        } else {
            Add-DojoCheck 'ok' 'config target' "$configs/worldserver.conf"
            if ($trainingConf.ContainsKey('SourceDirectory')) {
                $src = Resolve-DojoPath $trainingConf['SourceDirectory']
                if (Test-DojoSame $src $resolved.core) {
                    Add-DojoCheck 'ok' 'config SourceDirectory' $src
                } else {
                    Add-DojoCheck 'FAIL' 'config SourceDirectory' "$src is not the dojo core $($resolved.core)"
                }
            }
            if ($trainingConf.ContainsKey('LogsDir') -and $trainingConf['LogsDir']) {
                $logs = Resolve-DojoPath $trainingConf['LogsDir']
                $outside = (Test-DojoUnder $logs $prod.install_root) -and -not (Test-DojoUnder $logs $resolved.runtime)
                if ((Test-DojoUnder $logs $prod.build) -or $outside) {
                    Add-DojoCheck 'FAIL' 'config LogsDir' "$logs is outside the dojo runtime"
                } else {
                    Add-DojoCheck 'ok' 'config LogsDir' $logs
                }
            }
            if ($trainingConf.ContainsKey('MapUpdate.Threads') -and $trainingConf['MapUpdate.Threads'] -ne '0') {
                Add-DojoCheck 'FAIL' 'config MapUpdate.Threads' "is $($trainingConf['MapUpdate.Threads']); the first worker needs 0 (D11)"
            }
        }
    }
}

# --- module linkage ---

$modules = @(Get-DojoValue $DojoLayout 'modules' @('mod-playerbots', 'mod-dojo'))
foreach ($required in 'mod-dojo', 'mod-playerbots') {
    if ($required -notin $modules) { Add-DojoCheck 'FAIL' 'module set' "layout modules must include $required" }
}
if ($resolved.core -and (Test-Path -LiteralPath "$($resolved.core)/modules" -PathType Container)) {
    $isolatedRoots = @("$($resolved.source)/modules", $resolved.playerbots, $resolved.core) | Where-Object { $_ }
    $present = @{}
    foreach ($dir in Get-ChildItem -LiteralPath "$($resolved.core)/modules" -Directory -Force) {
        $name = $dir.Name
        $entry = ConvertTo-DojoPath $dir.FullName
        $target = Resolve-DojoPath $entry
        $present[$name] = $true
        $linked = Test-DojoLink $entry
        $inside = @($isolatedRoots | Where-Object { Test-DojoUnder $target $_ }).Count -gt 0
        $intoProduction = (Test-DojoUnder $target $prod.install_root) -and -not $inside
        if ($name -notin $modules) {
            Add-DojoCheck 'FAIL' "module $name" "not in the training module set (D02); remove it from $($resolved.core)/modules"
        } elseif ($linked -and (-not $inside -or $intoProduction)) {
            Add-DojoCheck 'FAIL' "module $name" "external junction $entry -> $target"
        } elseif ($name -eq 'mod-dojo' -and -not (Test-DojoSame $target "$($resolved.source)/modules/mod-dojo")) {
            Add-DojoCheck 'FAIL' "module $name" "$entry resolves to $target, not the source root's modules/mod-dojo"
        } elseif ($name -eq 'mod-playerbots' -and -not (Test-DojoSame $target $resolved.playerbots)) {
            Add-DojoCheck 'FAIL' "module $name" "$entry resolves to $target, not the playerbots root"
        } elseif ($name -notin 'mod-dojo', 'mod-playerbots') {
            Add-DojoCheck 'note' "module $name" "extra module $target; it belongs in a versioned experiment"
        } else {
            $how = if ($linked) { "junction -> $target" } else { 'directory' }
            Add-DojoCheck 'ok' "module $name" $how
        }
    }
    foreach ($name in $modules) {
        if (-not $present.ContainsKey($name)) { Add-DojoCheck 'FAIL' "module $name" "missing from $($resolved.core)/modules" }
    }
} elseif ($resolved.core) {
    Add-DojoCheck 'FAIL' 'module linkage' "$($resolved.core)/modules does not exist"
}

# --- marker ---

$instance = [string] (Get-DojoValue $DojoLayout 'instance' '')
if ((Get-DojoValue $DojoLayout 'marker') -ne $DojoMarker) {
    Add-DojoCheck 'FAIL' 'marker' "layout marker must be '$DojoMarker'"
} elseif ($instance -notmatch $DojoIdPattern) {
    Add-DojoCheck 'FAIL' 'marker' "layout instance '$instance' is not a valid name"
} elseif ($resolved.runtime) {
    $markerFile = "$($resolved.runtime)/dojo.marker"
    $found = $null
    if (Test-Path -LiteralPath $markerFile -PathType Leaf) {
        try { $found = Get-Content -LiteralPath $markerFile -Raw | ConvertFrom-Json -AsHashtable } catch { $found = $null }
    }
    if ($found -isnot [System.Collections.IDictionary]) {
        Add-DojoCheck 'FAIL' 'marker' "missing training marker $markerFile"
    } elseif ((Get-DojoValue $found 'marker') -ne $DojoMarker -or (Get-DojoValue $found 'instance') -ne $instance) {
        Add-DojoCheck 'FAIL' 'marker' "$markerFile does not mark instance '$instance'"
    } else {
        Add-DojoCheck 'ok' 'marker' "$markerFile ($instance)"
    }
}

# --- provenance ---

$provenance = $null
if ($provenancePath -and (Test-Path -LiteralPath $provenancePath -PathType Leaf)) {
    try { $provenance = Get-Content -LiteralPath $provenancePath -Raw | ConvertFrom-Json -AsHashtable } catch { $provenance = $null }
}
if ($provenance -isnot [System.Collections.IDictionary] -or (Get-DojoValue $provenance 'kind') -ne 'leeroy-dojo-provenance') {
    Add-DojoCheck 'FAIL' 'provenance' "missing provenance manifest $provenancePath (run with -WriteProvenance)"
} else {
    $created = Get-DojoValue $provenance 'created_utc'
    if ($created -is [DateTime]) { $created = $created.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
    Add-DojoCheck 'ok' 'provenance' "$provenancePath recorded $created"
    foreach ($name in 'core', 'playerbots') {
        $base = Get-DojoValue $provenance "baseline.$name.head"
        $root = $resolved[$name]
        if (-not $base) {
            Add-DojoCheck 'FAIL' "baseline $name" 'the manifest records no baseline commit'
        } elseif (-not $root -or -not (Test-Path -LiteralPath $root)) {
            continue
        } else {
            if (-not (Test-DojoGitRoot $root)) {
                Add-DojoCheck 'FAIL' "baseline $name" "$root is not a git checkout"
                continue
            }
            & git -C $root merge-base --is-ancestor $base HEAD 2>$null
            switch ($LASTEXITCODE) {
                0 { Add-DojoCheck 'ok' "baseline $name" "HEAD descends from $($base.Substring(0, 12))" }
                1 { Add-DojoCheck 'FAIL' "baseline $name" "HEAD does not descend from the recorded baseline $($base.Substring(0, 12))" }
                default { Add-DojoCheck 'FAIL' "baseline $name" "baseline $($base.Substring(0, 12)) is not in $root" }
            }
        }
    }
}

# --- databases ---

$liveTargets = [System.Collections.Generic.List[object]]::new()
foreach ($t in (Read-DojoDbTargets $prod.configs 'production')) { $liveTargets.Add($t) }
foreach ($d in @(Get-DojoValue $DojoLayout 'production.databases' @())) {
    if ($d -is [System.Collections.IDictionary]) {
        $liveTargets.Add([ordered]@{ source = 'production default'; host = [string] $d.host; port = [int] $d.port; user = ''; schema = [string] $d.schema })
    }
}
$liveSchemas = @{}
$liveUsers = @{}
foreach ($t in $liveTargets) {
    $endpoint = Get-DojoEndpoint $t.host $t.port
    $liveSchemas["$endpoint/$($t.schema)".ToLowerInvariant()] = $true
    if ($t.user) { $liveUsers["$endpoint/$($t.user)"] = $true }
}

$trainingTargets = [System.Collections.Generic.List[object]]::new()
$db = Get-DojoValue $DojoLayout 'database'
if ($db -is [System.Collections.IDictionary]) {
    foreach ($kind in 'auth', 'characters', 'world', 'playerbots') {
        $schema = Get-DojoValue $db "schemas.$kind"
        if (-not $schema) {
            Add-DojoCheck 'FAIL' 'database' "layout database.schemas.$kind is missing"
            continue
        }
        $trainingTargets.Add([ordered]@{ source = "layout $kind"; host = [string] $db.host; port = [int] $db.port; user = [string] (Get-DojoValue $db 'user' ''); schema = [string] $schema })
    }
    if (-not (Get-DojoValue $db 'user')) { Add-DojoCheck 'FAIL' 'database' 'layout database.user is missing: the dojo needs its own restricted user' }
} else {
    Add-DojoCheck 'FAIL' 'database' 'layout has no database section'
}
if ($configs -and -not (Test-DojoUnder $configs $prod.configs)) {
    foreach ($t in (Read-DojoDbTargets $configs 'training')) { $trainingTargets.Add($t) }
}

$seen = @{}
foreach ($t in $trainingTargets) {
    $endpoint = Get-DojoEndpoint $t.host $t.port
    $where = "$($t.host):$($t.port)/$($t.schema)"
    $key = "$endpoint/$($t.schema)".ToLowerInvariant()
    if ($liveSchemas.ContainsKey($key)) {
        Add-DojoCheck 'FAIL' "database $($t.source)" "writable live database target $where"
    } elseif ($t.user -and $liveUsers.ContainsKey("$endpoint/$($t.user)")) {
        Add-DojoCheck 'FAIL' "database $($t.source)" "$where uses the live server's database user"
    } else {
        Add-DojoCheck 'ok' "database $($t.source)" $where
    }
    if ($t.source -like 'layout *') {
        if ($seen.ContainsKey($key)) { Add-DojoCheck 'FAIL' "database $($t.source)" "$where is shared with $($seen[$key])" }
        $seen[$key] = $t.source
    }
}
foreach ($t in $trainingTargets | Where-Object { $_.source -like 'training *' }) {
    $key = "$(Get-DojoEndpoint $t.host $t.port)/$($t.schema)".ToLowerInvariant()
    if (-not $seen.ContainsKey($key) -and -not $liveSchemas.ContainsKey($key)) {
        Add-DojoCheck 'FAIL' "database $($t.source)" "$($t.host):$($t.port)/$($t.schema) is not one of the layout's schemas"
    }
}
foreach ($s in @(Get-DojoValue $DojoLayout 'database.bootstrap_sources' @())) {
    Add-DojoCheck 'note' 'database bootstrap' "$s may be read to provision the dojo schemas; runtime never writes it"
}
Add-DojoCheck 'note' 'database grants' 'not verified: preflight never connects to a database'

# --- assets ---

$assets = Resolve-DojoPath (Get-DojoValue $DojoLayout 'assets')
if (-not $assets) {
    Add-DojoCheck 'FAIL' 'assets' 'layout assets (the client Data directory) is missing'
} else {
    $missing = @('dbc', 'maps', 'vmaps', 'mmaps' | Where-Object { -not (Test-Path -LiteralPath "$assets/$_" -PathType Container) })
    if ($missing.Count -gt 0) {
        Add-DojoCheck 'FAIL' 'assets' "$assets lacks $($missing -join ', ')"
    } else {
        $shared = if (Test-DojoUnder $assets $prod.build) { ', shared read-only with the live server' } else { '' }
        Add-DojoCheck 'ok' 'assets' "$assets$shared"
    }
    if ($trainingConf.ContainsKey('DataDir') -and -not (Test-DojoSame (Resolve-DojoPath $trainingConf['DataDir']) $assets)) {
        Add-DojoCheck 'FAIL' 'config DataDir' "$($trainingConf['DataDir']) is not the layout's assets $assets"
    }
}

# --- ports ---

$liveConfs = @((Read-DojoConf "$($prod.configs)/worldserver.conf"), (Read-DojoConf "$($prod.configs)/authserver.conf"))
$livePorts = @{}
foreach ($p in @(Get-DojoValue $DojoLayout 'production.ports' @())) { $livePorts[[int] $p] = 'production' }
foreach ($conf in $liveConfs) {
    foreach ($key in 'WorldServerPort', 'RealmServerPort', 'Ra.Port', 'SOAP.Port') {
        $n = 0
        if ($conf.ContainsKey($key) -and [int]::TryParse($conf[$key], [ref] $n)) { $livePorts[$n] = $key }
    }
}
$listening = @{}
try {
    foreach ($listener in [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()) {
        $listening[$listener.Port] = $true
    }
} catch {
    Add-DojoCheck 'note' 'ports' 'cannot list listening sockets on this system'
}
$dojoHost = [string] (Get-DojoValue $DojoLayout 'ports.dojo_host' '127.0.0.1')
if (Test-DojoLoopback $dojoHost) {
    Add-DojoCheck 'ok' 'port dojo_host' "$dojoHost (loopback)"
} else {
    Add-DojoCheck 'FAIL' 'port dojo_host' "$dojoHost is not a loopback address (D14)"
}
$ports = Get-DojoValue $DojoLayout 'ports' @{}
$taken = @{}
foreach ($name in @($ports.Keys | Where-Object { $_ -ne 'dojo_host' } | Sort-Object)) {
    $value = $ports[$name]
    $isNumber = ($value -is [long]) -or ($value -is [int])
    if (-not $isNumber -or $value -lt 0 -or $value -gt 65535) {
        Add-DojoCheck 'FAIL' "port $name" "'$value' is not a port number"
    } elseif ($value -eq 0) {
        $status = if ($name -eq 'dojo') { 'ok' } else { 'FAIL' }
        Add-DojoCheck $status "port $name" 'ephemeral (chosen at start and written to the process receipt)'
    } elseif ($livePorts.ContainsKey([int] $value)) {
        Add-DojoCheck 'FAIL' "port $name" "$value is a production port ($($livePorts[[int] $value]))"
    } elseif ($taken.ContainsKey([int] $value)) {
        Add-DojoCheck 'FAIL' "port $name" "$value is also requested as $($taken[[int] $value])"
    } elseif ($listening.ContainsKey([int] $value)) {
        Add-DojoCheck 'FAIL' "port $name" "$value is already in use"
    } else {
        Add-DojoCheck 'ok' "port $name" "$value"
    }
    if ($isNumber -and $value -ge 0 -and $value -le 65535) { $taken[[int] $value] = $name }
}
foreach ($pair in @(@('WorldServerPort', 'world'), @('RealmServerPort', 'auth'))) {
    $key, $name = $pair
    if ($trainingConf.ContainsKey($key) -and $ports.Contains($name) -and "$($ports[$name])" -ne $trainingConf[$key]) {
        Add-DojoCheck 'FAIL' "config $key" "$($trainingConf[$key]) differs from the layout's $name port $($ports[$name])"
    }
}

# --- toolchains (reported, never required here) ---

function Get-DojoToolVersion([string] $Command, [string[]] $Arguments) {
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) { return $null }
    $out = @(& $Command @Arguments 2>$null)
    if ($LASTEXITCODE -ne 0 -or $out.Count -eq 0) { return $null }
    return "$($out[0])".Trim()
}
$git = Get-DojoToolVersion 'git' @('--version')
Add-DojoCheck $(if ($git) { 'ok' } else { 'note' }) 'toolchain git' $(if ($git) { $git } else { 'not found' })
$cmake = Get-DojoToolVersion 'cmake' @('--version')
Add-DojoCheck $(if ($cmake) { 'ok' } else { 'note' }) 'toolchain cmake' $(if ($cmake) { $cmake } else { 'not found' })
if ($IsWindows) {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
    $vs = if (Test-Path -LiteralPath $vswhere) { "$(& $vswhere -latest -products * -property displayName 2>$null)".Trim() } else { '' }
    Add-DojoCheck $(if ($vs) { 'ok' } else { 'note' }) 'toolchain msvc' $(if ($vs) { $vs } else { 'Visual Studio not found' })
}
$python = $null
foreach ($candidate in @(@('py', '-3.12', '--version'), @('python3.12', '--version'), @('uv', 'python', 'find', '3.12'))) {
    $python = Get-DojoToolVersion $candidate[0] $candidate[1..($candidate.Count - 1)]
    if ($python) { break }
}
Add-DojoCheck $(if ($python) { 'ok' } else { 'note' }) 'toolchain python 3.12' $(if ($python) { $python } else { 'not found (uv python install 3.12)' })
$boost = Get-DojoValue $DojoLayout 'toolchain.boost'
if ($boost) {
    $b = Resolve-DojoPath $boost
    Add-DojoCheck $(if (Test-Path -LiteralPath "$b/boost" -PathType Container) { 'ok' } else { 'FAIL' }) 'toolchain boost' $b
}

Write-DojoReport 0
if (@($DojoChecks | Where-Object { $_.status -eq 'FAIL' }).Count -gt 0) { exit 1 }
exit 0
