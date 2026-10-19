#!/usr/bin/env bash
# Orrery installer for a fresh Ubuntu or Debian server (Orrery + n8n + AI gateway + HTTPS + safe SSH + Hermes preparation).
#
#   scp the repository ZIP to the server, unzip it to /opt/orrery, then as root:
#       bash /opt/orrery/deploy/install.sh
#   (see deploy/DEPLOY.md for the exact steps; a fresh-server snapshot first is strongly advised)
#
# Design rule: nothing here may remove your way into the server. SSH is never switched to key-only by this
# script. It adds only harmless settings, installs a guard that repairs SSH if it ever breaks, and leaves
# strict mode as an optional last step with an automatic 10-minute rollback.
#
# Optional non-interactive use: set DOMAIN, N8N_DOMAIN, ACME_EMAIL, CONSOLE_USER, CONSOLE_PASSWORD, TZ_NAME,
# SSH_PUBKEY, PREP_HERMES (1 or 0), INSTALL_TAILSCALE (1 or 0), INSTALL_FAIL2BAN (1 or 0).
set -euo pipefail

REPO="${REPO:-https://github.com/sherinklbm/orrery.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/orrery}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/orrery}"
TOTAL=10

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARNING: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
step() { say "$1/$TOTAL $2"; }

[ "$(id -u)" -eq 0 ] || die "Run as root."
. /etc/os-release
case " ${ID:-} ${ID_LIKE:-} " in
  *" ubuntu "*|*" debian "*) ;;
  *) die "This installer supports Ubuntu 22.04+ and Debian 12+ (found: ${PRETTY_NAME:-unknown}). For other systems use local mode: bash get.sh --local" ;;
esac
[ "$(df --output=avail -BG / | tail -n1 | tr -dc 0-9)" -ge 6 ] || die "Less than 6 GB of free disk space."
awk '/MemTotal/ {exit !($2 < 1800000)}' /proc/meminfo && warn "Under 2 GB of RAM: the stack may be tight."

# Read from the terminal even if the script is piped in.
ask() {  # ask VAR "Prompt" [default] [secret]
  local var="$1" prompt="$2" default="${3:-}" secret="${4:-}" value="${!1:-}"
  if [ -z "$value" ]; then
    [ -r /dev/tty ] || die "No terminal available. Set $var as an environment variable."
    if [ -n "$secret" ]; then read -rsp "$prompt: " value </dev/tty; echo
    else read -rp "$prompt${default:+ [$default]}: " value </dev/tty; fi
    value="${value:-$default}"
  fi
  [ -n "$value" ] || die "$var is required."
  printf -v "$var" '%s' "$value"
}
yes_no() {  # yes_no "Question" default(y|n) -> returns 0 for yes
  local ans="" def="${2:-n}"
  if [ -r /dev/tty ]; then read -rp "$1 [$([ "$def" = y ] && echo Y/n || echo y/N)] " ans </dev/tty || true; fi
  ans="${ans:-$def}"; [ "$ans" = y ] || [ "$ans" = Y ]
}

# ---------------------------------------------------------------------------------------------
step 1 "Packages and automatic security updates"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git openssl ufw unattended-upgrades cron dnsutils unzip >/dev/null
# Security updates install themselves every day (Ubuntu's standard mechanism, switched on explicitly).
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\nAPT::Periodic::AutocleanInterval "7";\n' > /etc/apt/apt.conf.d/20auto-upgrades
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
# fail2ban is optional (asked at the end). With key-only SSH it adds little, and a careless setup can lock out
# its own owner; strict SSH (optional, last) is the real protection.

# ---------------------------------------------------------------------------------------------
step 2 "Time zone"
ask TZ_NAME "Time zone (Region/City)" "UTC"
timedatectl list-timezones | grep -qx "$TZ_NAME" || die "Unknown time zone: $TZ_NAME"
timedatectl set-timezone "$TZ_NAME"
# An accurate clock matters: two-factor codes are time-based. Use the system's time sync, installing one if missing.
if ! systemctl list-unit-files 2>/dev/null | grep -qE '^(systemd-timesyncd|chrony|chronyd)\.service'; then
  apt-get install -y -qq systemd-timesyncd >/dev/null 2>&1 || warn "Could not install time sync; keep the clock right yourself."
fi
timedatectl set-ntp true 2>/dev/null || true

# ---------------------------------------------------------------------------------------------
step 3 "Limits so nothing can fill the disk or starve SSH"
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=200M\n' > /etc/systemd/journald.conf.d/orrery.conf
systemctl restart systemd-journald || true
if [ ! -f /etc/docker/daemon.json ]; then
  mkdir -p /etc/docker
  printf '{\n  "log-driver": "json-file",\n  "log-opts": { "max-size": "10m", "max-file": "3" }\n}\n' > /etc/docker/daemon.json
fi
# Kernel network hardening: ignore redirects and source routing, log martians, SYN cookies, hide kernel pointers.
cat > /etc/sysctl.d/60-orrery-hardening.conf <<'SYSCTL'
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv6.conf.default.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv6.conf.all.accept_source_route = 0
net.ipv4.conf.all.log_martians = 1
net.ipv4.icmp_echo_ignore_broadcasts = 1
net.ipv4.tcp_syncookies = 1
kernel.kptr_restrict = 2
kernel.dmesg_restrict = 1
fs.protected_hardlinks = 1
fs.protected_symlinks = 1
SYSCTL
sysctl -q --system >/dev/null 2>&1 || warn "Some kernel hardening settings could not be applied (fine inside some containers)."
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# ---------------------------------------------------------------------------------------------
step 4 "Firewall (SSH is allowed BEFORE it is switched on)"
SSH_PORTS="$( { sshd -T 2>/dev/null | awk '/^port /{print $2}'; echo "${SSH_CONNECTION:-}" | awk '{print $NF}'; echo 22; } | grep -E '^[0-9]+$' | sort -un)"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
for p in $SSH_PORTS; do ufw allow "$p/tcp" >/dev/null; done
ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw allow 443/udp >/dev/null
ufw --force enable >/dev/null
echo "    SSH port(s) allowed: $(echo $SSH_PORTS)"

# ---------------------------------------------------------------------------------------------
step 5 "Docker (official convenience script from get.docker.com)"
command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh >/dev/null
systemctl enable --now docker >/dev/null 2>&1 || true
docker compose version >/dev/null 2>&1 || die "Docker Compose plugin is missing."

# ---------------------------------------------------------------------------------------------
step 6 "The code"
FETCH_URL="$REPO"
[ -z "${GH_TOKEN:-}" ] || FETCH_URL="${REPO/https:\/\//https://x-access-token:${GH_TOKEN}@}"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch -q "$FETCH_URL" "$BRANCH" || die "Could not fetch updates."
  git -C "$APP_DIR" checkout -q -B "$BRANCH" FETCH_HEAD
elif [ -f "$APP_DIR/deploy/docker-compose.yml" ]; then
  echo "    using the code already in $APP_DIR (copied by hand, no git)"
else
  git clone -q --branch "$BRANCH" "$FETCH_URL" "$APP_DIR" || die "Could not clone. Copy the ZIP to the server and unzip it into $APP_DIR instead."
  git -C "$APP_DIR" remote set-url origin "$REPO"
fi
unset GH_TOKEN FETCH_URL
cd "$APP_DIR/deploy"

# ---------------------------------------------------------------------------------------------
if [ -f .env ]; then
  step 7 "Configuration: keeping your existing .env"
  set -a; . ./.env; set +a
else
  step 7 "Configuration"
  ask DOMAIN "Your domain (for example panel.yourdomain.com or yourdomain.com)"
  ask N8N_DOMAIN "n8n address" "n8n.$DOMAIN"
  ask ACME_EMAIL "Email for HTTPS certificate notices"
  ask CONSOLE_USER "Control panel username"
  ask CONSOLE_PASSWORD "Control panel password (12+ characters)" "" secret
  [ "${#CONSOLE_PASSWORD}" -ge 12 ] || die "Password must be at least 12 characters."
  case "$CONSOLE_USER$DOMAIN$N8N_DOMAIN$ACME_EMAIL" in *[\'\"\$\`\ \\]*) die "Values must not contain quotes, spaces, \$, backticks or backslashes." ;; esac

  server_ip="$(curl -fsS https://api.ipify.org || true)"
  for host in "$DOMAIN" "$N8N_DOMAIN"; do
    resolved="$(dig +short "$host" | tail -n1)"
    if [ -n "$server_ip" ] && [ "$resolved" != "$server_ip" ]; then
      warn "$host resolves to '${resolved:-nothing}', but this server is $server_ip. HTTPS fails until the A record points here."
    fi
  done

  umask 077
  cat > .env <<EOF
DOMAIN=$DOMAIN
N8N_DOMAIN=$N8N_DOMAIN
ACME_EMAIL=$ACME_EMAIL
TZ=$TZ_NAME
CONSOLE_USER=$CONSOLE_USER
CONSOLE_PASSWORD_HASH='pending'
CONSOLE_SECRET=$(openssl rand -hex 32)
N8N_ENCRYPTION_KEY=$(openssl rand -hex 32)
N8N_VERSION=latest
AI_RUNTIME_TOKEN=$(openssl rand -hex 24)
LLM_GATE_TOKEN_N8N=$(openssl rand -hex 24)
LLM_GATE_TOKEN_HERMES=$(openssl rand -hex 24)
EOF
  chmod 600 .env
fi

# ---------------------------------------------------------------------------------------------
step 8 "Build, start, and nightly backups"
bash "$APP_DIR/deploy/enable-bridge.sh" || warn "Could not prepare the Hermes helper folder; run deploy/enable-bridge.sh later."
docker compose build console >/dev/null
if grep -q "^CONSOLE_PASSWORD_HASH='pending'" .env; then
  ask CONSOLE_PASSWORD "Control panel password (12+ characters)" "" secret
  hash="$(printf '%s' "$CONSOLE_PASSWORD" | docker compose run --rm -T console python -c "import sys, auth; print(auth.hash_password(sys.stdin.read()))" | tail -n1)"
  case "$hash" in scrypt:*) ;; *) die "Could not create the password hash." ;; esac
  sed -i "s|^CONSOLE_PASSWORD_HASH=.*|CONSOLE_PASSWORD_HASH='$hash'|" .env
  unset CONSOLE_PASSWORD
fi
docker compose up -d --build
for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:8080/healthz >/dev/null 2>&1 && break; sleep 2; done
curl -fsS http://127.0.0.1:8080/healthz >/dev/null 2>&1 || { docker compose logs --tail=40 console; die "The control panel did not start."; }
bash "$APP_DIR/deploy/enable-boot.sh" || warn "Could not enable start-at-boot for the stack (containers still restart by themselves)."
set -a; . ./.env; set +a
mkdir -p "$BACKUP_DIR"
printf '15 3 * * * root BACKUP_DIR=%s %s/deploy/backup.sh >> /var/log/orrery-backup.log 2>&1\n' "$BACKUP_DIR" "$APP_DIR" > /etc/cron.d/orrery-backup
chmod 644 /etc/cron.d/orrery-backup

# ---------------------------------------------------------------------------------------------
step 9 "SSH safety net (guard, harmless hardening, your key)"
for f in "$APP_DIR"/deploy/scripts/orrery-*; do install -m 0755 "$f" /usr/local/sbin/; done
mkdir -p /var/lib/orrery
cat > /etc/systemd/system/orrery-ssh-guard.service <<'EOF'
[Unit]
Description=Keep SSH reachable (restart it or restore the known-good config if it breaks)
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/orrery-ssh-guard
EOF
cat > /etc/systemd/system/orrery-ssh-guard.timer <<'EOF'
[Unit]
Description=Run the SSH guard every minute
[Timer]
OnBootSec=60
OnUnitActiveSec=60
AccuracySec=10
[Install]
WantedBy=timers.target
EOF
mkdir -p /etc/systemd/system/ssh.service.d
printf '[Service]\nOOMScoreAdjust=-900\n' > /etc/systemd/system/ssh.service.d/orrery.conf
systemctl daemon-reload
systemctl enable --now orrery-ssh-guard.timer >/dev/null 2>&1

cp -a /etc/ssh "/var/lib/orrery/ssh-backup-install-$(date +%Y%m%d-%H%M%S)"
SAFE=/etc/ssh/sshd_config.d/10-orrery-safe.conf
printf 'MaxAuthTries 3\nLoginGraceTime 30\nClientAliveInterval 60\nClientAliveCountMax 5\n' > "$SAFE"
if sshd -t 2>/dev/null; then systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
else rm -f "$SAFE"; warn "Safe SSH settings did not validate and were not applied."; fi

valid_pubkey() { local f rc; f="$(mktemp)"; printf '%s\n' "$1" > "$f"; ssh-keygen -l -f "$f" >/dev/null 2>&1; rc=$?; rm -f "$f"; return $rc; }
if [ -z "${SSH_PUBKEY:-}" ] && [ -r /dev/tty ]; then
  echo "    Paste your SSH PUBLIC key (on your computer: cat ~/.ssh/id_ed25519.pub), or press Enter to skip:"
  read -r SSH_PUBKEY </dev/tty || true
fi
if [ -n "${SSH_PUBKEY:-}" ]; then
  if valid_pubkey "$SSH_PUBKEY"; then
    mkdir -p /root/.ssh && chmod 700 /root/.ssh
    touch /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys
    grep -qxF "$SSH_PUBKEY" /root/.ssh/authorized_keys || echo "$SSH_PUBKEY" >> /root/.ssh/authorized_keys
    echo "    key added. Test it from a NEW terminal before doing anything else."
  else
    warn "That does not look like a valid public key, so it was not added. (It must be the .pub file.)"
  fi
fi

# ---------------------------------------------------------------------------------------------
step 10 "Optional extras"
if [ -z "${PREP_HERMES:-}" ]; then yes_no "Prepare the Hermes user now?" y && PREP_HERMES=1 || PREP_HERMES=0; fi
if [ "$PREP_HERMES" = "1" ]; then bash "$APP_DIR/deploy/hermes-prep.sh" || warn "Hermes preparation had a problem; see the output above."; fi
if [ -z "${INSTALL_TAILSCALE:-}" ]; then
  echo "    Tailscale (free) gives you a SECOND way into the server that does not depend on port 22."
  yes_no "Install Tailscale now? (recommended)" y && INSTALL_TAILSCALE=1 || INSTALL_TAILSCALE=0
fi
if [ -z "${INSTALL_FAIL2BAN:-}" ]; then
  echo "    fail2ban blocks addresses that keep failing SSH logins. Tailscale and your current address are never blocked."
  yes_no "Install fail2ban? (optional)" n && INSTALL_FAIL2BAN=1 || INSTALL_FAIL2BAN=0
fi
if [ "$INSTALL_FAIL2BAN" = "1" ]; then
  apt-get install -y -qq fail2ban >/dev/null
  my_ip="$(echo "${SSH_CONNECTION:-}" | awk '{print $1}')"
  cat > /etc/fail2ban/jail.d/orrery.local <<JAIL
[DEFAULT]
# Never ban this machine, the Tailscale network, or the address you installed from.
ignoreip = 127.0.0.1/8 ::1 100.64.0.0/10 ${my_ip}
bantime = 1h
findtime = 10m
maxretry = 6
[sshd]
enabled = true
backend = systemd
JAIL
  systemctl enable --now fail2ban >/dev/null 2>&1 && systemctl restart fail2ban || warn "fail2ban did not start."
  echo "    fail2ban is on. Unban an address with: fail2ban-client set sshd unbanip ADDRESS"
fi
if [ "$INSTALL_TAILSCALE" = "1" ]; then
  curl -fsSL https://tailscale.com/install.sh | sh >/dev/null
  ufw allow in on tailscale0 >/dev/null 2>&1 || true
  echo "    Sign in: open the link below in your browser (use the same account as on your own computer)."
  if [ -n "${TS_AUTHKEY:-}" ]; then tailscale up --ssh --authkey "$TS_AUTHKEY" || warn "Tailscale sign-in failed."
  else timeout 300 tailscale up --ssh </dev/tty || warn "Tailscale was not authorised in 5 minutes. Run later:  tailscale up --ssh"; fi
fi

# ---------------------------------------------------------------------------------------------
https_ok=""
printf 'Checking HTTPS (can take a minute)'
for _ in $(seq 1 18); do curl -fsS -o /dev/null "https://$DOMAIN/healthz" >/dev/null 2>&1 && { https_ok=1; break; }; printf '.'; sleep 5; done; echo

say "Done"
[ -n "$https_ok" ] && echo "Control:   https://$DOMAIN  (sign in; the Overview tab lists what is left to set up)" || warn "HTTPS is not answering yet. Check DNS, then: cd $APP_DIR/deploy && docker compose logs caddy"
echo "n8n:       https://$N8N_DOMAIN  (create the owner account now and turn on two-factor authentication)"
echo "Settings:  $APP_DIR/deploy/.env  (keep a private copy; it holds the n8n encryption key)"
echo "Health:    orrery-status"
echo "Backups:   $BACKUP_DIR nightly at 03:15 (copy them off this server too)"
cat <<'EOF'

NEXT, in this order:
  1. From a NEW terminal, confirm you can log in with your SSH key. Keep this window open until then.
  2. Test your SECOND DOOR: install Tailscale on your computer, then  ssh root@<this server's Tailscale name>.
     (Find the name with  tailscale status  or in the Tailscale admin console.)
  2b. Copy the backups off the server:  bash deploy/pull-backups.sh root@SERVER_IP ~/.ssh/id_ed25519
  2c. Check everything at once:  orrery-selftest
  3. Hermes: follow the steps printed above, then open the Hermes tab in the control panel.
  4. LAST and optional: orrery-ssh-strict   (key-only SSH with an automatic 10-minute rollback)
EOF
