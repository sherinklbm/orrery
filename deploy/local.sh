#!/usr/bin/env bash
# Run Orrery on this computer (localhost only), for trying it out. Needs Docker with Compose v2.24 or newer.
#   bash deploy/local.sh          start (asks for a username and password the first time)
#   bash deploy/local.sh stop     stop
#   bash deploy/local.sh reset-2fa   turn off two-factor sign-in (lost phone)
set -euo pipefail
cd "$(dirname "$0")"
COMPOSE=(docker compose -p orrery-local -f docker-compose.yml -f docker-compose.local.yml --env-file .env.local)
rand() { if command -v openssl >/dev/null 2>&1; then openssl rand -hex "$1"; else head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; fi; }

case "${1:-start}" in
  stop) "${COMPOSE[@]}" down; exit 0 ;;
  reset-2fa) "${COMPOSE[@]}" exec console python server.py reset-2fa; exit 0 ;;
  start) ;;
  *) echo "Usage: bash deploy/local.sh [start|stop|reset-2fa]" >&2; exit 1 ;;
esac

if [ ! -f .env.local ]; then
  read -rp "Choose a username for the panel: " user </dev/tty
  read -rsp "Choose a password (12+ characters): " pw </dev/tty; echo
  [ "${#pw}" -ge 12 ] || { echo "The password must be at least 12 characters." >&2; exit 1; }
  case "$user" in ""|*[!A-Za-z0-9._-]*) echo "Use letters, numbers, dot, dash or underscore for the username." >&2; exit 1 ;; esac
  umask 077
  cat > .env.local <<ENV
DOMAIN=localhost
N8N_DOMAIN=localhost
ACME_EMAIL=nobody@localhost
TZ=UTC
CONSOLE_USER=$user
CONSOLE_PASSWORD_HASH='pending'
CONSOLE_SECRET=$(rand 32)
N8N_ENCRYPTION_KEY=$(rand 32)
N8N_VERSION=latest
AI_RUNTIME_TOKEN=$(rand 24)
LLM_GATE_TOKEN_N8N=$(rand 24)
LLM_GATE_TOKEN_HERMES=$(rand 24)
ENV
  "${COMPOSE[@]}" build console >/dev/null
  hash="$(printf '%s' "$pw" | "${COMPOSE[@]}" run --rm -T --no-deps console python -c "import sys, auth; print(auth.hash_password(sys.stdin.read()))" | tail -n1)"
  unset pw
  case "$hash" in scrypt:*) ;; *) echo "Could not create the password hash." >&2; exit 1 ;; esac
  sed -i.bak "s|^CONSOLE_PASSWORD_HASH=.*|CONSOLE_PASSWORD_HASH='$hash'|" .env.local && rm -f .env.local.bak
fi

"${COMPOSE[@]}" up -d --build console llm-gate n8n
for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:8080/healthz >/dev/null 2>&1 && break; sleep 2; done
cat <<MSG

Orrery is running on this computer:
  Panel:  http://localhost:8080   (first sign-in sets up two-factor; have an authenticator app ready)
  n8n:    http://localhost:5678
  Gateway for a local Hermes: http://127.0.0.1:8081/v1  (token: LLM_GATE_TOKEN_HERMES in deploy/.env.local)
Stop with:  bash deploy/local.sh stop
MSG
