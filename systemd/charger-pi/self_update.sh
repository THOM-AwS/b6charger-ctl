#!/bin/sh
# Pull-based self-update for a b6charger-ctl host that a CI runner cannot
# reach (see README "Keeping a deployment updated"). Runs from
# b6charger-self-update.timer as root: it owns the install root, writes
# systemd units, and restarts the service. The daemon itself stays
# unprivileged.
#
# Zero third-party Python deps (pyproject.toml `dependencies = []`), so
# this is a plain tarball extract - no git or pip needed on the host.
#
# Sequence, and why each step is where it is:
#   1. compare the deployed version marker with BRANCH's pyproject.toml
#   2. download + extract into a temp dir UNDER the install root, so the
#      final rename is a same-filesystem, atomic mv (a /tmp temp dir
#      silently becomes copy+delete when /tmp is tmpfs)
#   3. refuse anything that is not a complete checkout whose own
#      pyproject.toml matches the version we were told about
#   4. capture host-specific unit settings into a drop-in ONCE, so the
#      shipped unit can stay generic (User=, ExecStart port, packs path
#      are the host's business, not the repo's)
#   5. swap src/, sync the allow-listed units, restart
#   6. health-check the daemon; on ANY failure restore src/ AND the
#      previous units, restart again, exit non-zero
#   7. only then write the version marker - a broken deploy therefore
#      retries next cycle instead of reporting "up to date" forever
#
# The 2026-08-03 "stale shadow copy" incident: a stray package directory
# directly under the install root (not under src/) was picked up by
# Python's CWD-first sys.path. Nothing here leaves anything at that
# level except src/, packs.toml, packs.example.toml and the marker; the
# temp dir is dot-prefixed, nested, and removed on exit.
#
# Configuration: /etc/default/b6charger-self-update (or
# $B6_SELF_UPDATE_CONFIG), sourced if present. Any of the variables in
# the "defaults" block may be set there. Run with --check to print the
# local/remote versions and exit without deploying.

set -eu

# --- configuration --------------------------------------------------------

CONFIG_FILE="${B6_SELF_UPDATE_CONFIG:-/etc/default/b6charger-self-update}"
if [ -f "$CONFIG_FILE" ]; then
  # shellcheck disable=SC1090
  . "$CONFIG_FILE"
fi

# defaults - override in $CONFIG_FILE, not here
: "${REPO_OWNER:=THOM-AwS}"
: "${REPO_NAME:=b6charger-ctl}"
: "${BRANCH:=main}"
: "${INSTALL_DIR:=/opt/b6charger-ctl}"
: "${SERVICE_NAME:=b6charger-httpd}"
: "${SYSTEMD_DIR:=/etc/systemd/system}"
# Path, inside the repo, of the units this host runs. Only the files
# named in UNIT_FILES are ever copied out of it.
: "${UNIT_SUBDIR:=systemd/charger-pi}"
: "${UNIT_FILES:=b6charger-httpd.service b6charger-self-update.service b6charger-self-update.timer}"
# Empty = derive from the live service's --port/--listen (127.0.0.1).
: "${HEALTH_URL:=}"
: "${HEALTH_TIMEOUT_S:=30}"
: "${CURL_MAX_TIME_S:=60}"
: "${MAX_TARBALL_BYTES:=20971520}"
: "${ALLOW_DOWNGRADE:=0}"

VERSION_FILE="$INSTALL_DIR/b6charger_ctl_version.txt"
LIVE_UNIT="$SYSTEMD_DIR/$SERVICE_NAME.service"
DROPIN_DIR="$SYSTEMD_DIR/$SERVICE_NAME.service.d"
DROPIN_FILE="$DROPIN_DIR/10-host.conf"
RAW_BASE="https://raw.githubusercontent.com/$REPO_OWNER/$REPO_NAME/$BRANCH"
TARBALL_URL="https://github.com/$REPO_OWNER/$REPO_NAME/archive/refs/heads/$BRANCH.tar.gz"

log() { echo "[self_update] $(date -Iseconds) $*"; }
die() {
  log "ERROR: $*"
  exit 1
}

# --- helpers --------------------------------------------------------------

# pyproject_version FILE -> prints the [project] version string
pyproject_version() {
  grep -m1 '^version *= *"' "$1" | sed -E 's/^version *= *"([^"]*)".*/\1/'
}

# version_lt A B -> true if A sorts strictly before B (needs sort -V)
version_lt() {
  [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" = "$1" ]
}

# Port the live service listens on, from its ExecStart; falls back to
# the daemon's own default. Only used when HEALTH_URL is not configured.
live_port() {
  exec_line=$(systemctl show -p ExecStart --value "$SERVICE_NAME" 2>/dev/null || true)
  port=$(printf '%s' "$exec_line" | grep -oE -- '--port +[0-9]+' | grep -oE '[0-9]+' | head -n1)
  if [ -z "$port" ]; then
    port=$(printf '%s' "$exec_line" | grep -oE -- '--listen +[^ ;]+' | sed -E 's/.*:([0-9]+)$/\1/' | grep -E '^[0-9]+$' || true)
  fi
  echo "${port:-9111}"
}

health_url() {
  if [ -n "$HEALTH_URL" ]; then
    echo "$HEALTH_URL"
  else
    echo "http://127.0.0.1:$(live_port)/metrics"
  fi
}

# wait_healthy URL SECONDS -> 0 once the daemon answers, 1 on timeout
wait_healthy() {
  url="$1"
  deadline=$(( $(date +%s) + $2 ))
  while :; do
    if curl -fsS --max-time 3 -o /dev/null "$url"; then
      return 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      return 1
    fi
    sleep 1
  done
}

# Capture the host-specific settings of the CURRENTLY INSTALLED unit into
# a drop-in, once. From then on the repo can ship a generic unit and the
# host keeps its own User=, packs path and listen port without either
# side needing to know about the other. Never overwrites an existing
# drop-in: that file belongs to the host, not to this script.
ensure_host_dropin() {
  [ -f "$DROPIN_FILE" ] && return 0
  [ -f "$LIVE_UNIT" ] || return 0
  user=$(grep -m1 '^User=' "$LIVE_UNIT" || true)
  [ -n "$user" ] || return 0
  mkdir -p "$DROPIN_DIR"
  {
    echo "# Host-specific overrides for $SERVICE_NAME, captured by self_update.sh"
    echo "# from the unit that was live at the time. Owned by this host; edit"
    echo "# freely, the updater never rewrites it."
    echo "[Service]"
    echo "$user"
    grep '^WorkingDirectory=' "$LIVE_UNIT" || true
    grep '^Environment=' "$LIVE_UNIT" || true
    exec_start=$(grep -m1 '^ExecStart=' "$LIVE_UNIT" || true)
    if [ -n "$exec_start" ]; then
      echo "ExecStart="
      echo "$exec_start"
    fi
  } > "$DROPIN_FILE"
  log "captured host settings into $DROPIN_FILE"
}

# --- 1. version check -----------------------------------------------------

remote_pyproject=$(mktemp)
trap 'rm -f "$remote_pyproject"' EXIT
curl -fsS --max-time "$CURL_MAX_TIME_S" -o "$remote_pyproject" "$RAW_BASE/pyproject.toml" \
  || die "could not fetch $RAW_BASE/pyproject.toml"
remote_version=$(pyproject_version "$remote_pyproject")
[ -n "$remote_version" ] || die "could not determine remote version from pyproject.toml"

local_version="none"
if [ -f "$VERSION_FILE" ]; then
  local_version=$(tr -d 'v \n' < "$VERSION_FILE")
fi

if [ "${1:-}" = "--check" ]; then
  echo "local=$local_version remote=$remote_version"
  exit 0
fi

if [ "$local_version" = "$remote_version" ]; then
  log "up to date (v$remote_version), nothing to do"
  exit 0
fi

if [ "$local_version" != "none" ] && [ "$ALLOW_DOWNGRADE" != "1" ] \
  && version_lt "$remote_version" "$local_version"; then
  log "refusing downgrade $local_version -> $remote_version (set ALLOW_DOWNGRADE=1 to force)"
  exit 0
fi

log "update available: local=$local_version remote=$remote_version"

# --- 2. download + extract under the install root -------------------------

mkdir -p "$INSTALL_DIR"
tmpdir=$(mktemp -d "$INSTALL_DIR/.update.XXXXXX")
trap 'rm -rf "$tmpdir" "$remote_pyproject"' EXIT

log "downloading $BRANCH.tar.gz to $tmpdir"
curl -fsSL --max-time "$CURL_MAX_TIME_S" --max-filesize "$MAX_TARBALL_BYTES" \
  -o "$tmpdir/src.tar.gz" "$TARBALL_URL" \
  || die "download failed or exceeded $MAX_TARBALL_BYTES bytes"

tar -xzf "$tmpdir/src.tar.gz" -C "$tmpdir"
new_src="$tmpdir/$REPO_NAME-$BRANCH"

# --- 3. refuse anything that doesn't look like the release we expect -------

[ -f "$new_src/b6charger/cli.py" ] || die "tarball has no b6charger/cli.py, aborting"
[ -f "$new_src/pyproject.toml" ] || die "tarball has no pyproject.toml, aborting"
tarball_version=$(pyproject_version "$new_src/pyproject.toml")
[ "$tarball_version" = "$remote_version" ] \
  || die "tarball is v$tarball_version but $BRANCH advertises v$remote_version, aborting"

# --- 4. host settings out of the shipped unit ------------------------------

ensure_host_dropin

# Back up every unit we might replace, so rollback can restore them too.
units_backup="$tmpdir/units.prev"
mkdir -p "$units_backup"
for name in $UNIT_FILES; do
  [ -f "$SYSTEMD_DIR/$name" ] && cp "$SYSTEMD_DIR/$name" "$units_backup/$name"
done

# --- 5. swap + sync + restart ---------------------------------------------

rollback() {
  log "ROLLBACK: restoring previous src/ and units"
  rm -rf "$INSTALL_DIR/src"
  if [ -d "$INSTALL_DIR/src.prev" ]; then
    mv "$INSTALL_DIR/src.prev" "$INSTALL_DIR/src"
  fi
  for name in $UNIT_FILES; do
    if [ -f "$units_backup/$name" ]; then
      cp "$units_backup/$name" "$SYSTEMD_DIR/$name"
    elif [ -f "$SYSTEMD_DIR/$name" ]; then
      rm -f "$SYSTEMD_DIR/$name"
    fi
  done
  systemctl daemon-reload || true
  systemctl restart "$SERVICE_NAME" || log "ROLLBACK: restart of $SERVICE_NAME failed too"
}

old_src_backup="$INSTALL_DIR/src.prev"
rm -rf "$old_src_backup"
if [ -d "$INSTALL_DIR/src" ]; then
  mv "$INSTALL_DIR/src" "$old_src_backup"
fi
mv "$new_src" "$INSTALL_DIR/src"
log "src/ swapped to v$remote_version (previous kept at src.prev until verified)"

units_changed=0
for name in $UNIT_FILES; do
  unit="$INSTALL_DIR/src/$UNIT_SUBDIR/$name"
  live="$SYSTEMD_DIR/$name"
  [ -f "$unit" ] || continue
  if [ ! -f "$live" ] || ! cmp -s "$unit" "$live"; then
    log "unit changed: $name"
    if [ -f "$live" ]; then
      diff -u "$live" "$unit" | sed 's/^/[self_update]   /' || true
    fi
    cp "$unit" "$live"
    units_changed=1
  fi
done

if [ "$units_changed" = "1" ]; then
  log "reloading systemd units"
  systemctl daemon-reload
fi

log "restarting $SERVICE_NAME"
if ! systemctl restart "$SERVICE_NAME"; then
  rollback
  die "restart of $SERVICE_NAME failed on v$remote_version, rolled back to v$local_version"
fi

# --- 6. verify ------------------------------------------------------------

url=$(health_url)
log "health check: $url (up to ${HEALTH_TIMEOUT_S}s)"
if ! wait_healthy "$url" "$HEALTH_TIMEOUT_S"; then
  rollback
  die "health check failed on v$remote_version, rolled back to v$local_version"
fi

# --- 7. commit ------------------------------------------------------------

echo "$remote_version" > "$VERSION_FILE"
rm -rf "$old_src_backup"
log "update complete: v$local_version -> v$remote_version"
