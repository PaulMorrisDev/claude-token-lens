<#
.SYNOPSIS
    Removes the Scheduled Task Register-TokenLensTask.ps1 created, and
    stops any currently-running claude-token-lens serve process
    (deliverable 2.c's counterpart).

.DESCRIPTION
    Two independent steps, each best-effort and reported separately:
    1. Unregister the Scheduled Task by name (no error if it doesn't
       exist -- this script is safe to run even if registration never
       happened, or already happened via the schtasks fallback).
    2. Find and stop any process whose command line invokes
       `claude_token_lens` (via `pythonw`/`python -m claude_token_lens
       serve`) using Get-CimInstance Win32_Process -- a Scheduled Task
       runs its action as an ordinary child process with no special
       marker of its own, so matching on the command line (rather than
       tracking a PID file this project doesn't otherwise keep) is the
       only way to find it whether it was started by the task, by hand,
       or left over from a previous session.

    Written for Windows PowerShell 5.1 compatibility: no `&&`, no
    ternary operator, no null-conditional operators.

.PARAMETER TaskName
    Name of the Scheduled Task to remove. Default: ClaudeTokenLens
    (must match Register-TokenLensTask.ps1's own default/parameter).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File Unregister-TokenLensTask.ps1
#>

[CmdletBinding()]
param(
    [string]$TaskName = "ClaudeTokenLens"
)

$ErrorActionPreference = "Stop"

Write-Host "claude-token-lens: unregistering Scheduled Task '$TaskName'"

$taskRemoved = $false
try {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        $taskRemoved = $true
        Write-Host "  Scheduled Task removed."
    }
    else {
        Write-Host "  No Scheduled Task named '$TaskName' found (Get-ScheduledTask)."
    }
}
catch {
    Write-Warning "  Get-ScheduledTask/Unregister-ScheduledTask unavailable or failed ($($_.Exception.Message)); trying schtasks /delete."
    & schtasks.exe /Delete /TN $TaskName /F 2>$null
    if ($LASTEXITCODE -eq 0) {
        $taskRemoved = $true
        Write-Host "  Scheduled Task removed via schtasks."
    }
    else {
        Write-Host "  schtasks /delete found nothing to remove (or failed) for '$TaskName'."
    }
}

Write-Host "claude-token-lens: looking for a running serve process"

$stopped = 0
try {
    $processes = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction Stop |
        Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains("claude_token_lens") }

    foreach ($proc in $processes) {
        Write-Host "  Stopping PID $($proc.ProcessId): $($proc.CommandLine)"
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
            $stopped = $stopped + 1
        }
        catch {
            Write-Warning "  Failed to stop PID $($proc.ProcessId): $($_.Exception.Message)"
        }
    }
}
catch {
    Write-Warning "  Get-CimInstance Win32_Process failed ($($_.Exception.Message)); skipping process cleanup."
}

if ($stopped -eq 0) {
    Write-Host "  No running claude_token_lens process found."
}
else {
    Write-Host "  Stopped $stopped process(es)."
}

if (-not $taskRemoved -and $stopped -eq 0) {
    Write-Host "Nothing to do -- claude-token-lens was not registered or running."
}
else {
    Write-Host "Done."
}
