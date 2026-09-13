#!/bin/sh
# Pull-based self-update for the charger-pi deployment. Runs on a timer
# (see b6charger-self-update.timer), not triggered by CI - charger-pi's
# mesh network isn't reachable from base's runner fleet, and charger-pi
# itself is armv6l/72Mi-free-RAM (too weak to host a GitHub Actions
# runner). Instead charger-pi periodically checks GitHub over the
# outbound HTTPS path it already proved working (same path the
# Prometheus agent's remote_write uses) and pulls updates itself.
#
# Zero third-party Python deps (see pyproject.toml `dependencies = []`)
# so this is a plain tarball extract, not pip/git - neither is even
# installed on this minimal image.
#
# Deliberately extracts to a temp dir OUTSIDE /opt/b6charger-ctl and
# only atomically renames the verified result into place. The 2026-08-03
# "stale shadow copy" incident happened because a stray package
# directory sat directly under /opt/b6charger-ctl/ (not under src/) and
# Python's CWD-first sys.path picked it up silently. Never leave
# anything at that top level except src/, packs.toml,
# packs.example.toml, and the version marker.

set -eu

REPO_OWNER="THOM-AwS"
REPO_NAME="b6charger-ctl"
INSTALL_DIR="/opt/b6charger-ctl"
VERSION_FILE="$INSTALL_DIR/b6charger_ctl_version.txt"
SERVICE_NAME="b6charger-httpd"
SELF_UPDATE_SERVICE_DIR="$INSTALL_DIR/src/systemd/charger-pi"

log() { echo "[self_update] $(date -Iseconds) $*"; }

remote_version=$(curl -fsS "https://raw.githubusercontent.com/$REPO_OWNER/$REPO_NAME/main/pyproject.toml" \
  | grep -m1 '^version' | sed -E 's/version = "(.*)"/\1/')

if [ -z "$remote_version" ]; then
  log "ERROR: could not determine remote version, aborting"
  exit 1
fi

local_version="none"
if [ -f "$VERSION_FILE" ]; then
  local_version=$(tr -d 'v \n' < "$VERSION_FILE")
fi

if [ "$local_version" = "$remote_version" ]; then
  log "up to date (v$remote_version), nothing to do"
  exit 0
fi

log "update available: local=$local_version remote=$remote_version"

tmpdir=$(mktemp -d /tmp/b6charger-ctl-update.XXXXXX)
trap 'rm -rf "$tmpdir"' EXIT

log "downloading main.tar.gz to $tmpdir"
curl -fsSL "https://github.com/$REPO_OWNER/$REPO_NAME/archive/refs/heads/main.tar.gz" \
  -o "$tmpdir/main.tar.gz"

tar -xzf "$tmpdir/main.tar.gz" -C "$tmpdir"
extracted_dir="$tmpdir/$REPO_NAME-main"

# Sanity check before touching anything live - refuse to deploy
# something that doesn't look like a real, complete checkout.
if [ ! -f "$extracted_dir/b6charger/cli.py" ]; then
  log "ERROR: extracted tarball doesn't contain b6charger/cli.py, aborting deploy"
  exit 1
fi
if [ ! -f "$extracted_dir/pyproject.toml" ]; then
  log "ERROR: extracted tarball doesn't contain pyproject.toml, aborting deploy"
  exit 1
fi

# Atomic swap: build the new src/ fully in temp, then rename over the
# old one. A rename within the same filesystem is atomic - there's no
# window where src/ is half-old/half-new.
new_src="$tmpdir/src-new"
mv "$extracted_dir" "$new_src"

old_src_backup="$INSTALL_DIR/src.prev"
rm -rf "$old_src_backup"
if [ -d "$INSTALL_DIR/src" ]; then
  mv "$INSTALL_DIR/src" "$old_src_backup"
fi
mv "$new_src" "$INSTALL_DIR/src"

echo "$remote_version" > "$VERSION_FILE"
log "deployed v$remote_version"

# Sync systemd units for this host from the freshly-deployed repo, if
# they've changed. Compares content, not mtime - a redeploy of the same
# version shouldn't cause spurious restarts.
units_changed=0
for unit in "$SELF_UPDATE_SERVICE_DIR"/*.service "$SELF_UPDATE_SERVICE_DIR"/*.timer; do
  [ -f "$unit" ] || continue
  name=$(basename "$unit")
  live="/etc/systemd/system/$name"
  if [ ! -f "$live" ] || ! cmp -s "$unit" "$live"; then
    log "unit changed: $name"
    cp "$unit" "$live"
    units_changed=1
  fi
done

if [ "$units_changed" = "1" ]; then
  log "reloading systemd units"
  systemctl daemon-reload
fi

log "restarting $SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

# Only remove the previous src/ once the restart above succeeded - if
# systemctl restart fails, set -e exits before this line and src.prev
# stays available for manual rollback (mv src.prev src).
rm -rf "$old_src_backup"

log "update complete: v$local_version -> v$remote_version"
