#!/usr/bin/env bash
# Temporärer Online-Zugang zum HHD-Mess-Server über einen Cloudflare-Quick-Tunnel (Muster: start_online.ps1 in GX).
# Voraussetzungen: cloudflared installiert; in config/server.toml ein Zugangscode (token) gesetzt.
# Der Server lauscht dann nur auf 127.0.0.1; Verbindungen über den Tunnel gelten als ENTFERNT (ED-8b:
# kritische Parameter nur mit Bestätigung, Triggerausgänge gesperrt).
set -euo pipefail
cd "$(dirname "$0")/.."
CFG=${1:-config/server.toml}
if ! grep -Eq '^token *= *"[^"]+"' "$CFG"; then
  echo "Abbruch: In $CFG ist kein Zugangscode (token) gesetzt – ohne Code kein Online-Betrieb." >&2
  exit 1
fi
PORT=$(grep -E '^port' "$CFG" | head -1 | sed -E 's/[^0-9]//g'); PORT=${PORT:-8766}
python -m hhd.server --config "$CFG" --host 127.0.0.1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT
sleep 2
echo "Tunnel startet – die angezeigte https://…trycloudflare.com-Adresse mit dem Zugangscode öffnen."
cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:${PORT}"
