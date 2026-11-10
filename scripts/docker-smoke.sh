#!/bin/bash
# Build a throwaway image, run it against a temp fixture tree (never a real
# folder, never your iCloud Drive) and prove the safety behaviours end to end.
# Everything it creates is removed on exit. Needs Docker and curl.
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="keepsake-smoke-$$"
NAME="$TAG"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/keepsake-smoke.XXXXXX")
PW="smoke-test-password"
pass=0
cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  # Files the container wrote belong to its user; hand them back before rm.
  docker run --rm --entrypoint sh -v "$TMP:/t" "$TAG" -c 'chmod -R a+rwX /t' >/dev/null 2>&1 || true
  docker rmi -f "$TAG" >/dev/null 2>&1 || true
  [ -n "$TMP" ] && [ -d "$TMP" ] && { rm -rf "$TMP" || true; }
}
trap cleanup EXIT
ok()   { pass=$((pass + 1)); echo "ok   $1"; }
bad()  { echo "FAIL $1"; docker logs "$NAME" 2>&1 | tail -20 || true; exit 1; }
api()  { curl -fsS -H "Authorization: Bearer $PW" "$@"; }
post() { api -X POST -H 'Content-Type: application/json' -d "$2" "http://127.0.0.1:$PORT$1"; }
get()  { api "http://127.0.0.1:$PORT$1"; }
jget() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

mkdir -p "$TMP/sources/Photos/2025" "$TMP/icloud" "$TMP/data"
for i in 1 2 3 4 5 6 7 8 9 10; do echo "photo $i" > "$TMP/sources/Photos/2025/$i.jpg"; done
chmod -R a+rwX "$TMP"

docker build -q -t "$TAG" . >/dev/null
ok "image builds"

# 1. It refuses to start on a reachable address without a password.
if out=$(docker run --rm "$TAG" 2>&1); then bad "started without a password"; fi
echo "$out" | grep -q "needs a password" && ok "refuses to start without a password (exit non-zero, plain sentence)" || bad "no plain refusal: $out"
if out=$(docker run --rm -e KEEPSAKE_PASSWORD=short "$TAG" 2>&1); then bad "started with a short password"; fi
ok "refuses a short password"

# 2. Run it the way the compose file does: read-only root, no capabilities, source read-only.
docker run -d --name "$NAME" --read-only --tmpfs /tmp --cap-drop ALL \
  --security-opt no-new-privileges:true -p 127.0.0.1::8765 \
  -e KEEPSAKE_PASSWORD="$PW" -e KEEPSAKE_ROOTS=sources=/sources -e KEEPSAKE_MIN_FREE_GB=0 \
  -v "$TMP/sources:/sources:ro" -v "$TMP/icloud:/icloud" -v "$TMP/data:/data" "$TAG" >/dev/null
PORT=$(docker port "$NAME" 8765/tcp | head -1 | sed 's/.*://')
for _ in $(seq 1 30); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$NAME")" = healthy ] && break
  sleep 2
done
[ "$(docker inspect -f '{{.State.Health.Status}}' "$NAME")" = healthy ] && ok "container reports healthy" || bad "never became healthy"

[ "$(docker exec "$NAME" id -u)" != 0 ] && ok "runs as a non-root user" || bad "runs as root"
docker exec "$NAME" sh -c 'touch /etc/x 2>/dev/null' && bad "root filesystem is writable" || ok "root filesystem is read-only"
docker exec "$NAME" sh -c 'touch /sources/x 2>/dev/null' && bad "source mount is writable" || ok "source mount is read-only"

# 3. The API needs the password.
[ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/status")" = 401 ] && ok "API refuses a request with no password" || bad "API open without a password"
curl -sI "http://127.0.0.1:$PORT/" | grep -qi "content-security-policy" && ok "security headers present" || bad "no CSP"

# 4. Nothing is copied until the iCloud folder is confirmed.
post /api/jobs '{"name":"photos","root":"sources","source":"Photos"}' >/dev/null
sleep 3
[ -z "$(ls -A "$TMP/icloud")" ] && ok "nothing written to iCloud before the one-time confirmation" || bad "wrote before confirmation"
post /api/icloud/confirm '{}' >/dev/null
post /api/jobs/photos/sync '{}' >/dev/null || true
for _ in $(seq 1 40); do
  r=$(get /api/status | jget "d['jobs'][0]['result']")
  [ "$r" = ok ] && break
  sleep 1
done
[ "$r" = ok ] && ok "first run copies and verifies (result ok)" || bad "first run result: $r"
[ "$(ls "$TMP/icloud/photos/2025" | wc -l | tr -d ' ')" = 10 ] && ok "10 files are in the iCloud folder" || bad "copy incomplete"
[ "$(ls "$TMP/sources/Photos/2025" | wc -l | tr -d ' ')" = 10 ] && ok "source untouched" || bad "source changed"

# 5. A vanished source (shrink guard): iCloud copy must survive.
for i in 1 2 3 4 5 6 7 8; do rm "$TMP/sources/Photos/2025/$i.jpg"; done
post /api/jobs/photos/sync '{}' >/dev/null
for _ in $(seq 1 40); do
  r=$(get /api/status | jget "d['jobs'][0]['result']+'|'+d['jobs'][0]['last_run']")
  [ "${r%%|*}" = failed ] && break
  sleep 1
done
[ "${r%%|*}" = failed ] && ok "shrink guard refuses the run" || bad "shrink guard did not fire: $r"
get /api/status | jget "d['jobs'][0]['error']" | grep -q "shrank" && ok "refusal is a plain sentence" || bad "no plain refusal"
[ "$(ls "$TMP/icloud/photos/2025" | wc -l | tr -d ' ')" = 10 ] && ok "the iCloud copy is intact after the refusal" || bad "iCloud copy changed"

# 6. Accepting the smaller source keeps what was deleted.
post /api/jobs/photos/sync '{"accept_shrink":true}' >/dev/null
for _ in $(seq 1 40); do
  r=$(get /api/status | jget "d['jobs'][0]['result']")
  [ "$r" = ok ] && break
  sleep 1
done
[ "$r" = ok ] && [ "$(ls "$TMP"/icloud/photos/.deleted/*/2025 | wc -l | tr -d ' ')" = 8 ] \
  && ok "accepted shrink moves 8 files to .deleted instead of deleting them" || bad "deleted files not kept ($r)"

# 7. Trust features: restore never writes the source, a check catches a same-size change.
code() { curl -s -o /tmp/smoke-body.$$ -w '%{http_code}' -H "Authorization: Bearer $PW" -X POST -H 'Content-Type: application/json' -d "$2" "http://127.0.0.1:$PORT$1"; }
DAY=$(get /api/jobs/photos/deleted | jget "d['entries'][0]['date']")
[ -n "$DAY" ] && ok "the deleted files list shows what was kept" || bad "no deleted files listed"
BODY=$(printf '{"date":"%s","path":"../../.keepsake-owner"}' "$DAY")
[ "$(code /api/jobs/photos/restore "$BODY")" = 400 ] && ok "restore refuses a path that climbs out" || bad "traversal not refused: $(cat /tmp/smoke-body.$$)"
FIRST=$(get /api/jobs/photos/deleted | jget "d['entries'][0]['path']")
BODY=$(printf '{"date":"%s","path":"%s"}' "$DAY" "$FIRST")
[ "$(code /api/jobs/photos/restore "$BODY")" = 200 ] && ok "restore answers ok" || bad "restore failed"
[ -f "$TMP/icloud/photos/.restored/$DAY/$FIRST" ] && ok "the file is restored inside the iCloud folder" || bad "restored file missing"
[ "$(ls "$TMP/sources/Photos/2025" | wc -l | tr -d ' ')" = 2 ] && ok "restore left the source untouched" || bad "source changed by restore"
[ "$(code /api/jobs/photos/verify '{}')" = 200 ] && ok "check a copy: all match" || bad "check failed on a good copy: $(cat /tmp/smoke-body.$$)"
docker exec "$NAME" sh -c 'printf "photo XX\n" > /icloud/photos/2025/10.jpg'
[ "$(code /api/jobs/photos/verify '{}')" = 409 ] && grep -q '10.jpg' /tmp/smoke-body.$$ && ok "check a copy: a same-size change is reported by name, not as a success" || bad "tampered copy not caught"
[ "$(get /api/status | jget "d['jobs'][0]['status']")" = failed ] && ok "the folder then needs attention" || bad "status not failed after a mismatch"
rm -f /tmp/smoke-body.$$

# 8. Preview next sync changes nothing; a pause is reported and can be undone.
snap() { docker exec "$NAME" sh -c 'find /icloud /data -exec stat -c "%n %F %s %Y" {} + | sort; find /icloud /data -type f -exec sha256sum {} + | sort'; }
printf 'brand new\n' > "$TMP/sources/Photos/2025/new.jpg"
BEFORE=$(snap)
post /api/jobs/photos/preview '{}' >/dev/null
for _ in $(seq 1 30); do
  s=$(get /api/jobs/photos/preview | jget "d['state']+'|'+str(d['preview'] is not None)")
  [ "$s" = "idle|True" ] && break
  sleep 1
done
[ "$s" = "idle|True" ] && ok "preview next sync runs on the worker and answers" || bad "preview did not answer: $s"
get /api/jobs/photos/preview | jget "d['preview']['label']+'|'+str(d['preview']['added'])+'|'+str(d['preview']['changed_nothing'])" | grep -q '^Preview|1|True$' \
  && ok "the preview is labelled and counts the one new file" || bad "preview content wrong: $(get /api/jobs/photos/preview)"
[ "$(snap)" = "$BEFORE" ] && ok "the preview changed nothing in the iCloud folder or the data folder (names, sizes, times, hashes)" || bad "a preview wrote something"
[ "$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $PW" -H 'Content-Type: application/json' -d '{"pause":{"preset":"1h"}}' "http://127.0.0.1:$PORT/api/settings")" = 200 ] \
  && [ "$(get /api/status | jget "d['pause']['paused']")" = True ] && ok "pause everything is reported by the status" || bad "pause not reported"
curl -fsS -X PUT -H "Authorization: Bearer $PW" -H 'Content-Type: application/json' -d '{"pause":null}' "http://127.0.0.1:$PORT/api/settings" >/dev/null
[ "$(get /api/status | jget "d['pause']['paused']")" = False ] && ok "resume now ends it" || bad "pause not ended"

# 9. Logs carry no secret.
docker logs "$NAME" 2>&1 | grep -q "$PW" && bad "password in the log" || ok "password is not in the log"

echo "docker smoke: $pass checks passed"
