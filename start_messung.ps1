# Heralded Homodyne Mobile (Harp ADQ32 HHD) - Start am Mess-PC mit echter Hardware
# Start normalerweise per Doppelklick auf start_messung.bat. Ablauf:
#   1. Geraetepruefung (python -m hhd.server.check, ohne Messung) - Ergebnis anzeigen, logs\geraetepruefung.json
#   2. nur wenn alle Live-Geraete in Ordnung: Server mit config\server_live.toml, Zugangscode, Browser lokal
# Optionen:
#   -Online      zusaetzlich Cloudflare Quick Tunnel + QR-Code wie start_online (sonst nur lokal)
#   -Trotzdem    gemischter Betrieb: Geraete, die die Pruefung nicht bestehen, werden ausdruecklich als
#                SIMULIERT gestartet (--harp-mode/--digitizer-mode sim); mindestens ein Live-Geraet muss
#                in Ordnung sein. Ohne -Trotzdem: Abbruch. Es gibt keinen stillen Rueckfall auf Simulation.
#   -Port 8766   -Config config\server_live.toml   -NoBrowser   -NoWait (nicht auf Enter warten)
# Umgebungsvariablen (optional):
#   HHD_HH500LIB / HHD_ADQAPI   Pfad der Herstellerbibliotheken (sonst hh500_lib/adq_lib in der Konfiguration)
#   HHD_TOKEN                   fester Zugangscode statt eines zufaelligen
#   HHD_TUNNEL_PROTOCOL         z. B. "http2", falls QUIC (UDP) im Netz blockiert ist
#   HHD_PYTHON / HHD_CLOUDFLARED   abweichende Pfade (fuer Tests)
# Beenden: Enter in diesem Fenster oder stop_messung.bat (= stop_online.bat).
param([int]$Port = 8766, [string]$Config = 'config\server_live.toml', [switch]$Online, [switch]$Trotzdem,
      [switch]$NoBrowser, [switch]$NoWait)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location -LiteralPath $root
$onWindows = ($env:OS -eq 'Windows_NT')
$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$pidFile = Join-Path $logDir 'online.pids'
$srvPidFile = Join-Path $logDir 'server.pid'
$infoFile = Join-Path $logDir 'online_info.txt'
$chkJson = Join-Path $logDir 'geraetepruefung.json'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Say([string]$m) { Write-Host $m -ForegroundColor Cyan }
function Warn([string]$m) { Write-Host "WARNUNG: $m" -ForegroundColor Yellow }
function Stop-All {
    & (Join-Path $root 'stop_online.ps1') -Port $Port -Quiet
}
function Fail([string]$m) {
    Write-Host ""
    Write-Host "FEHLER: $m" -ForegroundColor Red
    Stop-All
    exit 1   # start_messung.bat haelt das Fenster dann mit 'pause' offen
}
function Read-Shared([string]$path) {
    # Logdatei lesen, waehrend ein anderer Prozess hineinschreibt
    if (-not (Test-Path -LiteralPath $path)) { return '' }
    try {
        $fs = [System.IO.File]::Open($path, 'Open', 'Read', 'ReadWrite')
        $sr = New-Object System.IO.StreamReader($fs)
        $t = $sr.ReadToEnd()
        $sr.Close()
        return $t
    } catch { return '' }
}
function Test-PortInUse([int]$p) {
    $c = New-Object System.Net.Sockets.TcpClient
    try { $c.Connect('127.0.0.1', $p); return $true } catch { return $false } finally { $c.Close() }
}
function Get-Tail([string]$path) {
    $t = Read-Shared $path
    if ($t.Length -gt 1500) { $t = $t.Substring($t.Length - 1500) }
    return $t
}

$ver = '?'
$vm = Select-String -Path (Join-Path $root 'hhd\__init__.py') -Pattern '__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
if ($vm) { $ver = $vm.Matches[0].Groups[1].Value }
Say "== Heralded Homodyne Mobile $ver - Messung mit Hardware =="

# --- 1. Python-Umgebung und Konfiguration ---------------------------------------------
$py = $env:HHD_PYTHON
if (-not $py) { $py = [System.IO.Path]::Combine($root, '.venv', 'Scripts', 'python.exe') }
if (-not (Test-Path -LiteralPath $py)) {
    Fail "Python-Umgebung nicht gefunden ($py). Bitte zuerst installieren.bat ausfuehren."
}
$cfgPath = $Config
if (-not [System.IO.Path]::IsPathRooted($cfgPath)) { $cfgPath = Join-Path $root $Config }
if (-not (Test-Path -LiteralPath $cfgPath)) { Fail "Konfiguration $Config nicht gefunden." }
# Laufwerk D: (data_dir/log_dir) vorhanden? Sonst legt der Server die Daten unter .\hhd_data bzw. .\logs ab.
$cfgText = Get-Content -LiteralPath $cfgPath -Raw
$drives = @([regex]::Matches($cfgText, '"([A-Za-z]):[/\\]') | ForEach-Object { $_.Groups[1].Value.ToUpper() } | Select-Object -Unique)
foreach ($d in $drives) {
    if (-not (Test-Path -LiteralPath "$($d):\")) {
        Warn ("Laufwerk $($d): nicht vorhanden. Messdaten gehen nach $root\hhd_data, Logs nach $root\logs. " +
              "Falls das nicht gewuenscht ist: Laufwerk anschliessen oder data_dir/log_dir in $Config anpassen.")
    }
}

# --- 2. Laeuft schon etwas? ----------------------------------------------------------
if (Test-Path -LiteralPath $pidFile) {
    $alive = @(Get-Content -LiteralPath $pidFile | Where-Object { $_ -match '^\d+$' } |
               Where-Object { Get-Process -Id ([int]$_) -ErrorAction SilentlyContinue })
    if ($alive.Count -gt 0) {
        Fail 'Heralded Homodyne Mobile laeuft bereits. Bitte zuerst stop_messung.bat (oder stop_online.bat) ausfuehren.'
    }
    Remove-Item -LiteralPath $pidFile -Force
}
if (Test-PortInUse $Port) {
    Fail "Port $Port ist bereits belegt (laeuft start_simulation.bat oder start_online.bat noch?). Programm beenden oder stop_online.bat ausfuehren."
}

# --- 3. Geraetepruefung (ohne Messung) -------------------------------------------------
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
Say 'Geraetepruefung (Treiber laden, Geraete oeffnen und abfragen - keine Messung) ...'
Write-Host ''
if (Test-Path -LiteralPath $chkJson) { Remove-Item -LiteralPath $chkJson -Force }
& $py -m hhd.server.check --config $cfgPath --json $chkJson
$rc = $LASTEXITCODE
Write-Host ''
$modeArgs = @()
if ($rc -eq 2) { Fail "Konfiguration $Config fehlerhaft (siehe Meldung oben)." }
if ($rc -eq 3) {
    Fail "In $Config ist kein Geraet als live eingestellt. Fuer reine Simulation start_online.bat bzw. start_simulation.bat verwenden."
}
if ($rc -ne 0) {
    if (-not $Trotzdem) {
        Fail ("Geraetepruefung nicht bestanden - Messung wird NICHT gestartet. Abhilfe siehe oben " +
              "(Zusammenfassung: logs\geraetepruefung.json). Nach Behebung start_messung.bat erneut starten. " +
              "Gemischter Betrieb mit einem simulierten Geraet nur ausdruecklich: start_messung.bat -Trotzdem")
    }
    if (-not (Test-Path -LiteralPath $chkJson)) { Fail 'Geraetepruefung lieferte keine Zusammenfassung (logs\geraetepruefung.json).' }
    $j = Get-Content -LiteralPath $chkJson -Raw -Encoding UTF8 | ConvertFrom-Json
    $bad = @($j.devices | Where-Object { $_.checked -and -not $_.ok })
    $good = @($j.devices | Where-Object { $_.checked -and $_.ok })
    if ($bad.Count -eq 0) { Fail "Geraetepruefung meldet Fehler (Code $rc), aber kein fehlerhaftes Geraet - bitte Ausgabe oben pruefen." }
    if ($good.Count -eq 0) {
        Fail ('-Trotzdem ist nur fuer gemischten Betrieb gedacht: mindestens ein Live-Geraet muss die Pruefung bestehen. ' +
              'Fuer reine Simulation start_online.bat bzw. start_simulation.bat verwenden.')
    }
    foreach ($d in $bad) {
        if ($d.kind -eq 'harp') { $modeArgs += @('--harp-mode', 'sim') } else { $modeArgs += @('--digitizer-mode', 'sim') }
        Warn "-Trotzdem: $($d.name) besteht die Pruefung nicht ($($d.error)) und wird SIMULIERT gestartet."
    }
    Warn ('GEMISCHTER BETRIEB: Daten des simulierten Geraets sind keine Messdaten. Die Web-App zeigt die Betriebsart ' +
          'je Geraet (LIVE/SIMULIERT); sie steht auch in session.json und den Datendateien.')
} else {
    Say 'Geraetepruefung bestanden.'
}

# --- 4. Zugangscode ------------------------------------------------------------------
$alphabet = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'   # ohne 0/O und 1/I/L
if ($env:HHD_TOKEN) {
    $code = $env:HHD_TOKEN
    Say 'Zugangscode aus der Umgebungsvariablen HHD_TOKEN uebernommen.'
} else {
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $bytes = New-Object byte[] 64
    $rng.GetBytes($bytes)
    $sb = New-Object System.Text.StringBuilder
    foreach ($b in $bytes) {
        if ($b -lt 248) { [void]$sb.Append($alphabet[$b % 31]) }   # 248 = 8*31 -> gleichverteilt
        if ($sb.Length -ge 10) { break }
    }
    $code = $sb.ToString()
}

# --- 5. Server starten (nur 127.0.0.1, Live-Konfiguration) ----------------------------
$env:HHD_TOKEN = $code
$srvLog = Join-Path $logDir 'server-messung.log'
if (Test-Path -LiteralPath $srvPidFile) { Remove-Item -LiteralPath $srvPidFile -Force }
$spArgs = @{
    FilePath               = $py
    ArgumentList           = @('-m', 'hhd.server', '--config', "`"$cfgPath`"", '--host', '127.0.0.1', '--port', "$Port",
                               '--token', $code) + $modeArgs
    WorkingDirectory       = $root
    PassThru               = $true
    RedirectStandardOutput = $srvLog
    RedirectStandardError  = "$srvLog.err"
}
if ($onWindows) { $spArgs['WindowStyle'] = 'Hidden' }
Say "Starte Server auf http://127.0.0.1:$Port ($Config) ..."
$server = Start-Process @spArgs
Set-Content -LiteralPath $pidFile -Value $server.Id -Encoding Ascii

$ok = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 500
    if ($server.HasExited) { break }
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$Port/api/health"
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
}
if (-not $ok) { Fail ("Server startet nicht. Letzte Meldungen (logs\server-messung.log.err):`n" + (Get-Tail "$srvLog.err")) }
Say 'Server laeuft (Anmeldung mit Zugangscode aktiv).'
$srvOut = Read-Shared $srvLog
foreach ($line in ($srvOut -split "`r?`n")) { if ($line -match '^(Hinweis|HHD-Mess-Server)') { Say $line } }
$localUrl = "http://127.0.0.1:$Port"

if (-not $Online) {
    Set-Content -LiteralPath $infoFile -Value "URL: $localUrl`r`nCode: $code" -Encoding Ascii
    & $py ([System.IO.Path]::Combine($root, 'tools', 'print_online_info.py')) $localUrl $code 'lokal'
} else {
    # --- 6. cloudflared besorgen (wie start_online) ------------------------------------
    $cf = $env:HHD_CLOUDFLARED
    if (-not $cf) {
        $cmd = Get-Command cloudflared -ErrorAction SilentlyContinue
        if ($cmd) { $cf = $cmd.Source }
    }
    if (-not $cf) {
        $cf = [System.IO.Path]::Combine($root, 'tools', 'cloudflared.exe')
        if (-not (Test-Path -LiteralPath $cf)) {
            $url = 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe'
            Say "cloudflared nicht gefunden - lade offizielles Release von GitHub:`n  $url"
            try {
                [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
                $ProgressPreference = 'SilentlyContinue'
                $tmp = "$cf.download"
                Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $tmp
                Move-Item -LiteralPath $tmp -Destination $cf -Force
            } catch {
                Fail ("Download von cloudflared fehlgeschlagen: $($_.Exception.Message)`n" +
                      "Alternative: in einer Eingabeaufforderung  winget install --id Cloudflare.cloudflared  ausfuehren " +
                      "und start_messung.bat -Online erneut starten (oder ohne -Online nur lokal).")
            }
        }
    }
    Say "cloudflared: $cf"

    # --- 7. Tunnel starten -----------------------------------------------------------
    $cfLog = Join-Path $logDir 'cloudflared.log'
    if (Test-Path -LiteralPath $cfLog) { Remove-Item -LiteralPath $cfLog -Force }
    $cfArgs = @('tunnel', '--no-autoupdate', '--logfile', "`"$cfLog`"")
    if ($env:HHD_TUNNEL_PROTOCOL) { $cfArgs += @('--protocol', $env:HHD_TUNNEL_PROTOCOL) }
    $cfArgs += @('--url', $localUrl)
    $tpArgs = @{
        FilePath         = $cf
        ArgumentList     = $cfArgs
        WorkingDirectory = $root
        PassThru         = $true
    }
    if ($onWindows) { $tpArgs['WindowStyle'] = 'Hidden' }
    Say 'Starte Cloudflare Quick Tunnel (kann 10-30 Sekunden dauern) ...'
    try { $tunnel = Start-Process @tpArgs } catch { Fail "cloudflared konnte nicht gestartet werden: $($_.Exception.Message)" }
    Add-Content -LiteralPath $pidFile -Value $tunnel.Id -Encoding Ascii

    $publicUrl = $null
    for ($i = 0; $i -lt 120 -and -not $publicUrl; $i++) {
        Start-Sleep -Milliseconds 500
        $t = Read-Shared $cfLog
        $m = [regex]::Matches($t, 'https://[a-z0-9-]+\.trycloudflare\.com')
        foreach ($x in $m) { if ($x.Value -ne 'https://api.trycloudflare.com') { $publicUrl = $x.Value; break } }
        if ($tunnel.HasExited) { break }
    }
    if (-not $publicUrl) {
        Fail ("Keine trycloudflare-Adresse erhalten. Letzte cloudflared-Meldungen:`n" + (Get-Tail $cfLog))
    }
    $registered = $false
    for ($i = 0; $i -lt 60; $i++) {
        if ((Read-Shared $cfLog) -match 'Registered tunnel connection') { $registered = $true; break }
        if ($tunnel.HasExited) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $registered) {
        Warn ('Der Tunnel meldet noch keine Verbindung zu Cloudflare. Moeglicherweise blockiert das Netzwerk ' +
              'ausgehende Verbindungen (Port 7844, UDP/TCP). Versuch: set HHD_TUNNEL_PROTOCOL=http2 und neu starten. ' +
              'Details: logs\cloudflared.log')
    }
    Set-Content -LiteralPath $infoFile -Value "URL: $publicUrl`r`nCode: $code" -Encoding Ascii
    & $py ([System.IO.Path]::Combine($root, 'tools', 'print_online_info.py')) $publicUrl $code
    Say 'Hinweis: Die neue Adresse ist oft erst nach 10-60 Sekunden weltweit erreichbar.'
}

# --- 8. Browser am Mess-PC oeffnen -----------------------------------------------------
if ($onWindows -and -not $NoBrowser) {
    try { Start-Process "$($localUrl)/?token=$code" } catch { Warn "Browser konnte nicht geoeffnet werden: $localUrl (Code $code)" }
}

if ($NoWait) { exit 0 }
Write-Host ''
[void](Read-Host 'Zum BEENDEN (Server und ggf. Tunnel) Enter druecken')
Stop-All
Say 'Beendet.'
