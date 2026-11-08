# No ScheduledTasks cmdlet is ever called: Show is read-only; status/removal use fakes.
$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'ac-guild-backup-schedule.ps1'
$tokens = $null
$parseErrors = $null
$null = [Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref] $tokens, [ref] $parseErrors)
if ($parseErrors.Count) { throw 'Schedule script has parse errors.' }

$fixture = [pscustomobject]@{ Present = $false; Removed = 0; Registered = 0 }
function Get-ScheduledTask {
    param($TaskName, $TaskPath, $ErrorAction)
    if ($TaskName -ne 'leeroy-guild-backup' -or $TaskPath -ne '\') { throw 'Wrong task identity.' }
    if ($fixture.Present) { return [pscustomobject]@{ State = 'Ready' } }
}
function Get-ScheduledTaskInfo {
    [CmdletBinding()]
    param([Parameter(ValueFromPipeline = $true)] $InputObject)
    process { [pscustomobject]@{ LastRunTime = 'fixture'; NextRunTime = 'fixture'; LastTaskResult = 0 } }
}
function Register-ScheduledTask { $fixture.Registered++; throw 'Registration is forbidden in this test.' }
function Unregister-ScheduledTask {
    param($TaskName, $TaskPath, $Confirm)
    if ($TaskName -ne 'leeroy-guild-backup' -or $TaskPath -ne '\' -or $Confirm) { throw 'Unsafe removal.' }
    $fixture.Removed++
}

$preview = & $scriptPath -Show -Root $PSScriptRoot -Config (Join-Path $PSScriptRoot 'synthetic.toml') `
    -DataDir $PSScriptRoot -VoiceDataDir $PSScriptRoot -At '04:15' -WithDb
if (($preview -join "`n") -notmatch 'daily at 04:15' -or ($preview -join "`n") -notmatch '--with-db') {
    throw 'Show did not describe the daily task.'
}
if ((& $scriptPath -Status) -notmatch 'not installed') { throw 'Missing task status was wrong.' }
$null = & $scriptPath -Remove
if ($fixture.Removed -ne 0) { throw 'Attempted to remove an absent task.' }
$fixture.Present = $true
if (((& $scriptPath -Status) -join "`n") -notmatch 'Ready') { throw 'Installed task status was wrong.' }
$null = & $scriptPath -Remove
if ($fixture.Removed -ne 1) { throw 'Did not remove the intended fake task.' }
$rejected = $false
try { $null = & $scriptPath -Show -At '25:00' } catch { $rejected = $true }
if (-not $rejected) { throw 'Invalid daily time was accepted.' }
$rejected = $false
try {
    $null = & $scriptPath -Install -Root $PSScriptRoot -Config (Join-Path $PSScriptRoot 'absent-synthetic.toml') `
        -DataDir $PSScriptRoot -VoiceDataDir $PSScriptRoot
} catch { $rejected = $true }
if (-not $rejected -or $fixture.Registered) { throw 'Install must reject missing inputs before registering.' }
'Guild backup schedule: parser and 7 synthetic checks passed; no task registered.'
