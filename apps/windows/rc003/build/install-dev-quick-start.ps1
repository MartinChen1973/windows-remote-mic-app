#requires -Version 5.1
<#
.SYNOPSIS
    One-time (or repeat) install of instant Remote Mic dev launchers.

.DESCRIPTION
    Writes a silent launcher under %LOCALAPPDATA%\RemoteMic\DevLauncher,
    places a primary shortcut on the desktop and in the Start menu, sets a
    global hotkey (default Ctrl+Alt+M), and adds secondary shortcuts in a
    program group (setup, stop, logs, project folder).
#>

param(
    [switch]$SkipDesktop,
    [string]$Hotkey = "CTRL+ALT+M"
)

$ErrorActionPreference = "Stop"

$BuildRoot = $PSScriptRoot
$RC003Root = (Resolve-Path (Join-Path $BuildRoot "..")).Path
$RepoRoot = (Resolve-Path (Join-Path $BuildRoot "..\..\..\..")).Path
$SetupBat = Join-Path $RepoRoot "setup.bat"
$RunScript = Join-Path $BuildRoot "run-dev.ps1"
$StopScript = Join-Path $BuildRoot "stop-dev.ps1"
$VenvPythonw = Join-Path $RC003Root ".venv\Scripts\pythonw.exe"
$LogsDir = Join-Path $env:LOCALAPPDATA "RemoteMic\RC003\logs"
$AppIcon = Join-Path $RC003Root "src\ovb_rc003\assets\icons\remote-mic.ico"
$LauncherRoot = Join-Path $env:LOCALAPPDATA "RemoteMic\DevLauncher"
$LauncherVbs = Join-Path $LauncherRoot "open-settings.vbs"

$ProductName = -join @([char]0x65E0, [char]0x7EBF, [char]0x9EA6)
$DevLabel = -join @([char]0x5F00, [char]0x53D1, [char]0x7248)
$GroupName = "$ProductName $DevLabel"
$PrimaryLinkName = "$ProductName.lnk"
$ProgramsRoot = [Environment]::GetFolderPath("Programs")
if (-not $ProgramsRoot) {
    throw "Windows did not return a Start Menu Programs folder for the current user."
}
$StartGroup = Join-Path $ProgramsRoot $GroupName

$WscriptExe = Join-Path $env:SystemRoot "System32\wscript.exe"
$WindowsPowerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$Explorer = Join-Path $env:SystemRoot "explorer.exe"

function Set-ShortcutIcon {
    param($Shortcut)
    if (Test-Path -LiteralPath $AppIcon -PathType Leaf) {
        $Shortcut.IconLocation = "$AppIcon,0"
    }
}

function New-DevShortcut {
    param(
        [Parameter(Mandatory = $true)][string]$LinkPath,
        [Parameter(Mandatory = $true)][string]$TargetPath,
        [string]$Arguments = "",
        [string]$WorkingDirectory = "",
        [string]$Description = "",
        [string]$ShortcutHotkey = ""
    )

    $parent = Split-Path -Parent $LinkPath
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }

    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($LinkPath)
    $Shortcut.TargetPath = $TargetPath
    if ($Arguments) { $Shortcut.Arguments = $Arguments }
    if ($WorkingDirectory) { $Shortcut.WorkingDirectory = $WorkingDirectory }
    if ($Description) { $Shortcut.Description = $Description }
    if ($ShortcutHotkey) {
        try {
            $Shortcut.Hotkey = $ShortcutHotkey
        } catch {
            Write-Warning "Could not assign hotkey $ShortcutHotkey to $LinkPath"
        }
    }
    Set-ShortcutIcon -Shortcut $Shortcut
    $Shortcut.Save()
}

if (-not (Test-Path -LiteralPath $SetupBat -PathType Leaf)) {
    throw "setup.bat was not found at the repository root: $RepoRoot"
}

New-Item -ItemType Directory -Path $LauncherRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $VenvPythonw -PathType Leaf)) {
    Write-Warning "The local .venv is missing; run setup.bat once, then run this script again."
}

$vbs = @"
Option Explicit
Dim shell, env, pythonw, rc003
pythonw = "$($VenvPythonw -replace '\\', '\\')"
rc003 = "$($RC003Root -replace '\\', '\\')"
Set shell = CreateObject("WScript.Shell")
Set env = shell.Environment("PROCESS")
env("PYTHONPATH") = rc003 & "\src"
env("PYTHONDONTWRITEBYTECODE") = "1"
env("REMOTE_MIC_DEV_SESSION") = "1"
shell.CurrentDirectory = rc003
If CreateObject("Scripting.FileSystemObject").FileExists(pythonw) Then
  shell.Run """" & pythonw & """ -m ovb_rc003 --settings --remote-mic-dev-session", 0, False
Else
  shell.Run """$($WindowsPowerShellExe -replace '\\', '\\')"" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""$($RunScript -replace '\\', '\\')""", 0, False
End If
"@
Set-Content -LiteralPath $LauncherVbs -Value $vbs -Encoding ASCII

$launchTarget = $WscriptExe
$launchArgs = "//B //Nologo ""$LauncherVbs"""
$launchDescription = "Open Remote Mic settings (this source checkout)"

New-Item -ItemType Directory -Path $StartGroup -Force | Out-Null

$PrimaryStart = Join-Path $ProgramsRoot $PrimaryLinkName
New-DevShortcut `
    -LinkPath $PrimaryStart `
    -TargetPath $launchTarget `
    -Arguments $launchArgs `
    -WorkingDirectory $RC003Root `
    -Description $launchDescription `
    -ShortcutHotkey $Hotkey

if (-not $SkipDesktop) {
    $Desktop = [Environment]::GetFolderPath("Desktop")
    if ($Desktop) {
        New-DevShortcut `
            -LinkPath (Join-Path $Desktop $PrimaryLinkName) `
            -TargetPath $launchTarget `
            -Arguments $launchArgs `
            -WorkingDirectory $RC003Root `
            -Description $launchDescription `
            -ShortcutHotkey $Hotkey
    }
}

New-DevShortcut `
    -LinkPath (Join-Path $StartGroup "Open settings (instant).lnk") `
    -TargetPath $launchTarget `
    -Arguments $launchArgs `
    -WorkingDirectory $RC003Root `
    -Description $launchDescription `
    -ShortcutHotkey $Hotkey

$RunArguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""$RunScript"""
$StopArguments = "-NoProfile -ExecutionPolicy Bypass -File ""$StopScript"""

New-DevShortcut `
    -LinkPath (Join-Path $StartGroup "Setup and launch.lnk") `
    -TargetPath $SetupBat `
    -WorkingDirectory $RepoRoot `
    -Description "Refresh .venv if needed, verify, then open settings"

New-DevShortcut `
    -LinkPath (Join-Path $StartGroup "Stop dev session.lnk") `
    -TargetPath $WindowsPowerShellExe `
    -Arguments $StopArguments `
    -WorkingDirectory $RC003Root `
    -Description "Stop the marked development session for this checkout"

New-DevShortcut `
    -LinkPath (Join-Path $StartGroup "App logs folder.lnk") `
    -TargetPath $Explorer `
    -Arguments """$LogsDir""" `
    -Description "Open application logs"

New-DevShortcut `
    -LinkPath (Join-Path $StartGroup "Project folder.lnk") `
    -TargetPath $Explorer `
    -Arguments """$RepoRoot""" `
    -Description "Open the repository root"

Write-Host ""
Write-Host "Instant launchers installed."
Write-Host "  Hotkey     : $Hotkey  (from anywhere)"
Write-Host "  Start      : Win -> type ""$ProductName"" -> Enter"
Write-Host "  Desktop    : $PrimaryLinkName"
Write-Host "  Taskbar    : right-click the desktop shortcut -> Pin to taskbar"
Write-Host "  Launcher   : $LauncherVbs"
Write-Host ""
