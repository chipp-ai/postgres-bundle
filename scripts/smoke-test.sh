#!/usr/bin/env bash
# Extract a bundle archive into a fresh temp dir and prove it runs from
# there: initdb, pg_ctl start, and the extensions alchemist apps rely on.
#
#   scripts/smoke-test.sh postgresql-16.3.0-aarch64-apple-darwin.tar.gz
#
# Every DYLD_* / LD_LIBRARY_PATH variable is unset first, so the binaries
# must find their libraries through their own load commands. On macOS the
# loaded image list of `postgres` is also checked for any library outside
# the extracted tree and the system locations.
set -euo pipefail

ARCHIVE="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
for v in $(env | sed -n 's/^\(DYLD_[A-Z_]*\)=.*/\1/p'); do unset "$v"; done
unset LD_LIBRARY_PATH || true

# Physical path: dyld reports /private/var/..., not the /var symlink.
WORK="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/pgbundle-smoke.XXXXXX")" && pwd -P)"
PGROOT="$WORK/install"
DATA="$WORK/data"
SOCK="$WORK/sock"
PORT="${SMOKE_PORT:-55432}"
mkdir -p "$PGROOT" "$SOCK"
cleanup() {
  "$PGROOT/bin/pg_ctl" -D "$DATA" -m immediate stop >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

tar -xzf "$ARCHIVE" -C "$PGROOT"
echo "extracted to $PGROOT"
"$PGROOT/bin/postgres" --version

if [ "$(uname -s)" = "Darwin" ]; then
  # dyld prints every image it loads; anything outside the tree and the
  # system locations means a dependency was missed.
  LOADED="$(DYLD_PRINT_LIBRARIES=1 "$PGROOT/bin/postgres" --version 2>&1 >/dev/null \
    | sed -n 's/^dyld\[[0-9]*\]: <[^>]*> //p')"
  STRAY="$(printf '%s\n' "$LOADED" | grep -v -e "^$PGROOT/" -e '^/usr/lib/' -e '^/System/' -e '^$' || true)"
  if [ -n "$STRAY" ]; then
    echo "postgres loaded libraries from outside the bundle:" >&2
    echo "$STRAY" >&2
    exit 1
  fi
  echo "postgres loaded $(printf '%s\n' "$LOADED" | grep -c "^$PGROOT/" || true) libraries from the bundle, none from elsewhere"
fi

"$PGROOT/bin/initdb" -D "$DATA" -U postgres -A trust -E UTF8 --no-instructions >/dev/null
"$PGROOT/bin/pg_ctl" -D "$DATA" -w -l "$WORK/server.log" \
  -o "-p $PORT -k $SOCK -c listen_addresses=''" start || { cat "$WORK/server.log"; exit 1; }

"$PGROOT/bin/psql" -h "$SOCK" -p "$PORT" -U postgres -d postgres -v ON_ERROR_STOP=1 -X <<'SQL'
CREATE EXTENSION vector;
CREATE EXTENSION pgcrypto;
CREATE EXTENSION "uuid-ossp";
SELECT version();
SELECT extname, extversion FROM pg_extension ORDER BY extname;
SELECT '[1,2,3]'::vector <-> '[4,5,6]'::vector AS l2_distance,
       encode(digest('chipp', 'sha256'), 'hex') AS sha256,
       uuid_generate_v4() IS NOT NULL AS uuid_ok,
       gen_random_uuid() IS NOT NULL AS gen_uuid_ok;
SQL
echo "smoke test passed"
