#!/usr/bin/env bash
# ElderCare AI smoke teszt.
#
# A test/scenarios/*.json fájlok options.json fixture-ök — pontosan az a formátum,
# amit a Supervisor ír a /data/options.json-be. Ez a szkript ezekkel indítja a
# konténert, és ellenőrzi, hogy elindul és a healthcheck zöld.
#
# Használat:  bash test/smoke.sh [scenario ...]
set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE="local/eldercare-smoke"
BASE="${BUILD_FROM:-ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.21}"
SCENARIOS=("$@")
[ ${#SCENARIOS[@]} -eq 0 ] && SCENARIOS=(minimal offline full)

echo "==> Build ($BASE)"
docker build -q -t "$IMAGE" --build-arg BUILD_FROM="$BASE" --build-arg BUILD_VERSION=smoke . >/dev/null

FAILED=0
for scenario in "${SCENARIOS[@]}"; do
  file="test/scenarios/${scenario}.json"
  [ -f "$file" ] || { echo "!! nincs ilyen scenario: $file"; FAILED=1; continue; }

  echo "==> Scenario: $scenario"
  data=$(mktemp -d)
  cp "$file" "$data/options.json"
  name="eldercare-smoke-$scenario"

  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" -v "$data:/data" -p 8099:8099 "$IMAGE" >/dev/null

  # A /health a konténeren belülről (loopback) érhető el — az Ingress IP-szűrő
  # kivételt tesz rá. Az /api/status kívülről szándékosan 403.
  ok=0
  for _ in $(seq 1 30); do
    sleep 1
    if docker exec "$name" python3 -c \
        "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8099/health',timeout=2)" \
        >/dev/null 2>&1; then
      ok=1; break
    fi
  done

  if [ "$ok" -eq 1 ]; then
    echo "   OK — elindult, a helyi UI válaszol"
  else
    echo "   HIBA — nem indult el. Napló:"
    docker logs --tail 30 "$name" 2>&1 | sed 's/^/     /'
    FAILED=1
  fi

  docker rm -f "$name" >/dev/null 2>&1 || true
  rm -rf "$data"
done

[ "$FAILED" -eq 0 ] && echo "==> Minden scenario rendben." || echo "==> HIBÁVAL zárult."
exit "$FAILED"
