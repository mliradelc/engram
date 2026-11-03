#!/usr/bin/env bash
#
# makemkv-key-refresh — keep the MakeMKV beta key current in an Engram container.
#
# Ported from Automatic Ripping Machine's /opt/arm/scripts/update_key.sh, adapted for
# Engram's Docker layout. ARM rewrites settings.conf in place and its running process
# picks the key up; Engram reads the key at container start, so a changed key needs a
# restart.
#
# Design rules, each for a failure seen or foreseeable:
#   * Never write a partial or empty key. If the forum cannot be parsed, exit non-zero
#     and leave the existing key untouched — a blank app_Key stops ripping with a
#     registration error weeks later, with no obvious cause.
#   * Only restart when the key actually changed, so a quiet month does not interrupt
#     a rip in progress.
#   * Never restart while a job is running. Killing a transcode mid-flight is the
#     failure that killed ARM jobs 81 and 85.
#   * The scraper is unofficial. It is used because ARM already relies on it; if the
#     forum markup changes, this exits non-zero and leaves the key alone.
#
# Usage:
#   makemkv-key-refresh [--dry-run] [--force-restart]
#
# Exit codes:
#   0  success (key current, or refreshed and restarted)
#   10 key unchanged — nothing to do
#   20 forum unreachable or no key found
#   21 key failed validation
#   30 settings.conf unreadable or corrupt after write
#   40 container not running (key written, restart deferred)
#   41 restart deferred: a job is active
#
set -euo pipefail

# --- configuration -----------------------------------------------------------
CONTAINER="${ENGRAM_CONTAINER:-engram}"
# Host path of the container's /config bind mount. Overridden by ENGRAM_CONFIG_DIR.
CONFIG_DIR="${ENGRAM_CONFIG_DIR:-/opt/engram/config}"
SETTINGS="${CONFIG_DIR}/.MakeMKV/settings.conf"
PUID="${ENGRAM_PUID:-1000}"
PGID="${ENGRAM_PGID:-1000}"
FORUM_URL="https://forum.makemkv.com/forum/viewtopic.php?f=5&t=1053"
# Beta keys are "T-" + 66 chars; permanent keys are "M-" + 66 chars.
KEY_RE='[TM]-[A-Za-z0-9@_]{66}'
ENGRAM_API="http://127.0.0.1:8000"
STATE_DIR="/var/lib/makemkv-key-refresh"

DRY_RUN=0
FORCE_RESTART=0
for arg in "$@"; do
    case "$arg" in
        --dry-run)       DRY_RUN=1 ;;
        --force-restart) FORCE_RESTART=1 ;;
        -h|--help)       sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

log()  { printf '%s [makemkv-key] %s\n' "$(date -Is)" "$*"; }
fail() { log "ERROR: $*"; exit "${2:-1}"; }

# Redact a key for logging: keep the prefix and last 4 characters only.
redact() {
    local k="$1"
    printf '%s...%s' "${k:0:6}" "${k: -4}"
}

# --- 1. fetch the key --------------------------------------------------------
log "fetching beta key from ${FORUM_URL}"
html="$(curl -fsSL --max-time 30 --retry 2 --retry-delay 5 "$FORUM_URL" 2>/dev/null)" \
    || fail "forum unreachable (curl failed)" 20

# Exactly one match is expected. Zero means the page changed or the key is absent;
# more than one means the markup now contains something we do not understand.
mapfile -t matches < <(printf '%s' "$html" | grep -oP "$KEY_RE" || true)
if [ "${#matches[@]}" -eq 0 ]; then
    fail "no MakeMKV key found on the forum page — markup may have changed" 20
fi
if [ "${#matches[@]}" -gt 1 ]; then
    fail "found ${#matches[@]} candidate keys; refusing to guess" 20
fi
NEW_KEY="${matches[0]}"

# Validate the shape before trusting it. Belt and braces with the regex above.
if ! [[ "$NEW_KEY" =~ ^[TM]-[A-Za-z0-9@_]{66}$ ]]; then
    fail "scraped value does not look like a MakeMKV key (len ${#NEW_KEY})" 21
fi
log "found key $(redact "$NEW_KEY") (${#NEW_KEY} chars, $( [[ $NEW_KEY == T-* ]] && echo beta || echo permanent ))"

# --- 2. read the current key -------------------------------------------------
CURRENT_KEY=""
if [ -f "$SETTINGS" ]; then
    CURRENT_KEY="$(grep -oP '^app_Key\s*=\s*"\K[^"]+' "$SETTINGS" 2>/dev/null | head -1 || true)"
fi

if [ "$CURRENT_KEY" = "$NEW_KEY" ]; then
    log "key already current — nothing to do"
    exit 10
fi
log "key differs (current: $( [ -n "$CURRENT_KEY" ] && redact "$CURRENT_KEY" || echo '<none>' ))"

if [ "$DRY_RUN" -eq 1 ]; then
    log "dry run: would write $(redact "$NEW_KEY") to ${SETTINGS} and restart ${CONTAINER}"
    exit 0
fi

# --- 3. write the key --------------------------------------------------------
# Engram's entrypoint rebuilds this file from MAKEMKV_APP_KEY on every start when that
# env var is set, which would silently revert a key written here. Detect that and
# refuse rather than pretend to succeed.
if docker inspect "$CONTAINER" >/dev/null 2>&1; then
    if docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" \
        | grep -q '^MAKEMKV_APP_KEY=.\+'; then
        fail "container ${CONTAINER} sets MAKEMKV_APP_KEY, which overwrites settings.conf on start.
       Remove that env var (or update it) and re-run; otherwise this change is reverted at the next restart." 21
    fi
fi

mkdir -p "$(dirname "$SETTINGS")"
touch "$SETTINGS"

# printf, not sed: a key containing sed metacharacters cannot corrupt the file.
# The key goes on the first line; every other line is preserved verbatim.
tmp="$(mktemp "${SETTINGS}.XXXXXX")"
{
    printf 'app_Key = "%s"\n' "$NEW_KEY"
    grep -vE '^[[:space:]]*app_Key[[:space:]]*=' "$SETTINGS" || true
} > "$tmp"
mv -f "$tmp" "$SETTINGS"
chown "${PUID}:${PGID}" "$SETTINGS" 2>/dev/null || log "warning: could not chown ${SETTINGS} to ${PUID}:${PGID}"
chmod 0640 "$SETTINGS" 2>/dev/null || true

# --- 4. verify the write -----------------------------------------------------
written="$(grep -oP '^app_Key\s*=\s*"\K[^"]+' "$SETTINGS" 2>/dev/null | head -1 || true)"
if [ "$written" != "$NEW_KEY" ]; then
    fail "settings.conf does not hold the new key after writing" 30
fi
count="$(grep -cE '^[[:space:]]*app_Key[[:space:]]*=' "$SETTINGS" || true)"
if [ "$count" -ne 1 ]; then
    fail "settings.conf has ${count} app_Key lines; expected exactly 1" 30
fi
log "wrote $(redact "$NEW_KEY") to ${SETTINGS}"

# --- 5. restart, but only when it is safe ------------------------------------
if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
    log "container ${CONTAINER} is not running — key written, restart not needed"
    exit 40
fi

# A running job must not be interrupted. Engram exposes job state at /api/jobs.
# An unreachable API is treated as "unknown", and we defer rather than risk a rip.
if [ "$FORCE_RESTART" -eq 0 ]; then
    active="$(curl -fsS --max-time 10 "${ENGRAM_API}/api/jobs?status=active" 2>/dev/null || true)"
    if [ -z "$active" ]; then
        log "could not read job state from ${ENGRAM_API}; deferring restart to the next run"
        exit 41
    fi
    # Any non-empty job array means work is in flight.
    if printf '%s' "$active" | grep -qE '"(id|job_id)"'; then
        log "a job is active — deferring restart to the next run"
        exit 41
    fi
fi

log "restarting ${CONTAINER} to load the new key"
docker restart "$CONTAINER" >/dev/null
log "restarted ${CONTAINER}"
exit 0
