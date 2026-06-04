param(
    [int]$PhoenixHttpPort = 6006,
    [int]$PhoenixGrpcPort = 4347,
    [int]$BackendPort = 8010,
    [int]$FrontendPort = 8501,
    [switch]$SkipNeo4j,
    [switch]$SkipPhoenix,
    [switch]$SmokeTest
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

$VenvDir = Join-Path $ProjectRoot ".venv"
$LogDir = Join-Path $ProjectRoot ".logs"
$InfraDir = Join-Path $ProjectRoot "infra"
$BackendUrl = "http://localhost:$BackendPort"

$Python = Join-Path $VenvDir "Scripts\python.exe"
$Uvicorn = Join-Path $VenvDir "Scripts\uvicorn.exe"
$Streamlit = Join-Path $VenvDir "Scripts\streamlit.exe"
$Phoenix = Join-Path $VenvDir "Scripts\phoenix.exe"

$ChildProcesses = New-Object System.Collections.Generic.List[System.Diagnostics.Process]
$Neo4jStartedByUs = $false

function Write-Step {
    param([string]$Message)
    Write-Host "[run.ps1] $Message" -ForegroundColor Cyan
}

function Write-Warn {
    param([string]$Message)
    Write-Host "[run.ps1] $Message" -ForegroundColor Yellow
}

function Stop-Port {
    param([int]$Port)

    $pids = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique

    foreach ($pid in $pids) {
        if ($pid -and $pid -ne $PID) {
            Write-Warn "port :$Port held by pid $pid; stopping it"
            Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
        }
    }
}

function Wait-Url {
    param(
        [string]$Url,
        [int]$TimeoutSeconds = 30
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3 | Out-Null
            return $true
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }

    return $false
}

function Start-LoggedProcess {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$ArgumentList
    )

    $stdout = Join-Path $LogDir "$Name.out.log"
    $stderr = Join-Path $LogDir "$Name.err.log"

    $process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $ArgumentList `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -WindowStyle Hidden `
        -PassThru

    $ChildProcesses.Add($process)
    Write-Step "$Name pid=$($process.Id), logs: .logs\$Name.out.log / .logs\$Name.err.log"
    return $process
}

function Stop-Children {
    foreach ($process in $ChildProcesses) {
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }

    if ($Neo4jStartedByUs) {
        Write-Step "stopping Neo4j container"
        Push-Location $InfraDir
        try {
            docker compose down --remove-orphans | Out-Null
        }
        finally {
            Pop-Location
        }
    }
}

try {
    if (-not (Test-Path $Python)) {
        throw "venv missing at $VenvDir. Run: .\scripts\setup.ps1"
    }

    if (-not (Test-Path (Join-Path $ProjectRoot ".env"))) {
        throw ".env missing. Run: .\scripts\setup.ps1, then fill in Azure OpenAI values."
    }

    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

    Write-Step "freeing ports: $PhoenixHttpPort, $BackendPort, $FrontendPort"
    Stop-Port $PhoenixHttpPort
    Stop-Port $BackendPort
    Stop-Port $FrontendPort

    if (-not $SkipNeo4j) {
        if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
            Write-Warn "docker is not available; backend will start without KG retrieval"
        }
        else {
            $running = docker ps --filter "name=brd_agent_neo4j" --filter "status=running" --format "{{.Names}}"
            if ($running -contains "brd_agent_neo4j") {
                Write-Step "Neo4j container already running; reusing it"
            }
            else {
                Write-Step "starting Neo4j container"
                Push-Location $InfraDir
                try {
                    docker compose up -d neo4j | Out-Null
                    $Neo4jStartedByUs = $true
                }
                finally {
                    Pop-Location
                }
            }

            if (Wait-Url "http://localhost:7474" 45) {
                Write-Step "Neo4j ready"
            }
            else {
                Write-Warn "Neo4j did not become ready; backend will soft-fail the KG layer"
            }
        }
    }

    if (-not $SkipPhoenix) {
        Write-Step "starting Phoenix"
        Start-LoggedProcess "phoenix" $Phoenix @("serve", "--grpc-port", "$PhoenixGrpcPort") | Out-Null
        if (-not (Wait-Url "http://localhost:$PhoenixHttpPort/v1/projects" 45)) {
            throw "Phoenix failed to start. Check .logs\phoenix.err.log"
        }
        Write-Step "Phoenix ready"
    }

    Write-Step "starting FastAPI"
    Start-LoggedProcess "backend" $Uvicorn @("backend.main:app", "--port", "$BackendPort", "--log-level", "info") | Out-Null
    if (-not (Wait-Url "$BackendUrl/health" 45)) {
        throw "Backend failed to start. Check .logs\backend.err.log"
    }
    Write-Step "FastAPI ready"

    Write-Step "starting Streamlit"
    $env:BACKEND_URL = $BackendUrl
    Start-LoggedProcess "streamlit" $Streamlit @(
        "run", "frontend/streamlit_app.py",
        "--server.port", "$FrontendPort",
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false"
    ) | Out-Null
    if (-not (Wait-Url "http://localhost:$FrontendPort/_stcore/health" 45)) {
        throw "Streamlit failed to start. Check .logs\streamlit.err.log"
    }
    Write-Step "Streamlit ready"

    Write-Host ""
    Write-Host "BRD Agent is running"
    Write-Host "  Streamlit UI    : http://localhost:$FrontendPort"
    Write-Host "  FastAPI backend : http://localhost:$BackendPort"
    Write-Host "  Phoenix traces  : http://localhost:$PhoenixHttpPort"
    Write-Host "  Neo4j browser   : http://localhost:7474"
    Write-Host "  Logs            : .logs\*.log"
    Write-Host ""

    if ($SmokeTest) {
        Write-Step "smoke test complete; stopping services"
        return
    }

    Write-Host "Press Ctrl+C to stop."

    while ($true) {
        Start-Sleep -Seconds 1
        foreach ($process in $ChildProcesses) {
            if ($process.HasExited) {
                throw "Process $($process.Id) exited early. Check .logs."
            }
        }
    }
}
finally {
    Stop-Children
}
