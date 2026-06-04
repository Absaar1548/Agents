param(
    [switch]$SkipInstall,
    [switch]$SeedChroma,
    [switch]$SeedNeo4j
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

function Write-Step {
    param([string]$Message)
    Write-Host "[setup.ps1] $Message" -ForegroundColor Cyan
}

function Invoke-Checked {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $FilePath $($Arguments -join ' ')"
    }
}

function Get-Python311 {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return [pscustomobject]@{ FilePath = "py"; Arguments = @("-3.11") }
    }

    if (Get-Command python -ErrorAction SilentlyContinue) {
        $version = (& python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
        if ($version -eq "3.11") {
            return [pscustomobject]@{ FilePath = "python"; Arguments = @() }
        }
    }

    throw "Python 3.11 is required. Install it or ensure the Windows 'py -3.11' launcher works."
}

function Ensure-EnvFile {
    $envPath = Join-Path $ProjectRoot ".env"
    $examplePath = Join-Path $ProjectRoot ".env.example"

    if (Test-Path $envPath) {
        Write-Step ".env already exists; leaving it unchanged"
        return
    }

    Write-Step "creating .env from .env.example"
    $content = Get-Content $examplePath -Raw
    $content = $content -replace "NEO4J_PASSWORD=REPLACE_ME", "NEO4J_PASSWORD=brd_agent_neo4j_pw"
    Set-Content -Path $envPath -Value $content -NoNewline
}

Ensure-EnvFile

if (-not (Test-Path $VenvPython)) {
    $python = Get-Python311
    Write-Step "creating virtual environment with Python 3.11"
    Invoke-Checked $python.FilePath (@($python.Arguments) + @("-m", "venv", ".venv"))
}
else {
    Write-Step "virtual environment already exists"
}

if (-not $SkipInstall) {
    Write-Step "installing Python dependencies"
    Invoke-Checked $VenvPython @("-m", "pip", "install", "--upgrade", "pip")
    Invoke-Checked $VenvPython @("-m", "pip", "install", "-r", "requirements.txt")
}

if ($SeedChroma) {
    Write-Step "seeding Chroma"
    Invoke-Checked $VenvPython @("-m", "scripts.seed_chroma")
}

if ($SeedNeo4j) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Warning "Docker is not installed or not on PATH; skipping Neo4j seed. Install Docker Desktop or run .\scripts\run.ps1 -SkipNeo4j for Chroma-only local startup."
        return
    }

    Write-Step "starting Neo4j for seed"
    Push-Location (Join-Path $ProjectRoot "infra")
    try {
        Invoke-Checked "docker" @("compose", "up", "-d", "neo4j")
    }
    finally {
        Pop-Location
    }

    Write-Step "seeding Neo4j"
    Invoke-Checked $VenvPython @("-m", "scripts.seed_neo4j")
}

Write-Host ""
Write-Step "setup complete"
Write-Host "Next:"
Write-Host "  1. Put real Azure OpenAI values in .env"
Write-Host "  2. Run: .\scripts\run.ps1"
