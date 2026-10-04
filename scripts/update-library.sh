#!/usr/bin/env sh
# Upload a Playnite HTML export to the Game Library website (Linux/macOS).
# Usage: GAMELIB_URL=http://192.168.1.20:8095 GAMELIB_TOKEN=... ./update-library.sh "/path/to/export"
set -eu
SRC="${1:-${GAMELIB_EXPORT:-}}"
URL="${GAMELIB_URL:?set GAMELIB_URL}"; URL="${URL%/}"
TOKEN="${GAMELIB_TOKEN:?set GAMELIB_TOKEN}"
[ -f "$SRC/index.html" ] || { echo "No Playnite export found in '$SRC'"; exit 1; }
TMP="$(mktemp -t gamelib.XXXXXX).tar"
trap 'rm -f "$TMP"' EXIT
echo "Packing $SRC ..."; tar -C "$SRC" -cf "$TMP" .
echo "Uploading ..."
curl -fsS -X POST -H "X-Token: $TOKEN" -H "Content-Type: application/x-tar" --data-binary "@$TMP" "$URL/api/import" >/dev/null
echo "Processing ..."
while :; do
  sleep 2
  ST="$(curl -fsS -H "X-Token: $TOKEN" "$URL/api/status")"
  STATE="$(printf '%s' "$ST" | sed -n 's/^{"state": "\([a-z]*\)".*/\1/p')"
  case "$STATE" in running) printf '.';; done) echo; echo "Done."; exit 0;; *) echo; echo "Import failed:"; printf '%s\n' "$ST" | head -c 2000; exit 1;; esac
done
