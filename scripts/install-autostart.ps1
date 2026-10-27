[CmdletBinding(SupportsShouldProcess=$true, ConfirmImpact='High')]
param(
    [string]$TaskName='AutoBoya-AutoStart',
    [string]$DataHome=(Join-Path $env:USERPROFILE '.autoboya'),
    [switch]$StartNow
)
$ErrorActionPreference='Stop'
$projectRoot=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$python=Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Install the project virtual environment first.' }
$homeRoot=[IO.Path]::GetFullPath($DataHome)
if ($homeRoot -eq [IO.Path]::GetPathRoot($homeRoot)) { throw 'Data home cannot be a filesystem root.' }
$workerScript=Join-Path $PSScriptRoot 'worker.py'
$identity=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger=New-ScheduledTaskTrigger -AtLogOn -User $identity
$trigger.Delay='PT30S'
$action=New-ScheduledTaskAction -Execute $python -Argument ('"{0}" --home "{1}"' -f $workerScript,$homeRoot) -WorkingDirectory $projectRoot
$settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal=New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$task=New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal
if ($PSCmdlet.ShouldProcess($TaskName,'Register login task and desktop status shortcut')) {
    Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
    $shell=New-Object -ComObject WScript.Shell
    $desktop=[Environment]::GetFolderPath('Desktop')
    $shortcut=$shell.CreateShortcut((Join-Path $desktop 'BUAA Automation Status.lnk'))
    $shortcut.TargetPath=$env:ComSpec
    $shortcut.Arguments='/c ""' + (Join-Path $PSScriptRoot 'status.cmd') + '" "' + $homeRoot + '""'
    $shortcut.WorkingDirectory=$projectRoot
    $shortcut.Save()
    if ($StartNow) { Start-ScheduledTask -TaskName $TaskName }
    Write-Output 'Login task and status shortcut installed.'
}
