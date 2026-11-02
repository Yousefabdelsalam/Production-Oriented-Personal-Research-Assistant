<#
.SYNOPSIS
  Operate the DEDICATED Hindsight deployment of Personal Research Assistant (docker-compose.hindsight.yml).
  It only ever touches the Compose project "pra-hindsight" (containers pra-hindsight, pra-hindsight-db). It never uses -v,
  never prunes, and never references another project's containers or volumes.

.EXAMPLE
  .\scripts\hindsight.ps1 status
  .\scripts\hindsight.ps1 up                  # needs HINDSIGHT_API_LLM_API_KEY in .env.hindsight
  .\scripts\hindsight.ps1 up -ChunkOnly       # TEMPORARY no-LLM mode (chunk storage only; see docs/hindsight-deployment.md)
  .\scripts\hindsight.ps1 backup
  .\scripts\hindsight.ps1 restore -File backups\hindsight\hindsight-20261021-153458.dump -Yes
#>
param(
  [Parameter(Mandatory = $true, Position = 0)]
  [ValidateSet('up', 'stop', 'start', 'restart', 'down', 'status', 'health', 'logs', 'backup', 'restore', 'smoke')]
  [string]$Command,
  [string]$File,                       # restore: dump file
  [string]$Database = 'hindsight',     # restore: target database ('hindsight' = the live one, requires -Yes)
  [switch]$ChunkOnly,                  # up: run with HINDSIGHT_API_LLM_PROVIDER=none (no LLM; reduced functionality)
  [switch]$Yes,                        # restore: confirm overwriting the live database
  [int]$Tail = 80
)
$ErrorActionPreference = 'Continue'   # docker writes progress/log lines to stderr; Windows PowerShell 5.1 would treat those as errors under 'Stop'. Failures are detected via exit codes / explicit throws.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not (Test-Path .env.hindsight)) { throw ".env.hindsight not found. Copy .env.hindsight.example to .env.hindsight and fill it in (see docs/hindsight-deployment.md)." }

function Dc { docker compose --env-file .env.hindsight -f docker-compose.hindsight.yml @args }   # explicit file + env file: the app's .env is never read
function EnvValue([string]$name) { $l = Get-Content .env.hindsight | Where-Object { $_ -match "^$name=" } | Select-Object -First 1; if ($l) { ($l -split '=', 2)[1].Trim() } else { '' } }
function Mask([string]$s) { $s -replace 'sk-[A-Za-z0-9_\-]{8,}', 'sk-<masked>' -replace '(postgres(?:ql)?://[^:/\s]+:)[^@\s]+@', '$1<masked>@' -replace '(?i)((?:api[_-]?key|password|token)["''\s:=]+)[^\s"'',}]+', '$1<masked>' }
function Port { $p = EnvValue 'PRA_HINDSIGHT_API_PORT'; if ($p) { $p } else { '8890' } }
function WaitHealthy {
  $t0 = Get-Date
  for ($i = 0; $i -lt 100; $i++) {
    $a = docker inspect pra-hindsight --format '{{.State.Status}}/{{.State.Health.Status}}' 2>$null
    $b = docker inspect pra-hindsight-db --format '{{.State.Status}}/{{.State.Health.Status}}' 2>$null
    if ($a -eq 'running/healthy' -and $b -eq 'running/healthy') { Write-Host ("both containers healthy after {0}s" -f [int]((Get-Date) - $t0).TotalSeconds); return }
    if ($a -match '^(exited|restarting|dead)') { Write-Warning "pra-hindsight is $a. Check: .\scripts\hindsight.ps1 logs (a missing/invalid LLM key stops it from starting)."; return }
    Start-Sleep 3
  }
  Write-Warning "not healthy yet: api=$a db=$b"
}

switch ($Command) {
  'up' {
    $key = EnvValue 'HINDSIGHT_API_LLM_API_KEY'
    if (-not $key -and -not $ChunkOnly) {
      Write-Host "HINDSIGHT_API_LLM_API_KEY is empty in .env.hindsight. Hindsight 0.10.2 refuses to start without an LLM key." -ForegroundColor Yellow
      Write-Host "  - Put a valid OpenAI key for THIS Hindsight server in .env.hindsight (and set HINDSIGHT_API_SKIP_LLM_VERIFICATION=false), then run 'up' again." -ForegroundColor Yellow
      Write-Host "  - Or deliberately run the temporary no-LLM mode: .\scripts\hindsight.ps1 up -ChunkOnly" -ForegroundColor Yellow
      exit 2
    }
    if ($ChunkOnly) {
      Write-Warning "CHUNK-ONLY MODE (no LLM): retain stores raw chunks, there is NO fact extraction, and reflect/consolidation are disabled."
      $env:HINDSIGHT_API_LLM_PROVIDER = 'none'
    }
    try { Dc up -d } finally { Remove-Item Env:HINDSIGHT_API_LLM_PROVIDER -ErrorAction SilentlyContinue }
    WaitHealthy
  }
  'stop'    { Dc stop }                                    # keeps containers and ALL data
  'start'   { Dc start; WaitHealthy }
  'restart' { Dc restart; WaitHealthy }
  'down'    { Dc down }                                    # removes containers + network; volumes (the data) are kept. Never adds -v.
  'status'  {
    Dc ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'
    try { $v = Invoke-RestMethod "http://127.0.0.1:$(Port)/version" -TimeoutSec 5; Write-Host "API: reachable, version $($v.api_version)" } catch { Write-Host "API: NOT reachable on 127.0.0.1:$(Port)" }
    $m = (docker inspect pra-hindsight --format '{{range .Config.Env}}{{println .}}{{end}}' 2>$null | Where-Object { $_ -like 'HINDSIGHT_API_LLM_PROVIDER=*' })
    if ($m) { Write-Host ("LLM provider in the running container: " + ($m -replace 'HINDSIGHT_API_LLM_PROVIDER=', '')) }
  }
  'health'  {
    $p = Port
    "API   : " + (Invoke-RestMethod "http://127.0.0.1:$p/health" -TimeoutSec 10 | ConvertTo-Json -Compress)
    "ready : HTTP " + (Invoke-WebRequest "http://127.0.0.1:$p/health/ready" -UseBasicParsing -TimeoutSec 10).StatusCode
    $u = EnvValue 'PRA_HINDSIGHT_UI_PORT'; if (-not $u) { $u = '9990' }
    "UI    : HTTP " + (Invoke-WebRequest "http://127.0.0.1:$u/" -UseBasicParsing -TimeoutSec 20).StatusCode + "  (http://127.0.0.1:$u)"
    "DB    : " + (docker exec pra-hindsight-db pg_isready -U hindsight -d hindsight)
  }
  'logs'    { foreach ($c in 'pra-hindsight', 'pra-hindsight-db') { "===== $c (last $Tail lines, secrets masked)"; docker logs --tail $Tail $c 2>&1 | ForEach-Object { Mask "$_" } } }
  'backup'  {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'; $name = "hindsight-$stamp.dump"
    New-Item -ItemType Directory -Force backups\hindsight | Out-Null
    docker exec pra-hindsight-db pg_dump -U hindsight -d hindsight -Fc -f "/tmp/$name"; if ($LASTEXITCODE) { throw "pg_dump failed" }
    docker cp "pra-hindsight-db:/tmp/$name" "backups\hindsight\$name"; if ($LASTEXITCODE) { throw "docker cp failed" }
    docker exec pra-hindsight-db rm -f "/tmp/$name"
    $f = Get-Item "backups\hindsight\$name"
    docker cp $f.FullName "pra-hindsight-db:/tmp/verify.dump" | Out-Null
    $tables = docker exec pra-hindsight-db sh -c "pg_restore --list /tmp/verify.dump | grep -c 'TABLE DATA'"; docker exec pra-hindsight-db rm -f /tmp/verify.dump
    if (-not $tables -or [int]$tables -lt 1) { throw "backup file was written but pg_restore --list could not read it; do not rely on it" }
    "backup written: $($f.FullName)  ($($f.Length) bytes, $tables tables with data, sha256 $((Get-FileHash $f.FullName -Algorithm SHA256).Hash.Substring(0,16))...)"
    "NOTE: .env.hindsight (DB password, LLM key) is NOT in the backup; keep it safely elsewhere."
  }
  'restore' {
    if (-not $File -or -not (Test-Path $File)) { throw "restore needs -File <existing .dump>" }
    $live = ($Database -eq 'hindsight')
    if ($live -and -not $Yes) { throw "This OVERWRITES the live 'hindsight' database with the contents of $File. Re-run with -Yes to confirm (a safety backup is taken first). To test a dump safely use -Database hindsight_restore_test." }
    if ($Database -notmatch '^[a-z][a-z0-9_]{0,40}$') { throw "invalid database name" }
    if ($live) { & $PSCommandPath backup; Write-Host "stopping pra-hindsight so nothing writes during the restore"; docker stop pra-hindsight | Out-Null }
    docker cp (Resolve-Path $File).Path "pra-hindsight-db:/tmp/restore.dump" | Out-Null
    if (-not $live) { docker exec pra-hindsight-db psql -U hindsight -d postgres -c "create database $Database;" }
    $flags = if ($live) { @('--clean', '--if-exists') } else { @() }
    docker exec pra-hindsight-db pg_restore -U hindsight -d $Database --no-owner @flags /tmp/restore.dump
    $rc = $LASTEXITCODE; docker exec pra-hindsight-db rm -f /tmp/restore.dump
    "pg_restore exit code: $rc  (non-zero can be harmless warnings when --clean drops objects that do not exist yet; verify the counts below)"
    "rows: banks|documents|memory_units|chunks = " + (docker exec pra-hindsight-db psql -U hindsight -d $Database -At -c "select (select count(*) from banks),(select count(*) from documents),(select count(*) from memory_units),(select count(*) from chunks);")
    if ($live) { docker start pra-hindsight | Out-Null; WaitHealthy } else { "Restored into scratch database '$Database'. Drop it when done: docker exec pra-hindsight-db psql -U hindsight -d postgres -c `"drop database $Database;`"" }
  }
  'smoke'   {
    $py = Join-Path $root '.venv\Scripts\python.exe'
    & $py scripts\hindsight_smoke.py "http://127.0.0.1:$(Port)"; exit $LASTEXITCODE
  }
}
