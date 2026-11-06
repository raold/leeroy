<#
.SYNOPSIS
Installs, removes or shows the scheduled task that runs the live install's health check.

.DESCRIPTION
The task, "leeroy-health" for the current user, runs `python -m leeroy_mind health --toast`
every 15 minutes, only while that user is logged on, with no window: the mind venv's
pythonw.exe runs it directly (the check's own subprocesses are windowless too). A failure
shows a Windows toast once until it clears (health-state.json in the mind's data dir).

-Install creates the task, or replaces it with the same definition, so running it again is
safe. -Remove deletes it if it is there. -Status (the default) shows it. Nothing here runs the
check itself.

.PARAMETER Root
The install checkout (default: the folder above this script).

.PARAMETER DataDir
The mind's data dir (default: $env:LEEROY_MIND_DATA, else mind-data in Root).

.EXAMPLE
powershell -NoProfile -File scripts\ac-health-schedule.ps1 -Install
#>
[CmdletBinding(DefaultParameterSetName = 'Status')]
param(
    [Parameter(ParameterSetName = 'Install', Mandatory = $true)][switch] $Install,
    [Parameter(ParameterSetName = 'Remove', Mandatory = $true)][switch] $Remove,
    [Parameter(ParameterSetName = 'Status')][switch] $Status,
    [string] $Root = '',
    [string] $DataDir = ''
)

$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 has no $PSScriptRoot while it binds the parameters: the default is set here.
if (-not $Root) { $Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path) }
$TaskName = 'leeroy-health'
$TaskPath = '\'
$EveryMinutes = 15

function Get-HealthTask {
    Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
}

function Resolve-Dir([string] $Path, [string] $What) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$What not found: $Path"
    }
    # No trailing backslash: before a closing quote it would escape it.
    return (Resolve-Path -LiteralPath $Path).ProviderPath.TrimEnd('\')
}

switch ($PSCmdlet.ParameterSetName) {
    'Install' {
        $Root = Resolve-Dir $Root 'install root'
        if (-not $DataDir) {
            $DataDir = if ($env:LEEROY_MIND_DATA) { $env:LEEROY_MIND_DATA } else { Join-Path $Root 'mind-data' }
        }
        $DataDir = Resolve-Dir $DataDir 'mind data dir'
        $pythonw = Join-Path $Root 'mind\.venv\Scripts\pythonw.exe'
        if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
            throw "no mind venv: $pythonw is missing"
        }
        $arguments = '-m leeroy_mind health --root "{0}" --data-dir "{1}" --toast' -f $Root, $DataDir
        $action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments `
            -WorkingDirectory (Join-Path $Root 'mind')
        # No -RepetitionDuration: the repetition goes on indefinitely.
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
            -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
        $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        # Interactive: only while the user is logged on, in their session (the toast needs it).
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
        $existed = $null -ne (Get-HealthTask)
        $null = Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Action $action `
            -Trigger $trigger -Principal $principal -Settings $settings -Force `
            -Description "Read-only health check of the leeroy install every $EveryMinutes minutes; a toast on a new failure."
        if ($existed) { "replaced $TaskName" } else { "installed $TaskName" }
        "    every $EveryMinutes minutes while $user is logged on"
        "    $pythonw $arguments"
    }
    'Remove' {
        if ($null -eq (Get-HealthTask)) {
            "$TaskName is not installed"
        } else {
            Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Confirm:$false
            "removed $TaskName"
        }
    }
    default {
        $task = Get-HealthTask
        if ($null -eq $task) {
            "$TaskName is not installed (install it with -Install)"
            return
        }
        $info = $task | Get-ScheduledTaskInfo
        "$TaskName $($task.State)"
        "    last run     $($info.LastRunTime) (result $($info.LastTaskResult))"
        "    next run     $($info.NextRunTime)"
        foreach ($a in $task.Actions) { "    runs         $($a.Execute) $($a.Arguments)" }
    }
}
