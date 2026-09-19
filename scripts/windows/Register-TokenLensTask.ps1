<#
.SYNOPSIS
    Registers claude-token-lens's serve command as a logon-triggered
    Windows Scheduled Task (deliverable 2.c) -- the native, non-Docker
    hosting path this project's plan prioritises ahead of Docker on
    Windows: no admin rights, no container runtime, just a task that
    starts a windowless Python process when the user logs on.

.DESCRIPTION
    Runs entirely under the current user's own privileges (-RunLevel
    Limited -- explicitly NOT "Highest", since this service never
    needs elevation: it only reads the user's own ~/.claude/projects
    and reads/writes its own ~/.claude/token-lens directory). Uses
    `pythonw` rather than `python` so no console window appears at
    logon.

    Written for Windows PowerShell 5.1 compatibility: no `&&`, no
    ternary operator, no null-conditional operators -- see
    docs/deploy.md for the project's PowerShell-compatibility policy.

.PARAMETER TaskName
    Name of the Scheduled Task to create. Default: ClaudeTokenLens.

.PARAMETER PythonwPath
    Path to pythonw.exe. Default: resolved via `where.exe pythonw`, or
    prompts to install the package first if pythonw itself is not
    found (Python's own installer ships it alongside python.exe).

.PARAMETER BillingMode
    Optional -BillingMode {api,subscription} forwarded to `serve`.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File Register-TokenLensTask.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File Register-TokenLensTask.ps1 -BillingMode subscription

.NOTES
    If `Register-ScheduledTask` is unavailable (some locked-down
    corporate images restrict the ScheduledTasks module even for
    non-admin users), this script falls back to the older `schtasks
    /create` command-line tool automatically -- see the try/catch
    below. Run Unregister-TokenLensTask.ps1 to remove the task again.
#>

[CmdletBinding()]
param(
    [string]$TaskName = "ClaudeTokenLens",
    [string]$PythonwPath,
    [ValidateSet("api", "subscription")]
    [string]$BillingMode
)

$ErrorActionPreference = "Stop"

function Resolve-Pythonw {
    param([string]$Explicit)

    if ($Explicit) {
        if (Test-Path $Explicit) {
            return $Explicit
        }
        Write-Error "PythonwPath '$Explicit' does not exist."
        exit 1
    }

    $found = $null
    try {
        $found = (Get-Command pythonw.exe -ErrorAction Stop).Source
    }
    catch {
        $found = $null
    }

    if (-not $found) {
        Write-Error "pythonw.exe not found on PATH. Install Python (which ships pythonw.exe alongside python.exe) or pass -PythonwPath explicitly."
        exit 1
    }
    return $found
}

$pythonw = Resolve-Pythonw -Explicit $PythonwPath
$projectsRoot = Join-Path $env:USERPROFILE ".claude\projects"
$configDir = Join-Path $env:USERPROFILE ".claude\token-lens"

$argumentList = "-m claude_token_lens serve --projects-root `"$projectsRoot`" --config-dir `"$configDir`""
if ($BillingMode) {
    $argumentList = $argumentList + " --billing-mode $BillingMode"
}

Write-Host "claude-token-lens: registering Scheduled Task '$TaskName'"
Write-Host "  pythonw:       $pythonw"
Write-Host "  projects-root: $projectsRoot"
Write-Host "  config-dir:    $configDir"
Write-Host "  run level:     Limited (no admin rights requested or required)"

$registered = $false
try {
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument $argumentList
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    $registered = $true
}
catch {
    Write-Warning "Register-ScheduledTask unavailable or failed ($($_.Exception.Message)); falling back to schtasks /create."
}

if (-not $registered) {
    $trArg = "`"$pythonw`" $argumentList"
    # schtasks' own quoting rules require the whole command wrapped once
    # more for /TR -- RL LIMITED is schtasks' equivalent of -RunLevel
    # Limited above.
    $schtasksArgs = @(
        "/Create", "/TN", $TaskName, "/TR", $trArg, "/SC", "ONLOGON",
        "/RL", "LIMITED", "/F"
    )
    & schtasks.exe @schtasksArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Error "schtasks /create failed with exit code $LASTEXITCODE"
        exit $LASTEXITCODE
    }
    $registered = $true
}

Write-Host "Done. The task starts at your next logon, or run it now with:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
