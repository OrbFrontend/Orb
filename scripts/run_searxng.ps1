# Run the local SearXNG the assistant's web search uses, setting it up first if it is not yet, and point Orb at it.
#
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/OrbFrontend/Orb/main/scripts/run_searxng.ps1 | iex"
#
# Needs Docker Desktop. The first run writes a config and creates the container; later runs only start it.
# ORB_SEARXNG_UPDATE=1 pulls the latest SearXNG and recreates the container, keeping the config.
# ORB_SEARXNG_PORT (default 8888) applies when the container is created; ORB_URL defaults to http://127.0.0.1:8899.
# Continue, not Stop: Windows PowerShell turns a native command's redirected stderr into errors. Failures are checked below.
$ErrorActionPreference = "Continue"

$Port = if ($env:ORB_SEARXNG_PORT) { $env:ORB_SEARXNG_PORT } else { "8888" }
$OrbUrl = if ($env:ORB_URL) { $env:ORB_URL } else { "http://127.0.0.1:8899" }
$Name = "orb-searxng"
$Image = "docker.io/searxng/searxng:latest"
$ConfigDir = Join-Path $env:LOCALAPPDATA "orb-searxng"
$SearxngUrl = "http://127.0.0.1:$Port"

function Say($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "`nError: $text" -ForegroundColor Red; exit 1 }
# Whether Docker answers within 10 seconds: a half-started Docker Desktop can leave `docker info` waiting forever.
function DockerUp {
    $log = [System.IO.Path]::GetTempFileName()
    try {
        $p = Start-Process docker -ArgumentList "info" -NoNewWindow -PassThru -RedirectStandardOutput $log -RedirectStandardError "$log.err"
        $null = $p.Handle  # keeps the exit code readable after the process ends
        if (-not $p.WaitForExit(10000)) { $p.Kill(); return $false }
        return $p.ExitCode -eq 0
    } catch {
        return $false
    } finally {
        Remove-Item $log, "$log.err" -ErrorAction SilentlyContinue
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "Docker is not installed. Install Docker Desktop (winget install -e --id Docker.DockerDesktop), start it once, then run this again."
}

if (-not (DockerUp)) {
    $desktop = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (Test-Path $desktop) {
        Say "Starting Docker Desktop..."
        Start-Process $desktop
        $deadline = (Get-Date).AddSeconds(150)
        while (-not (DockerUp) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }
    }
    if (-not (DockerUp)) { Fail "Docker is installed but not answering. Start (or restart) Docker Desktop, then run this again." }
}

$Settings = Join-Path $ConfigDir "settings.yml"

function Exists { docker container inspect $Name *> $null; return $LASTEXITCODE -eq 0 }

# The config, the image and a new container. A config from an earlier setup is kept.
function SetUp {
    New-Item -ItemType Directory -Force -Path $ConfigDir -ErrorAction Stop | Out-Null
    # Docker makes an empty directory where a mounted file was missing; it holds nothing to keep.
    if (Test-Path $Settings -PathType Container) { Remove-Item $Settings -ErrorAction Stop }
    if (-not (Test-Path $Settings -PathType Leaf)) {
        Say "Writing $Settings"
        $bytes = New-Object byte[] 32
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $secret = -join ($bytes | ForEach-Object { $_.ToString("x2") })
        $yaml = @"
# SearXNG for Orb. Docs: https://docs.searxng.org/admin/settings/
use_default_settings: true
server:
  secret_key: "$secret"
  # Only this machine can reach it, so rate limiting is not needed.
  limiter: false
  image_proxy: false
search:
  # Orb reads results as JSON, which SearXNG leaves off by default.
  formats: [html, json]
"@
        # UTF-8 without a byte-order mark, which YAML readers can trip on.
        [System.IO.File]::WriteAllText($Settings, $yaml, (New-Object System.Text.UTF8Encoding $false))
    }
    Say "Pulling SearXNG..."
    docker pull $Image
    if ($LASTEXITCODE -ne 0) { Fail "Could not pull $Image." }
    docker rm -f $Name *> $null
    Say "Creating $Name on $SearxngUrl"
    docker run -d --name $Name --restart unless-stopped -p "127.0.0.1:${Port}:8080" -v "${Settings}:/etc/searxng/settings.yml:ro" $Image | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "Could not create the container." }
}

if ($env:ORB_SEARXNG_UPDATE -or -not (Exists)) {
    SetUp
} elseif (-not (Test-Path $Settings -PathType Leaf)) {
    Say "Its config is missing; setting it up again."
    SetUp
} else {
    # Where the existing container listens, whatever ORB_SEARXNG_PORT says now.
    $bound = docker port $Name 8080/tcp 2> $null | Select-Object -First 1
    if ($bound) { $SearxngUrl = "http://" + ($bound -replace "^0\.0\.0\.0", "127.0.0.1") }
    if ((docker container inspect -f "{{.State.Running}}" $Name) -eq "true") {
        Say "$Name is already running on $SearxngUrl"
    } else {
        Say "Starting $Name on $SearxngUrl"
        docker start $Name *> $null
        # A container that cannot start is set up again.
        if ($LASTEXITCODE -ne 0) { Say "It would not start; setting it up again."; SetUp }
    }
}

Write-Host -NoNewline "Waiting for it to answer"
$ok = $false
for ($i = 0; $i -lt 45; $i++) {
    try {
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop "$SearxngUrl/search?q=orb&format=json" | Out-Null
        $ok = $true
        break
    } catch { Write-Host -NoNewline "."; Start-Sleep -Seconds 2 }
}
Write-Host ""
if (-not $ok) { Fail "SearXNG did not answer. See: docker logs $Name" }

try {
    $body = @{ searxng_url = $SearxngUrl } | ConvertTo-Json -Compress
    Invoke-RestMethod -Method Put -Uri "$OrbUrl/api/settings" -ContentType "application/json" -Body $body -TimeoutSec 5 -ErrorAction Stop | Out-Null
    Say "Ready. Orb searches through $SearxngUrl. Reload Orb's page if it is open."
} catch {
    Say "Ready. Orb is not running, so set it yourself: Assistant -> Options -> SearXNG URL: $SearxngUrl"
}
Write-Host "It starts with Docker from now on. To remove it: docker rm -f $Name"
