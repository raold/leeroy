<#
.SYNOPSIS
Installs, removes or shows the scheduled task that runs the live install's guild console.

.DESCRIPTION
The task, "leeroy-console" for the current user, starts the guild console at logon and keeps
it running for the whole session, with no window: the mind venv's pythonw.exe runs
`python -m leeroy_mind console` with the stable key in KeyFile (--key-file, made private on
the first start) and the tailnet name AllowHost (--allow-host), so the owner's devices reach it
at https://<AllowHost>/ through `tailscale serve`. It runs only while that user is logged on,
with Limited rights, at normal priority (4; a task's default, 7, is below normal and what it
starts would inherit it), one instance at a time (IgnoreNew).

If the console fails to start or exits, Task Scheduler starts it again: up to RestartCount
times a minute apart, and a repeating trigger every few minutes besides, which IgnoreNew
skips while it runs. Its output goes nowhere: the start line that holds the key is not kept.
The bookmark is https://<AllowHost>/#key=<the key in KeyFile>; `python -m leeroy_mind console
rotate-key --key-file <KeyFile> --allow-host <AllowHost>` writes a new key and prints the URLs,
and the console takes it at its next start.

-Install creates the task, or replaces it with the same definition, so running it again is
safe; the console starts within a minute (a console already on its port makes that start
exit, and the next one tries again). -Show prints, as JSON, the task -Install would register,
and registers nothing. -Remove deletes the task if it is there; a console it started keeps
running until logoff. -Status (the default) shows it.

.PARAMETER AllowHost
The tailnet name the console answers to besides 127.0.0.1 (this PC's MagicDNS name,
<pc>.<tailnet>.ts.net). Required by -Install and -Show.

.PARAMETER Root
The install checkout (default: the folder above this script).

.PARAMETER DataDir
The mind's data dir (default: $env:LEEROY_MIND_DATA, else mind-data in Root).

.PARAMETER KeyFile
The console's key file (default: $env:LOCALAPPDATA\leeroy\console.key, under the user's
profile).

.EXAMPLE
powershell -NoProfile -File scripts\ac-console-schedule.ps1 -Install -AllowHost <pc>.<tailnet>.ts.net
#>
[CmdletBinding(DefaultParameterSetName = 'Status')]
param(
    [Parameter(ParameterSetName = 'Install', Mandatory = $true)][switch] $Install,
    [Parameter(ParameterSetName = 'Show', Mandatory = $true)][switch] $Show,
    [Parameter(ParameterSetName = 'Remove', Mandatory = $true)][switch] $Remove,
    [Parameter(ParameterSetName = 'Status')][switch] $Status,
    [Parameter(ParameterSetName = 'Install', Mandatory = $true)]
    [Parameter(ParameterSetName = 'Show', Mandatory = $true)]
    [string] $AllowHost,
    [string] $Root = '',
    [string] $DataDir = '',
    [string] $KeyFile = ''
)

$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 has no $PSScriptRoot while it binds the parameters: the default is set here.
if (-not $Root) { $Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path) }
$TaskName = 'leeroy-console'
$TaskPath = '\'
$RetryMinutes = 5
$RestartCount = 3
$Priority = 4

function Get-ConsoleTask {
    Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
}

function Resolve-Dir([string] $Path, [string] $What) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$What not found: $Path"
    }
    # No trailing backslash: before a closing quote it would escape it.
    return (Resolve-Path -LiteralPath $Path).ProviderPath.TrimEnd('\')
}

# The task -Install registers, built in memory only: New-ScheduledTask registers nothing.
function New-ConsoleTask {
    $script:Root = Resolve-Dir $Root 'install root'
    if (-not $DataDir) {
        $script:DataDir = if ($env:LEEROY_MIND_DATA) { $env:LEEROY_MIND_DATA } else { Join-Path $Root 'mind-data' }
    }
    $script:DataDir = Resolve-Dir $DataDir 'mind data dir'
    if (-not $KeyFile) {
        if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is not set: pass -KeyFile' }
        $script:KeyFile = Join-Path $env:LOCALAPPDATA 'leeroy\console.key'
    }
    $script:KeyFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($KeyFile)
    $pythonw = Join-Path $Root 'mind\.venv\Scripts\pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
        throw "no mind venv: $pythonw is missing"
    }
    if ($AllowHost -notmatch '^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$') {
        throw "not a host name: $AllowHost"
    }
    $arguments = '-m leeroy_mind console --root "{0}" --data-dir "{1}" --key-file "{2}" --allow-host {3}' -f `
        $Root, $DataDir, $KeyFile, $AllowHost
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments `
        -WorkingDirectory (Join-Path $Root 'mind')
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $atLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
    # No -RepetitionDuration: the repetition goes on indefinitely. While the console runs,
    # IgnoreNew skips each repetition; after it has gone, the next one starts it again.
    $retry = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $RetryMinutes)
    # Interactive: only while the user is logged on, in their session.
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    # No time limit ([TimeSpan]::Zero): the default would end it after 72 hours.
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd `
        -RestartCount $RestartCount -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -Priority $Priority
    return New-ScheduledTask -Action $action -Trigger @($atLogon, $retry) -Principal $principal `
        -Settings $settings `
        -Description "The leeroy guild console at logon, hidden, for 127.0.0.1 and https://$AllowHost/ (tailscale serve)."
}

switch ($PSCmdlet.ParameterSetName) {
    'Show' {
        $task = New-ConsoleTask
        $a = $task.Actions[0]
        [pscustomobject]@{
            TaskName = $TaskName
            Execute = $a.Execute
            Arguments = $a.Arguments
            WorkingDirectory = $a.WorkingDirectory
            Triggers = @($task.Triggers | ForEach-Object {
                [pscustomobject]@{ Kind = $_.CimClass.CimClassName; UserId = $_.UserId
                                   Repetition = "$($_.Repetition.Interval)" } })
            Principal = [pscustomobject]@{ UserId = $task.Principal.UserId
                                           LogonType = "$($task.Principal.LogonType)"
                                           RunLevel = "$($task.Principal.RunLevel)" }
            Settings = [pscustomobject]@{
                MultipleInstances = "$($task.Settings.MultipleInstances)"
                ExecutionTimeLimit = $task.Settings.ExecutionTimeLimit
                RestartCount = $task.Settings.RestartCount
                RestartInterval = $task.Settings.RestartInterval
                Priority = $task.Settings.Priority
                StartWhenAvailable = $task.Settings.StartWhenAvailable
                DisallowStartIfOnBatteries = $task.Settings.DisallowStartIfOnBatteries
                StopIfGoingOnBatteries = $task.Settings.StopIfGoingOnBatteries }
        } | ConvertTo-Json -Depth 4
    }
    'Install' {
        $task = New-ConsoleTask
        $folder = Split-Path -Parent $KeyFile
        if (-not (Test-Path -LiteralPath $folder -PathType Container)) {
            $null = New-Item -ItemType Directory -Path $folder
        }
        $existed = $null -ne (Get-ConsoleTask)
        $null = Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -InputObject $task -Force
        $user = $task.Principal.UserId
        if ($existed) { "replaced $TaskName" } else { "installed $TaskName" }
        "    at logon while $user is logged on, started again within $RetryMinutes minutes if it stops"
        "    $($task.Actions[0].Execute) $($task.Actions[0].Arguments)"
        "    key file $KeyFile (made at its first start)"
        "    bookmark https://$AllowHost/#key=<the key in that file>"
    }
    'Remove' {
        $task = Get-ConsoleTask
        if ($null -eq $task) {
            "$TaskName is not installed"
        } else {
            $running = $task.State -eq 'Running'
            Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Confirm:$false
            "removed $TaskName"
            if ($running) { "    its console keeps running until you log off" }
        }
    }
    default {
        $task = Get-ConsoleTask
        if ($null -eq $task) {
            "$TaskName is not installed (install it with -Install -AllowHost <name>)"
            return
        }
        $info = $task | Get-ScheduledTaskInfo
        "$TaskName $($task.State)"
        "    last run     $($info.LastRunTime) (result $($info.LastTaskResult))"
        "    next run     $($info.NextRunTime)"
        foreach ($a in $task.Actions) { "    runs         $($a.Execute) $($a.Arguments)" }
    }
}
