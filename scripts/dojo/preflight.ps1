#Requires -Version 7.2
# This file is part of leeroy, released under the GNU GPL v2. See LICENSE.

<#
.SYNOPSIS
Checks a dojo training layout without starting anything.

.DESCRIPTION
Reads a layout file (see layout.example.json) and reports one ok/FAIL/note line
per check: the live install it must stay away from, source, core, playerbots,
build, runtime and data roots, module linkage and folder-name rules (each
module's .cmake and script loader follow its folder name; mod-dojo ships with
Dojo.Enable = 0), the training marker, the
provenance manifest, database targets, asset availability, requested and
configured ports and toolchains. It refuses rather than passes whenever it
cannot positively identify the live install and its database targets. It never
connects to a database, starts a server or changes a file, except with
-WriteProvenance, which records the baseline commits and dirty-file
fingerprints the layout's checkouts derive from, and only inside the data root
once every live-install and root check has passed. Database passwords and users
are never printed. The only programs it runs are git (rev-parse, remote, status,
merge-base, with fsmonitor off), and cmake, Python and vswhere for their
versions.

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
# AzerothCore's compiled-in defaults, used when a .conf leaves the key out.
$DojoDefaultPorts = [ordered]@{ WorldServerPort = 8085; RealmServerPort = 3724; 'Ra.Port' = 3443; 'SOAP.Port' = 7878 }

function Add-DojoCheck([string] $Status, [string] $Check, [string] $Detail) {
    $DojoChecks.Add([ordered]@{ status = $Status; check = $Check; detail = $Detail })
}

function Get-DojoFailCount {
    return @($DojoChecks | Where-Object { $_.status -eq 'FAIL' }).Count
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

# The final target of a link, or $null for a dangling one.
function Get-DojoLinkTarget([System.IO.FileSystemInfo] $Item) {
    $target = $Item.ResolveLinkTarget($true)
    if ($null -ne $target) { return $target.FullName }
    if (-not $Item.LinkTarget) { return $null }
    $parent = [System.IO.Path]::GetDirectoryName($Item.FullName)
    return [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($parent, $Item.LinkTarget))
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
                $target = Get-DojoLinkTarget $item
                if ($target) { $next = $target }
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

# Every junction or symbolic link (file or directory) below Root, not following any; .git folders are skipped.
function Find-DojoLinks([string] $Root, [string[]] $Skip = @()) {
    $found = [System.Collections.Generic.List[System.IO.FileSystemInfo]]::new()
    if (-not $Root -or -not (Test-Path -LiteralPath $Root -PathType Container)) { return , $found }
    $options = [System.IO.EnumerationOptions]::new()
    $options.AttributesToSkip = [System.IO.FileAttributes]::None
    $options.IgnoreInaccessible = $true
    $stack = [System.Collections.Generic.Stack[string]]::new()
    $stack.Push($Root)
    while ($stack.Count -gt 0) {
        $dir = $stack.Pop()
        foreach ($entry in [System.IO.DirectoryInfo]::new($dir).EnumerateFileSystemInfos('*', $options)) {
            if ($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                $found.Add($entry)
            } elseif (($entry.Attributes -band [System.IO.FileAttributes]::Directory) -and $entry.Name -ne '.git') {
                $full = ConvertTo-DojoPath $entry.FullName
                if (-not @($Skip | Where-Object { Test-DojoSame $full $_ }).Count) { $stack.Push($entry.FullName) }
            }
        }
    }
    return , $found
}

# AzerothCore .conf: Key = "value". Values are returned whole; callers never print secret ones.
function Read-DojoConf([string] $Path) {
    $values = @{}
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $values }
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
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

# Every spelling of this machine a database client could use: loopback names, the host and domain name, and the
# address of every local interface. Read from the system, never looked up.
$DojoLocalNames = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
foreach ($n in 'localhost', '.', '0.0.0.0', '::', '::1') { [void] $DojoLocalNames.Add($n) }
try {
    $hostName = [System.Net.Dns]::GetHostName()
    [void] $DojoLocalNames.Add($hostName)
    [void] $DojoLocalNames.Add([Environment]::MachineName)
    $domain = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().DomainName
    if ($domain) { [void] $DojoLocalNames.Add("$hostName.$domain") }
    foreach ($nic in [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
        foreach ($address in $nic.GetIPProperties().UnicastAddresses) {
            [void] $DojoLocalNames.Add(($address.Address.ToString() -replace '%.*$', ''))
        }
    }
} catch {
    Add-DojoCheck 'note' 'local names' 'cannot list this machine''s interfaces; only loopback spellings are recognised'
}

function Get-DojoHostKey([string] $HostName) {
    $h = $HostName.Trim().TrimStart('[').TrimEnd(']').TrimEnd('.')
    if ($h -match '^127\.' -or $h -match '^::ffff:127\.' -or $DojoLocalNames.Contains($h)) { return 'this-machine' }
    return $h.ToLowerInvariant()
}

function Test-DojoLoopback([string] $HostName) {
    $h = $HostName.Trim().TrimStart('[').TrimEnd(']')
    return $h -eq 'localhost' -or $h -eq '::1' -or $h -match '^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$'
}

# "host;port;user;password;schema" -> host, port, user and schema; the password is dropped here. $null when the
# value cannot be read as a TCP target (a named pipe or socket path included), which callers refuse.
function ConvertFrom-DojoDbInfo([string] $Value, [string] $Source) {
    $parts = $Value.Split(';')
    if ($parts.Count -lt 5 -or -not $parts[0].Trim() -or -not $parts[4].Trim()) { return $null }
    $port = 0
    if (-not [int]::TryParse($parts[1], [ref] $port) -or $port -lt 1 -or $port -gt 65535) { return $null }
    return [ordered]@{ source = $Source; host = $parts[0].Trim(); port = $port; user = $parts[2]; schema = $parts[4].Trim() }
}

function Read-DojoDbTargets([string] $ConfigDir, [string] $Label) {
    $result = [ordered]@{
        targets    = [System.Collections.Generic.List[object]]::new()
        unreadable = [System.Collections.Generic.List[string]]::new()
    }
    if (-not $ConfigDir) { return $result }
    $files = [ordered]@{
        'worldserver.conf'         = @('LoginDatabaseInfo', 'WorldDatabaseInfo', 'CharacterDatabaseInfo')
        'authserver.conf'          = @('LoginDatabaseInfo')
        'modules/playerbots.conf'  = @('PlayerbotsDatabaseInfo', 'Playerbots.DatabaseInfo', 'AiPlayerbot.DatabaseInfo')
    }
    foreach ($file in $files.Keys) {
        $conf = Read-DojoConf (Join-Path $ConfigDir $file)
        foreach ($key in $files[$file]) {
            if ($conf.ContainsKey($key)) {
                $target = ConvertFrom-DojoDbInfo $conf[$key] "$Label $file $key"
                if ($null -ne $target) { $result.targets.Add($target) } else { $result.unreadable.Add("$Label $file $key") }
            }
        }
    }
    return $result
}

function Invoke-DojoGit {
    # fsmonitor would start a daemon; it stays off for every call.
    & git -c core.fsmonitor=false @args
}

# True when Path is the top of a git checkout, not a folder inside some other repository.
function Test-DojoGitRoot([string] $Path) {
    $top = Invoke-DojoGit -C $Path rev-parse --show-toplevel 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $top) { return $false }
    return Test-DojoSame (Resolve-DojoPath "$top".Trim()) (Resolve-DojoPath $Path)
}

function Get-DojoRepoState([string] $Path) {
    $state = [ordered]@{ path = $Path; exists = [bool]($Path -and (Test-Path -LiteralPath $Path)) }
    if (-not $state.exists) { return $state }
    if (-not (Test-DojoGitRoot $Path)) { $state.git = $false; return $state }
    $head = Invoke-DojoGit -C $Path rev-parse HEAD 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $head) { $state.git = $false; return $state }
    $state.git = $true
    $state.head = "$head".Trim()
    $state.branch = "$(Invoke-DojoGit -C $Path rev-parse --abbrev-ref HEAD 2>$null)".Trim()
    $remotes = [ordered]@{}
    foreach ($name in @(Invoke-DojoGit -C $Path remote 2>$null)) {
        if (-not $name) { continue }
        $url = "$(Invoke-DojoGit -C $Path remote get-url $name 2>$null)".Trim()
        $remotes[$name] = $url -replace '://[^@/]+@', '://'
    }
    $state.remotes = $remotes
    $raw = (Invoke-DojoGit -C $Path status --porcelain=v1 -z --untracked-files=all 2>$null) -join "`n"
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
    $fails = Get-DojoFailCount
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

# --- the live install: identified positively before anything is compared with it ---

$prod = [ordered]@{}
foreach ($name in 'install_root', 'core', 'build', 'configs', 'modules') {
    $raw = Get-DojoValue $DojoLayout "production.$name"
    $prod[$name] = $null
    if (-not $raw) {
        if ($name -ne 'modules') { Add-DojoCheck 'FAIL' "production $name" 'missing: the live install must be named so it can be refused' }
    } elseif (-not [System.IO.Path]::IsPathFullyQualified([string] $raw)) {
        Add-DojoCheck 'FAIL' "production $name" "'$raw' is not an absolute path"
    } else {
        $prod[$name] = Resolve-DojoPath $raw
        if (-not (Test-Path -LiteralPath $prod[$name] -PathType Container)) {
            Add-DojoCheck 'FAIL' "production $name" "$($prod[$name]) does not exist, so the live install cannot be identified"
        }
    }
}
$protected = @(@(Get-DojoValue $DojoLayout 'production.protected' @()) | ForEach-Object { Resolve-DojoPath $_ } | Where-Object { $_ })
$liveConf = @{}
if ($prod.core -and (Test-Path -LiteralPath $prod.core -PathType Container) -and -not (Test-DojoGitRoot $prod.core)) {
    Add-DojoCheck 'FAIL' 'production core' "$($prod.core) is not a git checkout"
}
if ($prod.build -and (Test-Path -LiteralPath $prod.build -PathType Container)) {
    $cache = "$($prod.build)/CMakeCache.txt"
    $cacheSource = $null
    if (Test-Path -LiteralPath $cache -PathType Leaf) {
        foreach ($line in [System.IO.File]::ReadAllLines($cache)) {
            if ($line -match '^CMAKE_HOME_DIRECTORY:INTERNAL=(.*)$') { $cacheSource = Resolve-DojoPath $Matches[1]; break }
        }
    }
    if (-not (Test-DojoSame $cacheSource $prod.core)) {
        Add-DojoCheck 'FAIL' 'production build' "$cache does not name production.core as its source ('$cacheSource'), so this is not the live build"
    }
}
if ($prod.configs -and (Test-Path -LiteralPath $prod.configs -PathType Container)) {
    $liveConf = Read-DojoConf "$($prod.configs)/worldserver.conf"
    if ($liveConf.Count -eq 0) {
        Add-DojoCheck 'FAIL' 'production configs' "$($prod.configs)/worldserver.conf is missing or empty, so the live targets cannot be read"
    }
}
if (@($DojoChecks | Where-Object { $_.status -eq 'FAIL' -and $_.check -like 'production*' }).Count -eq 0) {
    Add-DojoCheck 'ok' 'production' "$($prod.install_root): core $($prod.core), build configured from it, configs $($prod.configs)"
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
    if (Test-DojoSame $r $prod.install_root) {
        $problems.Add('is the live install checkout')
    } elseif (Test-DojoUnder $prod.install_root $r) {
        $problems.Add("contains the live install ($($prod.install_root))")
    }
    foreach ($label in $prodChecks.Keys) {
        $p = $prodChecks[$label]
        if (Test-DojoUnder $r $p) { $problems.Add("is inside the $label ($p)") }
        elseif (Test-DojoUnder $p $r) { $problems.Add("contains the $label ($p)") }
    }
    foreach ($p in $protected) {
        if (Test-DojoUnder $r $p) { $problems.Add("is inside protected $p") }
        elseif (Test-DojoUnder $p $r) { $problems.Add("contains protected $p") }
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

# --- the provenance manifest's place: inside the data root, and never over anything else ---

$provenancePath = Resolve-DojoPath (Get-DojoValue $DojoLayout 'provenance' $(if ($roots.data) { "$($roots.data)/manifests/provenance.json" } else { $null }))
$provenanceProblems = [System.Collections.Generic.List[string]]::new()
if (-not $provenancePath) {
    $provenanceProblems.Add('no provenance path (layout "provenance" or roots.data)')
} else {
    if (-not $resolved.data -or -not (Test-DojoUnder $provenancePath $resolved.data) -or (Test-DojoSame $provenancePath $resolved.data)) {
        $provenanceProblems.Add("$provenancePath is not inside the data root $($resolved.data)")
    }
    foreach ($p in @($prod.core, $prod.build, $prod.configs, $prod.modules) + $protected) {
        if (Test-DojoUnder $provenancePath $p) { $provenanceProblems.Add("$provenancePath is inside $p") }
    }
    if (Test-Path -LiteralPath $provenancePath -PathType Container) {
        $provenanceProblems.Add("$provenancePath is a directory")
    } elseif (Test-Path -LiteralPath $provenancePath -PathType Leaf) {
        $existing = $null
        try { $existing = Get-Content -LiteralPath $provenancePath -Raw | ConvertFrom-Json -AsHashtable } catch { $existing = $null }
        if ($existing -isnot [System.Collections.IDictionary] -or (Get-DojoValue $existing 'kind') -ne 'leeroy-dojo-provenance') {
            $provenanceProblems.Add("$provenancePath exists and is not a provenance manifest")
        }
    }
}
if ($provenanceProblems.Count -gt 0) { Add-DojoCheck 'FAIL' 'provenance path' ($provenanceProblems -join '; ') }

# --- provenance: written on request once the checks above pass, then the run stops ---

if ($WriteProvenance) {
    if ((Get-DojoFailCount) -gt 0) {
        Add-DojoCheck 'FAIL' 'provenance' 'not written: the live install, root and provenance path checks must pass first'
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
        foreach ($line in [System.IO.File]::ReadAllLines($cache)) {
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
$trainingAuth = @{}
$configsUsable = $false
if ($configs) {
    if ((Test-DojoUnder $configs $prod.configs) -or (Test-DojoUnder $configs $prod.build)) {
        Add-DojoCheck 'FAIL' 'config target' "$configs is a production configuration directory"
    } else {
        $configsUsable = $true
        $trainingConf = Read-DojoConf "$configs/worldserver.conf"
        $trainingAuth = Read-DojoConf "$configs/authserver.conf"
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
            # The core's updater writes its mysql option file (with the training password) into TempDir, or the
            # user's %TEMP% when it is empty, where the live server writes its own.
            $temp = if ($trainingConf.ContainsKey('TempDir') -and $trainingConf['TempDir']) { Resolve-DojoPath $trainingConf['TempDir'] } else { $null }
            if (-not $temp -or -not (Test-DojoUnder $temp $resolved.runtime)) {
                Add-DojoCheck 'FAIL' 'config TempDir' "'$temp' is not inside the dojo runtime $($resolved.runtime)"
            } else {
                Add-DojoCheck 'ok' 'config TempDir' $temp
            }
        }
    }
}

# --- training rules: each training config is its .dist plus the reviewed differences in training-rules.json ---

$rulesPath = if ($resolved.source) { "$($resolved.source)/scripts/dojo/training-rules.json" } else { $null }
$rules = $null
if ($rulesPath -and (Test-Path -LiteralPath $rulesPath -PathType Leaf)) {
    try { $rules = Get-Content -LiteralPath $rulesPath -Raw | ConvertFrom-Json -AsHashtable } catch { $rules = $null }
}
if ($rules -isnot [System.Collections.IDictionary] -or (Get-DojoValue $rules 'kind') -ne 'leeroy-dojo-training-rules') {
    Add-DojoCheck 'FAIL' 'training rules' "cannot read $rulesPath"
} elseif ($configsUsable) {
    $distDir = if ($resolved.build) { "$($resolved.build)/bin/RelWithDebInfo/configs" } else { $null }
    foreach ($file in $rules.files.Keys) {
        $spec = $rules.files[$file]
        $confPath = "$configs/$file"
        if (-not (Test-Path -LiteralPath $confPath -PathType Leaf)) {
            Add-DojoCheck 'note' "rules $file" "$confPath not provisioned yet"
            continue
        }
        $distPath = if ($distDir) { "$distDir/$($spec.dist)" } else { '' }
        if (-not $distPath -or -not (Test-Path -LiteralPath $distPath -PathType Leaf)) {
            Add-DojoCheck 'FAIL' "rules $file" "no $distPath to compare with, so ordinary rules cannot be verified"
            continue
        }
        $conf = Read-DojoConf $confPath
        $dist = Read-DojoConf $distPath
        $training = if ($spec.Contains('training')) { $spec.training } else { @{} }
        $ordinary = if ($spec.Contains('ordinary')) { $spec.ordinary } else { @{} }
        $isolation = @(if ($spec.Contains('isolation')) { $spec.isolation } else { @() })
        $problems = [System.Collections.Generic.List[string]]::new()
        foreach ($key in $ordinary.Keys) {
            $want = [string] $ordinary[$key]
            if ($dist[$key] -cne $want) { $problems.Add("$key`: the .dist says '$($dist[$key])', the rules expect the stock '$want'") }
            if ($conf[$key] -cne $want) { $problems.Add("$key is '$($conf[$key])', not the ordinary '$want'") }
        }
        foreach ($key in $training.Keys) {
            $want = [string] $training[$key].value
            if (-not $conf.ContainsKey($key) -or $conf[$key] -cne $want) { $problems.Add("$key is '$($conf[$key])', not the training '$want'") }
        }
        foreach ($key in $dist.Keys) {
            if (-not $conf.ContainsKey($key)) { $problems.Add("$key is missing, so a compiled-in default would apply") }
        }
        foreach ($key in $conf.Keys) {
            if ($training.Contains($key) -or $ordinary.Contains($key) -or $key -in $isolation) { continue }
            if (-not $dist.ContainsKey($key)) { $problems.Add("$key is not in the .dist (an unreviewed setting)") }
            elseif ($conf[$key] -cne $dist[$key]) { $problems.Add("$key is '$($conf[$key])', not the stock '$($dist[$key])'") }
        }
        if ($problems.Count -eq 0) {
            Add-DojoCheck 'ok' "rules $file" "$($dist.Count) stock keys, $($training.Count) training differences, $($ordinary.Count) ordinary rules held"
        } else {
            foreach ($p in ($problems | Select-Object -First 20)) { Add-DojoCheck 'FAIL' "rules $file" $p }
            if ($problems.Count -gt 20) { Add-DojoCheck 'FAIL' "rules $file" "and $($problems.Count - 20) more" }
        }
    }

    # mod_dojo.conf: this layout's instance, and a scenario manifest from the isolated source or data root whose
    # pinned hash is the one recorded next to it.
    $dojoConfPath = "$configs/modules/mod_dojo.conf"
    if (Test-Path -LiteralPath $dojoConfPath -PathType Leaf) {
        $dojoConf = Read-DojoConf $dojoConfPath
        $wantInstance = [string] (Get-DojoValue $DojoLayout 'instance' '')
        if ($dojoConf['Dojo.Instance'] -cne $wantInstance) {
            Add-DojoCheck 'FAIL' 'config Dojo.Instance' "'$($dojoConf['Dojo.Instance'])' is not the layout's instance '$wantInstance'"
        } else {
            Add-DojoCheck 'ok' 'config Dojo.Instance' $wantInstance
        }
        $manifest = [string] $dojoConf['Dojo.Scenario.Manifest']
        $pin = [string] $dojoConf['Dojo.Scenario.Sha256']
        if ($manifest -or $pin) {
            $m = Resolve-DojoPath $manifest
            $inside = $m -and ((Test-DojoUnder $m $resolved.source) -or (Test-DojoUnder $m $resolved.data))
            if (-not $m -or -not (Test-Path -LiteralPath $m -PathType Leaf) -or -not $inside) {
                Add-DojoCheck 'FAIL' 'config Dojo.Scenario.Manifest' "'$manifest' is not a file inside the source or data root"
            } elseif ($pin -cnotmatch '^[0-9a-f]{64}$') {
                Add-DojoCheck 'FAIL' 'config Dojo.Scenario.Sha256' 'is not 64 lower-case hex digits'
            } else {
                $pinFile = [System.IO.Path]::ChangeExtension($m, '.sha256')
                $recorded = if (Test-Path -LiteralPath $pinFile -PathType Leaf) { ((Get-Content -LiteralPath $pinFile -Raw) -split '\s+')[0] } else { '' }
                if ($recorded -and $recorded -cne $pin) {
                    Add-DojoCheck 'FAIL' 'config Dojo.Scenario.Sha256' "pins $($pin.Substring(0, 12)), but $pinFile records $($recorded.Substring(0, [Math]::Min(12, $recorded.Length)))"
                } else {
                    Add-DojoCheck 'ok' 'config Dojo.Scenario' "$m pinned at $($pin.Substring(0, 12))"
                }
            }
        }
    }
}

# --- module linkage ---

$modules = @(Get-DojoValue $DojoLayout 'modules' @('mod-playerbots', 'mod-dojo'))
foreach ($required in 'mod-dojo', 'mod-playerbots') {
    if ($required -notin $modules) { Add-DojoCheck 'FAIL' 'module set' "layout modules must include $required" }
}
$isolatedRoots = @(@($resolved.source, $resolved.playerbots, $resolved.core) | Where-Object { $_ })
$liveAreas = @(@($prod.core, $prod.build, $prod.configs, $prod.modules) + $protected | Where-Object { $_ })
# A link target outside every isolated checkout, or inside a live area, takes the dojo somewhere it must not go.
function Test-DojoLeaves([string] $Target) {
    $inside = @($isolatedRoots | Where-Object { Test-DojoUnder $Target $_ }).Count -gt 0
    $live = @($liveAreas | Where-Object { Test-DojoUnder $Target $_ }).Count -gt 0
    return (-not $inside) -or $live
}
# CLAUDE.md build trap 1: CMake includes only modules/<dir>/<dir>.cmake, and the generated loader calls
# Add<dir, dashes as underscores>Scripts(), so a module whose files do not follow its folder name is skipped
# silently or fails to link. mod-dojo also needs its .cmake (the variant switch its runtime refuses to compile
# without) and must ship off.
function Get-DojoModuleProblems([string] $Name, [string] $Root) {
    $problems = [System.Collections.Generic.List[string]]::new()
    $cmakeFiles = @(Get-ChildItem -LiteralPath $Root -Filter '*.cmake' -File -Force -ErrorAction SilentlyContinue)
    $own = Test-Path -LiteralPath "$Root/$Name.cmake" -PathType Leaf
    if ($cmakeFiles.Count -gt 0 -and -not $own) {
        $problems.Add("ships $($cmakeFiles[0].Name), but CMake includes only $Name.cmake, so it would be skipped silently")
    }
    $loader = 'Add' + $Name.Replace('-', '_') + 'Scripts'
    $pattern = [regex]::new("(?m)^\s*void\s+$loader\s*\(\s*\)\s*(\{|$)")
    $defined = $false
    if (Test-Path -LiteralPath "$Root/src" -PathType Container) {
        foreach ($file in [System.IO.Directory]::EnumerateFiles("$Root/src", '*.cpp', [System.IO.SearchOption]::AllDirectories)) {
            $text = [System.IO.File]::ReadAllText($file)
            if ($text.Contains($loader) -and $pattern.IsMatch($text)) { $defined = $true; break }
        }
    }
    if (-not $defined) { $problems.Add("src defines no $loader(), which the generated script loader calls (LNK2019)") }
    if ($Name -eq 'mod-dojo') {
        if (-not $own) { $problems.Add('has no mod-dojo.cmake') }
        $dist = Read-DojoConf "$Root/conf/mod_dojo.conf.dist"
        if (-not $dist.ContainsKey('Dojo.Enable') -or $dist['Dojo.Enable'] -ne '0') {
            $problems.Add('conf/mod_dojo.conf.dist does not ship Dojo.Enable = 0')
        }
    }
    return , $problems
}

$scanned = [System.Collections.Generic.List[object]]::new()
if ($resolved.core -and (Test-Path -LiteralPath "$($resolved.core)/modules" -PathType Container)) {
    $moduleRoots = @(@("$($resolved.source)/modules", $resolved.playerbots, $resolved.core) | Where-Object { $_ })
    $present = @{}
    foreach ($dir in Get-ChildItem -LiteralPath "$($resolved.core)/modules" -Directory -Force) {
        $name = $dir.Name
        $entry = ConvertTo-DojoPath $dir.FullName
        $target = Resolve-DojoPath $entry
        $present[$name] = $true
        $linked = Test-DojoLink $entry
        $inside = @($moduleRoots | Where-Object { Test-DojoUnder $target $_ }).Count -gt 0
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
        if (-not (Test-DojoLeaves $target)) {
            $scanned.Add([pscustomobject]@{ label = "module $name"; root = $target; skip = @() })
            if ($name -in $modules) {
                $rules = Get-DojoModuleProblems $name $target
                if ($rules.Count -gt 0) {
                    Add-DojoCheck 'FAIL' "module rules $name" ($rules -join '; ')
                } else {
                    Add-DojoCheck 'ok' "module rules $name" "files follow the folder name (loader Add$($name.Replace('-', '_'))Scripts)"
                }
            }
        }
    }
    foreach ($name in $modules) {
        if (-not $present.ContainsKey($name)) { Add-DojoCheck 'FAIL' "module $name" "missing from $($resolved.core)/modules" }
    }
} elseif ($resolved.core) {
    Add-DojoCheck 'FAIL' 'module linkage' "$($resolved.core)/modules does not exist"
}
# A link deeper inside the core or a module (src -> another checkout) would compile or load what the layout never
# named; every one must stay inside the isolated checkouts.
if ($resolved.core) {
    $scanned.Add([pscustomobject]@{ label = 'core'; root = $resolved.core; skip = @("$($resolved.core)/modules") })
}
foreach ($scan in $scanned) {
    $label = $scan.label
    $root = $scan.root
    $bad = 0
    foreach ($link in (Find-DojoLinks $root $scan.skip)) {
        $target = Get-DojoLinkTarget $link
        $target = if ($target) { Resolve-DojoPath $target } else { $null }
        if (-not $target -or (Test-DojoLeaves $target)) {
            $bad++
            Add-DojoCheck 'FAIL' "link $label" "$(ConvertTo-DojoPath $link.FullName) -> $(if ($target) { $target } else { 'nowhere' }) leaves the isolated checkouts"
        }
    }
    if ($bad -eq 0) { Add-DojoCheck 'ok' "links $label" "no link below $root leaves the isolated checkouts" }
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
            Invoke-DojoGit -C $root merge-base --is-ancestor $base HEAD 2>$null
            switch ($LASTEXITCODE) {
                0 { Add-DojoCheck 'ok' "baseline $name" "HEAD descends from $($base.Substring(0, 12))" }
                1 { Add-DojoCheck 'FAIL' "baseline $name" "HEAD does not descend from the recorded baseline $($base.Substring(0, 12))" }
                default { Add-DojoCheck 'FAIL' "baseline $name" "baseline $($base.Substring(0, 12)) is not in $root" }
            }
        }
    }
}

# --- databases: every live target must be readable, every training target on loopback and none of them live ---

$liveTargets = [System.Collections.Generic.List[object]]::new()
$live = Read-DojoDbTargets $prod.configs 'production'
foreach ($source in $live.unreadable) {
    Add-DojoCheck 'FAIL' "database $source" 'cannot be read as host;port;user;password;schema over TCP, so it cannot be told apart from a training target'
}
foreach ($t in $live.targets) { $liveTargets.Add($t) }
if ($live.targets.Count -eq 0) {
    Add-DojoCheck 'FAIL' 'database live targets' 'no live database target could be read from production.configs; nothing could be refused'
}
foreach ($d in @(Get-DojoValue $DojoLayout 'production.databases' @())) {
    if ($d -is [System.Collections.IDictionary]) {
        $liveTargets.Add([ordered]@{ source = 'production default'; host = [string] $d.host; port = [int] $d.port; user = ''; schema = [string] $d.schema })
    }
}
$liveSchemas = @{}
$liveSchemaNames = @{}
$liveUsers = @{}
foreach ($t in $liveTargets) {
    $endpoint = '{0}:{1}' -f (Get-DojoHostKey $t.host), $t.port
    $liveSchemas["$endpoint/$($t.schema)".ToLowerInvariant()] = $true
    $liveSchemaNames[$t.schema.ToLowerInvariant()] = $true
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
        $trainingTargets.Add([ordered]@{ source = "layout $kind"; host = [string] (Get-DojoValue $db 'host' ''); port = [int] (Get-DojoValue $db 'port' 0); user = [string] (Get-DojoValue $db 'user' ''); schema = [string] $schema })
    }
    if (-not (Get-DojoValue $db 'user')) { Add-DojoCheck 'FAIL' 'database' 'layout database.user is missing: the dojo needs its own restricted user' }
} else {
    Add-DojoCheck 'FAIL' 'database' 'layout has no database section'
}
if ($configsUsable) {
    $training = Read-DojoDbTargets $configs 'training'
    foreach ($source in $training.unreadable) {
        Add-DojoCheck 'FAIL' "database $source" 'cannot be read as host;port;user;password;schema over TCP loopback'
    }
    foreach ($t in $training.targets) { $trainingTargets.Add($t) }
}

$seen = @{}
foreach ($t in $trainingTargets) {
    $endpoint = '{0}:{1}' -f (Get-DojoHostKey $t.host), $t.port
    $where = "$($t.host):$($t.port)/$($t.schema)"
    $key = "$endpoint/$($t.schema)".ToLowerInvariant()
    if ($t.port -lt 1 -or $t.port -gt 65535) {
        Add-DojoCheck 'FAIL' "database $($t.source)" "$where has no valid port"
    } elseif (-not (Test-DojoLoopback $t.host)) {
        Add-DojoCheck 'FAIL' "database $($t.source)" "$where is not on loopback; the dojo's databases stay on this machine's loopback"
    } elseif ($liveSchemas.ContainsKey($key)) {
        Add-DojoCheck 'FAIL' "database $($t.source)" "writable live database target $where"
    } elseif ($liveSchemaNames.ContainsKey($t.schema.ToLowerInvariant()) -or $t.schema -like 'acore_*') {
        Add-DojoCheck 'FAIL' "database $($t.source)" "$where reuses a live schema name; dojo schemas never share one, on any server"
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
    $key = ('{0}:{1}/{2}' -f (Get-DojoHostKey $t.host), $t.port, $t.schema).ToLowerInvariant()
    if (-not $seen.ContainsKey($key) -and -not $liveSchemas.ContainsKey($key) -and -not $liveSchemaNames.ContainsKey($t.schema.ToLowerInvariant()) -and $t.schema -notlike 'acore_*') {
        Add-DojoCheck 'FAIL' "database $($t.source)" "$($t.host):$($t.port)/$($t.schema) is not one of the layout's schemas"
    }
}
foreach ($s in @(Get-DojoValue $DojoLayout 'database.bootstrap_sources' @())) {
    Add-DojoCheck 'note' 'database bootstrap' "$s may be read to provision the dojo schemas; runtime never writes it"
}
Add-DojoCheck 'note' 'database grants' 'not verified: preflight never connects to a database (python -m leeroy_dojo realm db-check does)'

# --- the dojo's own MySQL instance: a port no live database uses, its own folder, loopback only ---

$dbPort = [int] (Get-DojoValue $DojoLayout 'database.port' 0)
$liveDbPorts = @{ 3306 = 'MySQL default'; 33060 = 'MySQL X default' }
foreach ($t in $liveTargets) { $liveDbPorts[[int] $t.port] = "live $($t.source)" }
foreach ($p in @(Get-DojoValue $DojoLayout 'production.ports' @())) { if (-not $liveDbPorts.ContainsKey([int] $p)) { $liveDbPorts[[int] $p] = 'production' } }
if ($liveDbPorts.ContainsKey($dbPort)) {
    Add-DojoCheck 'FAIL' 'database port' "$dbPort is a live port ($($liveDbPorts[$dbPort])); the dojo runs its own MySQL instance"
} else {
    Add-DojoCheck 'ok' 'database port' "$dbPort (not a live database port)"
}
$server = Get-DojoValue $DojoLayout 'database.server'
if ($server -isnot [System.Collections.IDictionary]) {
    Add-DojoCheck 'FAIL' 'database server' 'layout database.server is missing: the dojo needs its own MySQL instance'
} else {
    $serverRoot = Resolve-DojoPath ([string] (Get-DojoValue $server 'root' ''))
    $basedir = Resolve-DojoPath ([string] (Get-DojoValue $server 'basedir' ''))
    $rootProblems = [System.Collections.Generic.List[string]]::new()
    if (-not $serverRoot) { $rootProblems.Add('database.server.root is missing') }
    foreach ($p in @($prod.core, $prod.build, $prod.configs, $prod.modules) + $protected) {
        if ($serverRoot -and $p -and ((Test-DojoUnder $serverRoot $p) -or (Test-DojoUnder $p $serverRoot))) { $rootProblems.Add("overlaps $p") }
    }
    if ($serverRoot -and $prod.install_root -and (Test-DojoUnder $prod.install_root $serverRoot)) { $rootProblems.Add("contains the live install") }
    if ($serverRoot -and $resolved.source -and (Test-DojoUnder $serverRoot $resolved.source)) { $rootProblems.Add('is inside the source checkout') }
    if ($rootProblems.Count -gt 0) {
        Add-DojoCheck 'FAIL' 'database server root' "$serverRoot $($rootProblems -join '; ')"
    } else {
        Add-DojoCheck 'ok' 'database server root' $serverRoot
    }
    $mysqld = if ($IsWindows) { "$basedir/bin/mysqld.exe" } else { "$basedir/bin/mysqld" }
    Add-DojoCheck $(if ($basedir -and (Test-Path -LiteralPath $mysqld -PathType Leaf)) { 'ok' } else { 'FAIL' }) 'database server binaries' $mysqld
    $myIni = if ($serverRoot) { "$serverRoot/my.ini" } else { $null }
    if ($myIni -and (Test-Path -LiteralPath $myIni -PathType Leaf)) {
        $ini = @{}
        $section = ''
        foreach ($line in [System.IO.File]::ReadAllLines($myIni)) {
            $l = $line.Trim()
            if (-not $l -or $l.StartsWith('#') -or $l.StartsWith(';')) { continue }
            if ($l -match '^\[(.+)\]$') { $section = $Matches[1].Trim().ToLowerInvariant(); continue }
            if ($section -ne 'mysqld') { continue }
            $k, $v = $l.Split('=', 2)
            $v = if ($null -eq $v) { '' } else { $v.Trim().Trim('"') }
            $ini[$k.Trim().ToLowerInvariant().Replace('_', '-')] = $v
        }
        $iniProblems = [System.Collections.Generic.List[string]]::new()
        if ($ini['port'] -ne "$dbPort") { $iniProblems.Add("port is '$($ini['port'])', not $dbPort") }
        if ($ini['bind-address'] -notin '127.0.0.1', '::1') { $iniProblems.Add("bind-address '$($ini['bind-address'])' is not loopback") }
        if (-not (Test-DojoSame (Resolve-DojoPath $ini['datadir']) "$serverRoot/data")) { $iniProblems.Add("datadir '$($ini['datadir'])' is not $serverRoot/data") }
        foreach ($k in 'mysqlx', 'named-pipe', 'shared-memory') {
            if ($ini[$k] -notin 'OFF', '0') { $iniProblems.Add("$k is not off") }
        }
        if ($ini.ContainsKey('password')) { $iniProblems.Add('holds a password') }
        if ($iniProblems.Count -gt 0) {
            Add-DojoCheck 'FAIL' 'database my.ini' "$myIni $($iniProblems -join '; ')"
        } else {
            Add-DojoCheck 'ok' 'database my.ini' "$myIni (127.0.0.1:$dbPort, own datadir, X plugin, pipes and shared memory off)"
        }
    } else {
        Add-DojoCheck 'note' 'database my.ini' "not initialized yet (python -m leeroy_dojo realm db-init)"
    }
}

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

# --- ports: what the layout requests and what the training configs would really bind ---

$livePorts = @{}
foreach ($p in @(Get-DojoValue $DojoLayout 'production.ports' @())) { $livePorts[[int] $p] = 'production' }
foreach ($key in $DojoDefaultPorts.Keys) { $livePorts[[int] $DojoDefaultPorts[$key]] = "live $key default" }
foreach ($conf in @($liveConf, (Read-DojoConf "$($prod.configs)/authserver.conf"))) {
    foreach ($key in $DojoDefaultPorts.Keys) {
        $n = 0
        if ($conf.ContainsKey($key) -and [int]::TryParse($conf[$key], [ref] $n)) { $livePorts[$n] = "live $key" }
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
# The training servers bind what their own configs say (or AzerothCore's default when a key is missing), whatever
# the layout requests; authserver.conf carries RealmServerPort.
$bound = @{}
$configPorts = @(
    @('worldserver.conf', $trainingConf, 'WorldServerPort', 'world', $true),
    @('authserver.conf', $trainingAuth, 'RealmServerPort', 'auth', $true),
    @('worldserver.conf', $trainingConf, 'Ra.Port', 'ra', ($trainingConf['Ra.Enable'] -eq '1')),
    @('worldserver.conf', $trainingConf, 'SOAP.Port', $null, ($trainingConf['SOAP.Enabled'] -eq '1'))
)
foreach ($entry in $configPorts) {
    $file, $conf, $key, $layoutName, $enabled = $entry
    if ($conf.Count -eq 0) {
        if ($configsUsable -and $layoutName) { Add-DojoCheck 'note' "config $key" "$configs/$file not provisioned yet" }
        continue
    }
    if (-not $enabled) { continue }
    $text = if ($conf.ContainsKey($key)) { $conf[$key] } else { "$($DojoDefaultPorts[$key])" }
    $shown = if ($conf.ContainsKey($key)) { "$file $key = $text" } else { "$file has no $key, so the default $text" }
    $n = 0
    if (-not [int]::TryParse($text, [ref] $n) -or $n -lt 1 -or $n -gt 65535) {
        Add-DojoCheck 'FAIL' "config $key" "$shown is not a port number"
    } elseif ($livePorts.ContainsKey($n)) {
        Add-DojoCheck 'FAIL' "config $key" "$shown is a production port ($($livePorts[$n]))"
    } elseif ($bound.ContainsKey($n)) {
        Add-DojoCheck 'FAIL' "config $key" "$shown is also $($bound[$n])"
    } elseif ($listening.ContainsKey($n)) {
        Add-DojoCheck 'FAIL' "config $key" "$shown is already in use"
    } elseif ($layoutName -and $ports.Contains($layoutName) -and "$($ports[$layoutName])" -ne "$n") {
        Add-DojoCheck 'FAIL' "config $key" "$shown differs from the layout's $layoutName port $($ports[$layoutName])"
    } else {
        Add-DojoCheck 'ok' "config $key" $shown
    }
    $bound[$n] = $key
}

# --- toolchains (reported, never required here) ---

function Get-DojoToolVersion([string] $Command, [string[]] $Arguments) {
    if (-not (Get-Command $Command -CommandType Application -ErrorAction SilentlyContinue)) { return $null }
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
if ((Get-DojoFailCount) -gt 0) { exit 1 }
exit 0
