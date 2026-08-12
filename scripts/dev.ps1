# Starts this service: the FastMCP server, calling the real upstream.
#
# That is all this repo owns. The UI lives in cm_mcp_agent and starts itself --
# nothing here knows or cares whether it is running.
#
# No simulator is started here. The offline mock is a test fixture under tests/,
# booted in-process by the suite and by nothing else, so a running engine always
# answers from the upstream its contracts name. Set DEV_OFFLINE=0 and a real
# SALLA_ACCESS_TOKEN in .env, exactly as the deployed service does.
#
#   pwsh scripts/dev.ps1          # start
#   pwsh scripts/dev.ps1 -Stop    # stop

param([switch]$Stop)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $repo '.cache/dev-pids.json'
$ports = @(8765)

function Stop-Stack {
    if (Test-Path $pidFile) {
        foreach ($entry in (Get-Content $pidFile -Raw | ConvertFrom-Json)) {
            # /T kills the tree: `uv run python -m ...` is a uv wrapper around a
            # python child, and stopping only the wrapper leaves the port held.
            $null = & taskkill.exe /PID $entry.ProcessId /T /F 2>&1
            Write-Host "stopped $($entry.Name) (pid $($entry.ProcessId))" -ForegroundColor DarkGray
        }
        Remove-Item $pidFile -Force
    }
    $stray = Get-NetTCPConnection -LocalPort $ports -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($processId in $stray) {
        $null = & taskkill.exe /PID $processId /T /F 2>&1
        Write-Host "stopped stray listener (pid $processId)" -ForegroundColor DarkGray
    }
    Write-Host 'Engine stopped.' -ForegroundColor Green
}

if ($Stop) { Stop-Stack; return }

Push-Location $repo
try {
    if (-not (Test-Path '.env')) { Copy-Item '.env.example' '.env' }
    New-Item -ItemType Directory -Force (Join-Path $repo '.cache') | Out-Null

    $occupied = Get-NetTCPConnection -LocalPort $ports -State Listen -ErrorAction SilentlyContinue
    if ($occupied) {
        # A stale listener makes every health probe pass against the wrong
        # process, which looks like success until the demo behaves strangely.
        $occupied | Select-Object LocalPort, OwningProcess -Unique | ForEach-Object {
            Write-Host "  port $($_.LocalPort) held by pid $($_.OwningProcess)" -ForegroundColor Red
        }
        throw 'Ports in use. Run "pwsh scripts/dev.ps1 -Stop" first.'
    }

    $started = @()

    function Start-Probed($name, $file, $arguments, $probe, $acceptAnyResponse = $false) {
        Write-Host "starting $name ..." -NoNewline
        $process = Start-Process -FilePath $file -ArgumentList $arguments `
            -WorkingDirectory $repo -PassThru -WindowStyle Hidden
        $script:started += [pscustomobject]@{ Name = $name; ProcessId = $process.Id }
        $script:started | ConvertTo-Json -AsArray | Set-Content $pidFile -Encoding utf8

        $deadline = (Get-Date).AddSeconds(45)
        while ((Get-Date) -lt $deadline) {
            if ($process.HasExited) {
                Write-Host " FAILED (exited $($process.ExitCode))" -ForegroundColor Red
                throw "$name exited during startup."
            }
            try {
                Invoke-WebRequest -Uri $probe -TimeoutSec 2 -UseBasicParsing | Out-Null
                Write-Host " ok (pid $($process.Id))" -ForegroundColor Green; return
            } catch {
                # FastMCP's /mcp rejects a bare GET; any HTTP reply proves it is up.
                if ($acceptAnyResponse -and $_.Exception.Response) {
                    Write-Host " ok (pid $($process.Id))" -ForegroundColor Green; return
                }
                Start-Sleep -Milliseconds 400
            }
        }
        Write-Host ' TIMEOUT' -ForegroundColor Red
        throw "$name did not become healthy at $probe."
    }

    Start-Probed 'FastMCP server :8765' 'uv' @('run', 'python', '-m', 'cm_engine.server') `
        'http://127.0.0.1:8765/mcp' $true

    Write-Host ''
    Write-Host 'Engine is up.' -ForegroundColor Green
    Write-Host '  MCP   http://127.0.0.1:8765/mcp'
    Write-Host ''
    Write-Host 'Stop with: pwsh scripts/dev.ps1 -Stop' -ForegroundColor DarkGray
}
finally { Pop-Location }
