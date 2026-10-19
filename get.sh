#!/usr/bin/env bash
# Orrery one-command installer.
#
#   Server (Ubuntu 22.04+ or Debian 12+, as root):
#     curl -fsSL https://raw.githubusercontent.com/sherinklbm/orrery/main/get.sh | sudo bash
#   Any computer with Docker (Linux, macOS, Windows with WSL 2), for trying it out on localhost:
#     curl -fsSL https://raw.githubusercontent.com/sherinklbm/orrery/main/get.sh | bash -s -- --local
#
# Server mode installs everything (HTTPS, firewall, SSH safety net, n8n, the AI gateway, the panel, Hermes
# preparation) and asks a few questions. Local mode runs only the panel, n8n and the AI gateway on localhost.
set -euo pipefail
REPO="${ORRERY_REPO:-https://github.com/sherinklbm/orrery.git}"
BRANCH="${ORRERY_BRANCH:-main}"
MODE="server"
for arg in "$@"; do
  case "$arg" in
    --local) MODE="local" ;;
    --server) MODE="server" ;;
    -h|--help) sed -n '2,11p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "Unknown option: $arg (use --local or --server)" >&2; exit 1 ;;
  esac
done
die() { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

if [ "$MODE" = "server" ]; then
  [ "$(uname -s)" = "Linux" ] || die "Server mode needs a Linux server. On this computer use:  ... | bash -s -- --local"
  [ "$(id -u)" -eq 0 ] || die "Run as root:  curl -fsSL .../get.sh | sudo bash"
  . /etc/os-release
  case " ${ID:-} ${ID_LIKE:-} " in *" ubuntu "*|*" debian "*) ;; *) die "Server mode supports Ubuntu and Debian (found ${PRETTY_NAME:-unknown}). Use --local on other systems." ;; esac
  command -v git >/dev/null 2>&1 || { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git ca-certificates >/dev/null; }
  APP_DIR="${APP_DIR:-/opt/orrery}"
  if [ -d "$APP_DIR/.git" ]; then git -C "$APP_DIR" fetch -q origin "$BRANCH" && git -C "$APP_DIR" checkout -q -B "$BRANCH" FETCH_HEAD
  else git clone -q --branch "$BRANCH" "$REPO" "$APP_DIR"; fi
  exec env REPO="$REPO" BRANCH="$BRANCH" APP_DIR="$APP_DIR" bash "$APP_DIR/deploy/install.sh"
fi

# Local mode
command -v git >/dev/null 2>&1 || die "Install git first."
command -v docker >/dev/null 2>&1 || die "Install Docker first (Docker Desktop on macOS/Windows, or Docker Engine on Linux)."
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is needed (it comes with Docker Desktop)."
DIR="${ORRERY_DIR:-$HOME/orrery}"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull -q --ff-only || true; else git clone -q --branch "$BRANCH" "$REPO" "$DIR"; fi
exec bash "$DIR/deploy/local.sh"
