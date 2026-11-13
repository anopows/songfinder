#!/bin/bash
# Ubuntu 24.04 installer. Configure /etc/songfinder.env before running as root.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Run as root.' >&2; exit 1; }
[ -s /etc/songfinder.env ] || { echo 'Create /etc/songfinder.env from .env.example first.' >&2; exit 1; }
for tool in python3.12 uv ffmpeg bwrap apparmor_parser rsync; do
    command -v "$tool" >/dev/null || { echo "Missing prerequisite: $tool" >&2; exit 1; }
done
SONGFINDER_SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
getent passwd songfinder >/dev/null || useradd --system --home /var/lib/songfinder --shell /usr/sbin/nologin songfinder
install -d -m 0755 /opt/songfinder /opt/songfinder/bin
install -d -m 0700 -o songfinder -g songfinder /var/lib/songfinder
if [ "$SONGFINDER_SOURCE_DIR" != /opt/songfinder ]; then
    rsync -a --exclude .git --exclude .venv --exclude '.env*' --exclude __pycache__ \
      --exclude .pytest_cache --exclude .songfinder-cache --exclude 'web-data*' \
      "$SONGFINDER_SOURCE_DIR/" /opt/songfinder/
fi
chown -R root:root /opt/songfinder
chmod -R a+rX /opt/songfinder
chmod 0600 /etc/songfinder.env
install -m 0755 /usr/bin/bwrap /opt/songfinder/bin/bwrap
install -m 0644 /opt/songfinder/deploy/songfinder-bwrap.apparmor /etc/apparmor.d/songfinder-bwrap
apparmor_parser -r /etc/apparmor.d/songfinder-bwrap
if [ ! -x /opt/songfinder/.venv/bin/python ]; then uv venv --python python3.12 /opt/songfinder/.venv; fi
UV_CACHE_DIR=/var/tmp/songfinder-uv uv pip install --python /opt/songfinder/.venv/bin/python -r /opt/songfinder/requirements-web.lock
install -m 0644 /opt/songfinder/deploy/songfinder-api.service /opt/songfinder/deploy/songfinder-worker.service /opt/songfinder/deploy/songfinder-purge.service /opt/songfinder/deploy/songfinder-purge.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable songfinder-api songfinder-worker songfinder-purge.timer
systemctl restart songfinder-api songfinder-worker
systemctl start songfinder-purge.timer
systemctl is-active songfinder-api songfinder-worker
