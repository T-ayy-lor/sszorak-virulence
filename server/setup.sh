#!/bin/bash
# Taynalysis server setup for Ubuntu 24.04 on Amazon Lightsail. Run it from the Lightsail browser SSH window with:
#   curl -fsSL https://raw.githubusercontent.com/t-ayy-lor/taynalysis/main/server/setup.sh -o setup.sh && sudo bash setup.sh
# Safe to run again: it keeps the existing upload token and site files, and refreshes everything else.
set -euo pipefail

DOMAIN="taynalysis.com"
RAW="https://raw.githubusercontent.com/t-ayy-lor/taynalysis/main/server"
SITE="/srv/taynalysis/site"
APP="/opt/taynalysis"
CONF="/etc/taynalysis"
SVC_USER="taynalysis"

if [ "$(id -u)" -ne 0 ]; then echo "Run this with sudo (see the first lines of this script)."; exit 1; fi
say() { printf '\n==> %s\n' "$1"; }
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a

say "1/7  Swap file (1 GB) so updates never run out of memory"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 1G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "     added"
else
  echo "     already on"
fi

say "2/7  System updates"
apt-get update -q
apt-get upgrade -y -q
apt-get install -y -q curl gnupg debian-keyring debian-archive-keyring apt-transport-https python3 unattended-upgrades

say "3/7  Caddy (web server with automatic HTTPS), from the Caddy project's own package repository"
if [ ! -f /usr/share/keyrings/caddy-stable-archive-keyring.gpg ]; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
fi
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
apt-get update -q
apt-get install -y -q caddy

say "4/7  Automatic security updates (restarts, when one needs it, at 5:30 AM Eastern, far from raid time)"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONFEOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONFEOF
cat > /etc/apt/apt.conf.d/52taynalysis-upgrades <<'CONFEOF'
// Also keep Caddy up to date from its own repository
Unattended-Upgrade::Origins-Pattern:: "site=dl.cloudsmith.io";
Unattended-Upgrade::Automatic-Reboot "true";
// server clock is UTC: 09:30 UTC is 5:30 AM Eastern daylight time, 4:30 AM standard time
Unattended-Upgrade::Automatic-Reboot-Time "09:30";
CONFEOF
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

say "5/7  Upload service"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
mkdir -p "$SITE" "$APP" "$CONF"
chown -R "$SVC_USER:$SVC_USER" /srv/taynalysis
chmod 755 /srv/taynalysis "$SITE"
curl -fsSL "$RAW/upload_server.py" -o "$APP/upload_server.py"
chmod 755 "$APP/upload_server.py"
NEW_TOKEN=0
if [ ! -s "$CONF/upload-token" ]; then
  python3 -c 'import secrets; print(secrets.token_hex(32))' > "$CONF/upload-token"
  NEW_TOKEN=1
fi
chown root:"$SVC_USER" "$CONF" "$CONF/upload-token"
chmod 750 "$CONF"
chmod 640 "$CONF/upload-token"
cat > /etc/systemd/system/taynalysis-upload.service <<UNITEOF
[Unit]
Description=Taynalysis upload service
After=network.target

[Service]
User=$SVC_USER
Group=$SVC_USER
Environment=TAYNALYSIS_SITE=$SITE
Environment=TAYNALYSIS_TOKEN_FILE=$CONF/upload-token
Environment=TAYNALYSIS_PORT=8787
ExecStart=/usr/bin/python3 $APP/upload_server.py
Restart=always
RestartSec=2
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$SITE

[Install]
WantedBy=multi-user.target
UNITEOF
systemctl daemon-reload
systemctl enable taynalysis-upload >/dev/null 2>&1
systemctl restart taynalysis-upload

say "6/7  Web server settings for $DOMAIN"
if [ ! -f "$SITE/index.html" ]; then
  cat > "$SITE/index.html" <<'HTMLEOF'
<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Taynalysis</title>
<body style="background:#151515;color:#ddd;font:16px system-ui;display:grid;place-items:center;height:100vh;margin:0"><p>Taynalysis is being set up.</p>
HTMLEOF
  chown "$SVC_USER:$SVC_USER" "$SITE/index.html"
fi
cat > /etc/caddy/Caddyfile <<CADDYEOF
$DOMAIN {
	encode zstd gzip
	handle /api/* {
		reverse_proxy 127.0.0.1:8787
	}
	handle {
		root * $SITE
		header Cache-Control "no-cache"
		try_files {path} {path}.html
		file_server {
			hide .*
		}
	}
}

www.$DOMAIN {
	redir https://$DOMAIN{uri} permanent
}
CADDYEOF
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
systemctl enable caddy >/dev/null 2>&1
systemctl reload caddy || systemctl restart caddy

say "7/7  Checking"
sleep 2
if curl -fsS http://127.0.0.1:8787/api/health >/dev/null; then echo "     upload service: running"; else echo "     upload service: NOT running (sudo journalctl -u taynalysis-upload)"; fi
if systemctl is-active --quiet caddy; then echo "     web server: running"; else echo "     web server: NOT running (sudo journalctl -u caddy)"; fi
echo "     HTTPS: Caddy gets the certificate for $DOMAIN on its own once the domain points here (it keeps retrying until then)."

echo
echo "================================================================"
if [ "$NEW_TOKEN" = 1 ]; then echo " Setup finished. Your upload token (copy it now):"; else echo " Setup finished. Your upload token (unchanged):"; fi
echo
echo "   $(cat "$CONF/upload-token")"
echo
echo " The uploader on your Mac asks for it once and saves it."
echo "================================================================"
