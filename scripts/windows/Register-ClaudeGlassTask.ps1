<#
.SYNOPSIS
    Registers claudeglass's serve command as a logon-triggered
    Windows Scheduled Task (deliverable 2.c) -- the native, non-Docker
    hosting path this project's plan prioritises ahead of Docker on
    Windows: no admin rights, no container runtime, just a task that
    starts a windowless Python process when the user logs on.

.DESCRIPTION
    Runs entirely under the current user's own privileges (-RunLevel
    Limited -- explicitly NOT "Highest", since this service never
    needs elevation: it only reads the user's own ~/.claude/projects
    and reads/writes its own ~/.claude/claudeglass directory). Uses
    `pythonw` rather than `python` so no console window appears at
    logon.

    Written for Windows PowerShell 5.1 compatibility: no `&&`, no
    ternary operator, no null-conditional operators -- see
    docs/deploy.md for the project's PowerShell-compatibility policy.

.PARAMETER TaskName
    Name of the Scheduled Task to create. Default: ClaudeGlass.

.PARAMETER PythonwPath
    Path to pythonw.exe. Default: resolved via `Get-Command pythonw.exe`
    (nit 28: an earlier draft of this help text said `where.exe`, which
    the code never actually called), or reports an error asking you to
    install Python (which ships pythonw.exe alongside python.exe) or
    pass -PythonwPath explicitly if pythonw itself is not found.

.PARAMETER BillingMode
    Optional -BillingMode {api,subscription} forwarded to `serve`.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File Register-ClaudeGlassTask.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File Register-ClaudeGlassTask.ps1 -BillingMode subscription

.NOTES
    If `Register-ScheduledTask` is unavailable (some locked-down
    corporate images restrict the ScheduledTasks module even for
    non-admin users), this script falls back to the older `schtasks
    /create` command-line tool automatically -- see the try/catch
    below. Run Unregister-ClaudeGlassTask.ps1 to remove the task again.
#>

[CmdletBinding()]
param(
    [string]$TaskName = "ClaudeGlass",
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
        # Nit 28: this script sets $ErrorActionPreference = "Stop" at its
        # top level, which this function inherits (it never sets its own
        # local override) -- Write-Error is therefore already a
        # terminating call here, so an `exit 1` on the next line would
        # never run. No such line follows, on purpose.
        Write-Error "PythonwPath '$Explicit' does not exist."
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
    }
    return $found
}

$pythonw = Resolve-Pythonw -Explicit $PythonwPath
$projectsRoot = Join-Path $env:USERPROFILE ".claude\projects"
$configDir = Join-Path $env:USERPROFILE ".claude\claudeglass"

# --exit-on-code-change: after an update lands without a restart, serve
# exits and starts this task again on the new code (only when the task is
# named ClaudeGlass; under another -TaskName it just reports it).
$argumentList = "-m claudeglass serve --projects-root `"$projectsRoot`" --config-dir `"$configDir`" --exit-on-code-change"
if ($BillingMode) {
    $argumentList = $argumentList + " --billing-mode $BillingMode"
}

Write-Host "claudeglass: registering Scheduled Task '$TaskName'"
Write-Host "  pythonw:       $pythonw"
Write-Host "  projects-root: $projectsRoot"
Write-Host "  config-dir:    $configDir"
Write-Host "  run level:     Limited (no admin rights requested or required)"

$registered = $false
try {
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument $argumentList
    # Fix: an -AtLogOn trigger with no -User fires on *any* user's
    # logon, which Task Scheduler treats as a machine-wide trigger and
    # refuses to register without admin rights -- even though the
    # -Principal below already scopes who the task actually runs as.
    # Passing the same "DOMAIN\user" identity to the trigger itself
    # scopes the trigger to this one account's logons, which a Limited
    # (non-admin) principal is allowed to register.
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    # Review finding 14: a bare $env:USERNAME is ambiguous as a
    # -UserId on a domain-joined machine (Task Scheduler needs to
    # resolve it to one SID, and an unqualified name can match a
    # different account than the one actually running this script, or
    # fail to resolve at all). $env:USERDOMAIN is the domain name when
    # domain-joined and the local computer name otherwise, so
    # "$env:USERDOMAIN\$env:USERNAME" always names this exact account
    # unambiguously, the same "DOMAIN\user" form Task Scheduler's own UI
    # displays a principal as.
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    # -ExecutionTimeLimit ([TimeSpan]::Zero) means "no time limit" --
    # without it Task Scheduler's own default (72 hours) kills this
    # long-running serve process after three days.
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    $registered = $true
}
catch {
    Write-Warning "Register-ScheduledTask unavailable or failed ($($_.Exception.Message)); falling back to schtasks /create."
}

if (-not $registered) {
    # Review finding 13: schtasks.exe re-parses its own /TR value with a
    # second, internal split (into "the executable" and "the
    # executable's own arguments") on top of the normal OS-level argv
    # rules PowerShell already applies when it turns this array into the
    # child process's command line. Handing it a /TR value with plain
    # embedded quotes (`"$pythonw`" ...) collides with PowerShell's own
    # auto-quoting of that value (needed because it contains spaces,
    # e.g. "C:\Users\Jane Doe\...") -- confirmed by spawning a real argv
    # probe: the plain form gets the whole value split apart wherever a
    # space falls *outside* what should have been a still-quoted
    # segment, silently truncating both the pythonw path and every
    # `--projects-root`/`--config-dir` value that itself contains a
    # space. Escaping every embedded quote as `\"` (backslash + quote,
    # not just PowerShell's own backtick-quote) makes the single /TR
    # argument round-trip intact through both parsing layers.
    $trArg = ("`"$pythonw`" $argumentList") -replace '"', '\"'
    # /RU + /IT mirror the -User on the Register-ScheduledTask trigger
    # above: /RU scopes the ONLOGON trigger to this one account (a
    # bare ONLOGON with no /RU fires on any user's logon, which
    # schtasks likewise refuses to create without admin rights), and
    # /IT ("interactive token") is required whenever /RU names the
    # currently-running user without also supplying /RP a password.
    $schtasksArgs = @(
        "/Create", "/TN", $TaskName, "/TR", $trArg, "/SC", "ONLOGON",
        "/RL", "LIMITED", "/RU", "$env:USERDOMAIN\$env:USERNAME", "/IT", "/F"
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
