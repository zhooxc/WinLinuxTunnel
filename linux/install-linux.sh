#!/usr/bin/env bash
# wlx Linux-side installer. Fed to `ssh user@host "bash -s"` by install-win.ps1
# (the ONE step that uses the Linux password, typed interactively, never stored).
# Placeholders __X__ are substituted by the Windows installer before sending.
# User-level only: no root, no system packages, nothing outside $HOME.
set -euo pipefail

ROOT="__REMOTE_ROOT__"
[ -n "$ROOT" ] || ROOT="$HOME/wlx"
case "$ROOT" in
  "~"*) ROOT="$HOME${ROOT#\~}" ;;
esac

WLX="$HOME/.wlx"
mkdir -p "$ROOT" "$WLX" "$HOME/.ssh"
chmod 700 "$HOME/.ssh"

# Token + ports (mode 0600).
cat > "$WLX/bridge.env" <<EOF
WLX_AGENT_TOKEN=__TOKEN__
WLX_TUNNEL_PORT=__TUNNEL_PORT__
WLX_AGENT_PORT=__AGENT_PORT__
EOF
chmod 600 "$WLX/bridge.env"

# Tunnel public key, kept so the CLI can rewrite its authorized_keys options.
printf '%s\n' '__TUNNEL_PUB__' > "$WLX/tunnel_ed25519.pub"
chmod 600 "$WLX/tunnel_ed25519.pub"

# Empty rule store (source of truth) if absent.
if [ ! -f "$WLX/tunnels.json" ]; then
  printf '{"version": 1, "rules": []}\n' > "$WLX/tunnels.json"
  chmod 600 "$WLX/tunnels.json"
fi

# authorized_keys: add a key line only if its blob is not already present.
AK="$HOME/.ssh/authorized_keys"
touch "$AK"
chmod 600 "$AK"
add_key() {
  line="$1"
  blob="$(printf '%s' "$line" | awk '{for(i=1;i<=NF;i++) if($i ~ /^(ssh|ecdsa|sk-)/){print $(i+1); exit}}')"
  if [ -n "$blob" ] && grep -qF -- "$blob" "$AK" 2>/dev/null; then
    echo "exists  $(printf '%s' "$line" | awk '{print $NF}')"
  else
    printf '%s\n' "$line" >> "$AK"
    echo "added   $(printf '%s' "$line" | awk '{print $NF}')"
  fi
}
# Tunnel key: control port only at install time; the CLI grows the lists as
# rules are added. permitopen dummy seals the -L direction until rules exist.
add_key 'restrict,port-forwarding,permitlisten="127.0.0.1:__TUNNEL_PORT__",permitopen="127.0.0.1:9" __TUNNEL_PUB__'
# Management key: commands allowed, forwarding forbidden.
add_key 'no-pty,no-port-forwarding,no-agent-forwarding,no-x11-forwarding __MGMT_PUB__'

echo "wlx linux install done: ROOT=$ROOT WLX=$WLX"