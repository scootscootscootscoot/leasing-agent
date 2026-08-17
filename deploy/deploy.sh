#!/bin/bash
# Deploy leasing-agent to fatman: rsync + user-unit (re)start.
# Mirrors fleet-dashboard/deploy/deploy.sh. Usage: ./deploy.sh [host]
#
# .env is pushed separately and only if it exists locally — it is gitignored
# and must never ride along in a source sync by accident.
set -euo pipefail

HOST=${1:-fatman}
LAN=${LAN_CIDR:-192.168.1.0/24}
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT=$(python3 -c "import json;print(json.load(open('$DIR/config.json'))['dashboard']['port'])")

ssh "$HOST" 'mkdir -p ~/leasing_agent/sources ~/leasing_agent/data ~/.config/systemd/user'

rsync -a --delete --exclude '__pycache__' \
  "$DIR/agent.py" "$DIR/bot.py" "$DIR/crawler.py" "$DIR/dashboard.py" \
  "$DIR/geo.py" "$DIR/learn.py" "$DIR/score.py" "$DIR/sqft.py" \
  "$DIR/store.py" "$DIR/walk.py" "$DIR/config.json" \
  "$HOST:leasing_agent/"
rsync -a --delete --exclude '__pycache__' "$DIR/sources/" "$HOST:leasing_agent/sources/"

if [ -f "$DIR/.env" ]; then
  rsync -a "$DIR/.env" "$HOST:leasing_agent/.env"
  ssh "$HOST" 'chmod 600 ~/leasing_agent/.env'
  echo "pushed .env"
else
  echo "no local .env — create one on $HOST at ~/leasing_agent/.env (see .env.example)"
fi

rsync -a "$DIR/deploy/leasing-agent.service" "$HOST:.config/systemd/user/"

ssh "$HOST" "systemctl --user daemon-reload &&
             systemctl --user enable leasing-agent >/dev/null 2>&1 || true
             systemctl --user restart leasing-agent && sleep 3 &&
             systemctl --user is-active leasing-agent"

# The dashboard port needs a LAN rule to match 8080/8800; Tailscale traffic is
# already permitted by Tailscale's own netfilter chain.
ssh "$HOST" "sudo -n ufw allow from $LAN to any port $PORT proto tcp \
             comment 'leasing-agent dashboard' >/dev/null 2>&1 || \
             echo '(could not add ufw rule — add it by hand if the LAN cannot reach it)'"

echo "deployed: http://$HOST.local:$PORT/ (also reachable on the tailnet)"
