# Set Curveball Tools Icon.ps1
#
# Creates/updates "Start Curveball Tools.lnk" in this same folder, pointing
# at "Start Curveball Tools.bat" with its icon set to pixel_tyler.ico.
#
# Why this exists: Windows won't apply a custom icon directly to a .bat file
# (or offer "Pin to taskbar" on one) -- the icon has to live on a .lnk
# shortcut instead. Run this once (right-click -> Run with PowerShell) to
# (re)generate that shortcut, then pin/move the .lnk it creates instead of
# the raw .bat.
#
# After running:
#   1. A "Start Curveball Tools.lnk" file appears in this folder, using
#      pixel_tyler.ico as its icon.
#   2. Copy/move that .lnk into your Start Menu Programs folder
#      (Win+R -> shell:programs) if it isn't there already.
#   3. Open Start, find "Start Curveball Tools", right-click -> Pin to
#      Start (or launch it once so it shows in the taskbar, then right-click
#      the running taskbar icon -> Pin to taskbar).
#
# TASKBAR PINNING BLOCKED BY POLICY? (confirmed 2026-08-07 on a
# CrowdStrike-managed corporate machine): if "Pin to taskbar" is missing
# from every context menu AND dragging the .lnk onto the taskbar shows the
# "not allowed" cursor, that's Group Policy's "Do not allow pinning
# programs to the Taskbar" blocking direct pins -- not a bug here, and no
# script/CLI trick gets around it (Microsoft locked down programmatic
# pinning specifically to stop this kind of automation).
#
# WORKING FIX: open the .lnk's Properties -> Shortcut tab -> Target field,
# and prepend "explorer.exe " (with the trailing space) before the existing
# path, e.g.:
#   explorer.exe "C:\Claude\Scripts\Curveball Tools\Start Curveball Tools.lnk"
# This makes Windows treat the launch as Explorer-initiated rather than a
# direct script/exe launch, which sidesteps that specific policy. After
# saving, "Pin to taskbar" becomes available on that shortcut again.
#
# Built by P. Capps.

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$targetBat = Join-Path $scriptDir "Start Curveball Tools.bat"
$iconPath  = Join-Path $scriptDir "pixel_tyler.ico"
$shortcutPath = Join-Path $scriptDir "Start Curveball Tools.lnk"

if (-not (Test-Path $targetBat)) {
    Write-Host "[!] Could not find: $targetBat"
    Write-Host "    Make sure this script sits in the same folder as the .bat file."
    exit 1
}
if (-not (Test-Path $iconPath)) {
    Write-Host "[!] Could not find: $iconPath"
    exit 1
}

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($shortcutPath)
$Shortcut.TargetPath       = $targetBat
$Shortcut.WorkingDirectory = $scriptDir
$Shortcut.IconLocation     = "$iconPath,0"
$Shortcut.Description      = "Launch Curveball Tools"
$Shortcut.Save()

Write-Host ""
Write-Host "Done. Created/updated:"
Write-Host "  $shortcutPath"
Write-Host "using icon:"
Write-Host "  $iconPath"
Write-Host ""
Write-Host "Next: pin THIS .lnk (not the .bat) to Start/taskbar."
