#!/usr/bin/env bash
# Tests the SSH safety scripts against a fake sshd setup (no real SSH or root changes involved).
#   bash deploy/scripts/test-ssh-safety.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ok   $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL $1"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1"; fi; }

new_env() {
  T="$(mktemp -d)"; export T
  export ORRERY_SSH_DIR="$T/ssh" ORRERY_STATE="$T/state" ORRERY_KEYS="$T/authorized_keys"
  mkdir -p "$T/bin" "$ORRERY_SSH_DIR/sshd_config.d" "$ORRERY_STATE"
  echo "Include sshd_config.d/*.conf" > "$ORRERY_SSH_DIR/sshd_config"
  echo "yes" > "$T/listening"; echo "yes" > "$T/ssh_can_start"
  cat > "$T/bin/sshd" <<'S'
#!/bin/bash
case "$1" in
  -t) grep -rqs INVALID "$ORRERY_SSH_DIR" && { echo "bad config" >&2; exit 1; }; exit 0 ;;
  -T) echo "port 22" ;;
esac
S
  cat > "$T/bin/ss" <<'S'
#!/bin/bash
echo "State Recv-Q Send-Q Local Address:Port Peer Address:Port"
[ "$(cat "$T/listening")" = "yes" ] && echo "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*"
exit 0
S
  cat > "$T/bin/systemctl" <<'S'
#!/bin/bash
echo "$*" >> "$T/systemctl.log"
case "$*" in "restart ssh") [ "$(cat "$T/ssh_can_start")" = "yes" ] && echo yes > "$T/listening" ;; esac
exit 0
S
  printf '#!/bin/bash\nexit 0\n' > "$T/bin/logger"; cp "$T/bin/logger" "$T/bin/sleep"
  printf '#!/bin/bash\necho "$*" >> "$T/systemd-run.log"\n' > "$T/bin/systemd-run"
  chmod +x "$T"/bin/*
  export PATH="$T/bin:$PATH"
  S="$ORRERY_SSH_DIR/sshd_config.d/00-orrery-strict.conf"
}

echo "guard: healthy system"
new_env
bash "$HERE/orrery-ssh-guard" >/dev/null
check "does nothing when SSH is fine" "[ ! -s '$T/systemctl.log' ]"

echo "guard: invalid config, known-good copy exists"
new_env
mkdir -p "$ORRERY_STATE/sshd-known-good/sshd_config.d"
echo "Include sshd_config.d/*.conf" > "$ORRERY_STATE/sshd-known-good/sshd_config"
echo "MaxAuthTries 3" > "$ORRERY_STATE/sshd-known-good/sshd_config.d/10-orrery-safe.conf"
echo "INVALID line" > "$ORRERY_SSH_DIR/sshd_config.d/00-orrery-strict.conf"
bash "$HERE/orrery-ssh-guard" >/dev/null
check "restores the known-good config" "[ ! -e '$ORRERY_SSH_DIR/sshd_config.d/00-orrery-strict.conf' ] && [ -f '$ORRERY_SSH_DIR/sshd_config.d/10-orrery-safe.conf' ]"
check "restarts ssh afterwards" "grep -q 'restart ssh' '$T/systemctl.log'"

echo "guard: invalid config, no known-good copy"
new_env
echo "INVALID line" > "$ORRERY_SSH_DIR/sshd_config.d/00-orrery-strict.conf"
bash "$HERE/orrery-ssh-guard" >/dev/null
check "removes the project's drop-ins" "[ ! -e '$ORRERY_SSH_DIR/sshd_config.d/00-orrery-strict.conf' ]"

echo "guard: SSH not listening, a restart fixes it"
new_env; echo no > "$T/listening"
bash "$HERE/orrery-ssh-guard" >/dev/null
check "restarts ssh and it comes back" "[ \"\$(cat '$T/listening')\" = yes ] && grep -q 'restart ssh' '$T/systemctl.log'"

echo "guard: SSH not listening, restart alone does not fix it"
new_env; echo no > "$T/listening"; echo no > "$T/ssh_can_start"
mkdir -p "$ORRERY_STATE/sshd-known-good/sshd_config.d"
echo "Include sshd_config.d/*.conf" > "$ORRERY_STATE/sshd-known-good/sshd_config"
echo "KNOWN GOOD" > "$ORRERY_STATE/sshd-known-good/sshd_config.d/10-orrery-safe.conf"
rm -f "$ORRERY_SSH_DIR/sshd_config.d/"*; echo "odd setting" > "$ORRERY_SSH_DIR/sshd_config.d/99-odd.conf"
bash "$HERE/orrery-ssh-guard" >/dev/null
check "falls back to the known-good config" "[ -f '$ORRERY_SSH_DIR/sshd_config.d/10-orrery-safe.conf' ] && [ ! -e '$ORRERY_SSH_DIR/sshd_config.d/99-odd.conf' ]"

echo "strict: safety checks"
new_env
out="$(bash "$HERE/orrery-ssh-strict" --yes 2>&1)"; rc=$?
check "refuses when root has no SSH key" "[ $rc -ne 0 ] && echo \"\$out\" | grep -q 'No SSH key'"
check "changes nothing when refused" "[ ! -e '$ORRERY_SSH_DIR/sshd_config.d/00-orrery-strict.conf' ]"

echo "strict: normal run"
new_env; echo "ssh-ed25519 AAAAC3Nz test@mac" > "$ORRERY_KEYS"
bash "$HERE/orrery-ssh-strict" --yes >/dev/null 2>&1
check "writes the strict file" "grep -q 'PasswordAuthentication no' '$S'"
check "starts the 10-minute rollback timer" "grep -q -- '--on-active=10min' '$T/systemd-run.log'"
check "backs up the SSH directory first" "ls '$ORRERY_STATE' | grep -q '^ssh-backup-'"

echo "strict: provider address exception"
new_env; echo "ssh-ed25519 AAAAC3Nz test@mac" > "$ORRERY_KEYS"
bash "$HERE/orrery-ssh-strict" --yes --provider-ip 203.0.113.9 >/dev/null 2>&1
check "keeps passwords only for that address" "grep -q 'Match Address 203.0.113.9' '$S' && ! grep -B1 'PasswordAuthentication yes' '$S' | grep -q 'PermitRootLogin'"
new_env; echo "ssh-ed25519 AAAAC3Nz test@mac" > "$ORRERY_KEYS"
bash "$HERE/orrery-ssh-strict" --yes --provider-ip 'bad;ip' >/dev/null 2>&1
check "rejects a malformed address" "[ ! -e '$ORRERY_SSH_DIR/sshd_config.d/00-orrery-strict.conf' ]"

echo "strict: a config that does not validate is removed"
new_env; echo "ssh-ed25519 AAAAC3Nz test@mac" > "$ORRERY_KEYS"
echo "INVALID line" > "$ORRERY_SSH_DIR/sshd_config.d/60-broken.conf"
bash "$HERE/orrery-ssh-strict" --yes >/dev/null 2>&1
check "removes its own file and reloads nothing" "[ ! -e '$S' ] && ! grep -q reload '$T/systemctl.log'"

echo "rollback and confirm"
new_env; echo "ssh-ed25519 AAAAC3Nz test@mac" > "$ORRERY_KEYS"
bash "$HERE/orrery-ssh-strict" --yes >/dev/null 2>&1
bash "$HERE/orrery-ssh-rollback" >/dev/null 2>&1
check "rollback removes the strict file" "[ ! -e '$S' ]"
bash "$HERE/orrery-ssh-strict" --yes >/dev/null 2>&1
bash "$HERE/orrery-ssh-confirm" >/dev/null 2>&1
check "confirm saves a known-good copy" "[ -f '$ORRERY_STATE/sshd-known-good/sshd_config.d/00-orrery-strict.conf' ]"
check "confirm stops the rollback timer" "grep -q 'stop orrery-ssh-rollback.timer' '$T/systemctl.log'"

echo
echo "passed: $PASS  failed: $FAIL"
[ "$FAIL" -eq 0 ]
