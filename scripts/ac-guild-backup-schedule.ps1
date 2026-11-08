<#
.SYNOPSIS
Installs, removes, shows or inspects a per-user daily guild backup task.
.DESCRIPTION
Nothing is installed by default. The task uses the mind venv's pythonw.exe to run
the same engine as ac-guild-backup.sh without a console window. It runs only while
the owner is logged on, with that user's mounted-share access, no stored password,
and Limited privileges. A missed run starts when the user next logs on.
-Show prints the proposed definition without registering or running anything.
-Status (default) reports the last task result; zero means the backup verified.
.EXAMPLE
powershell -NoProfile -File scripts\ac-guild-backup-schedule.ps1 -Show -At '03:00'
.EXAMPLE
powershell -NoProfile -File scripts\ac-guild-backup-schedule.ps1 -Install -At '03:00' -WithDb
#>
[CmdletBinding(DefaultParameterSetName = 'Status')]
param(
    [Parameter(ParameterSetName = 'Install', Mandatory = $true)][switch] $Install,
    [Parameter(ParameterSetName = 'Remove', Mandatory = $true)][switch] $Remove,
    [Parameter(ParameterSetName = 'Show', Mandatory = $true)][switch] $Show,
    [Parameter(ParameterSetName = 'Status')][switch] $Status,
    [string] $Root = '',
    [string] $Config = '',
    [string] $DataDir = '',
    [string] $VoiceDataDir = '',
    [ValidatePattern('^([01][0-9]|2[0-3]):[0-5][0-9]$')][string] $At = '03:00',
    [switch] $WithDb
)

$ErrorActionPreference = 'Stop'
if (-not $Root) { $Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path) }
$TaskName = 'leeroy-guild-backup'
$TaskPath = '\'

function Get-GuildBackupTask {
    Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
}

function ConvertTo-TaskArgument([string] $Value) {
    # Paths cannot contain quotes/newlines. Trim a trailing slash before quoting.
    if ($Value -match '["\r\n]') { throw 'Invalid task path.' }
    return '"' + $Value.TrimEnd('\', '/') + '"'
}

switch ($PSCmdlet.ParameterSetName) {
    { $_ -in 'Install', 'Show' } {
        if (-not $Config) { $Config = Join-Path $env:LOCALAPPDATA 'leeroy\backup.toml' }
        if (-not $DataDir) {
            $DataDir = if ($env:LEEROY_MIND_DATA) { $env:LEEROY_MIND_DATA } else { Join-Path $Root 'mind-data' }
        }
        if (-not $VoiceDataDir) {
            $VoiceDataDir = if ($env:LEEROY_VOICE_DATA) { $env:LEEROY_VOICE_DATA } else { Join-Path $Root 'voice-data' }
        }
        $pythonw = Join-Path $Root 'mind\.venv\Scripts\pythonw.exe'
        $engine = Join-Path $Root 'scripts\guild_backup.py'
        $arguments = (ConvertTo-TaskArgument $engine) + ' --root ' + (ConvertTo-TaskArgument $Root) +
            ' --config ' + (ConvertTo-TaskArgument $Config) +
            ' --mind-data ' + (ConvertTo-TaskArgument $DataDir) +
            ' --voice-data ' + (ConvertTo-TaskArgument $VoiceDataDir)
        if ($WithDb) { $arguments += ' --with-db' }
        if ($Show) {
            "$TaskName daily at $At (local time), only while the current user is logged on"
            "    runs $pythonw $arguments"
            '    Limited privileges; missed runs start when available; no overlapping runs'
            return
        }
        foreach ($path in @($Root, $Config, $pythonw, $engine, $DataDir)) {
            if (-not (Test-Path -LiteralPath $path)) { throw 'A required backup path is missing; check -Show.' }
            if (-not [IO.Path]::IsPathRooted($path)) { throw 'Task paths must be absolute.' }
        }
        $action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $Root
        $trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.Add([TimeSpan]::Parse($At)))
        $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Hours 6)
        $existed = $null -ne (Get-GuildBackupTask)
        $null = Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Action $action `
            -Trigger $trigger -Principal $principal -Settings $settings -Force `
            -Description 'Verified guild backup to the destination in the owner profile; daily while logged on.'
        if ($existed) { "replaced $TaskName" } else { "installed $TaskName" }
        "    daily at $At while the current user is logged on; inspect results with -Status"
    }
    'Remove' {
        if ($null -eq (Get-GuildBackupTask)) {
            "$TaskName is not installed"
        } else {
            Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Confirm:$false
            "removed $TaskName"
        }
    }
    default {
        $task = Get-GuildBackupTask
        if ($null -eq $task) {
            "$TaskName is not installed (preview with -Show; install with -Install)"
            return
        }
        $info = $task | Get-ScheduledTaskInfo
        "$TaskName $($task.State)"
        "    last run $($info.LastRunTime) (result $($info.LastTaskResult); 0 means verified)"
        "    next run $($info.NextRunTime)"
    }
}
