#!/bin/bash
set -e

URL="http://localhost:8077"
PASS=0
FAIL=0
ERRORS=""

pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); ERRORS="$ERRORS\n  - $1"; }

cleanup() {
    docker compose down 2>/dev/null || true
    rm -rf data/ models/
}

trap cleanup EXIT

echo "=== yt-subtitler smoke test ==="
echo ""

echo "[1/6] Fresh build..."
docker compose down -v 2>/dev/null || true
rm -rf data/ models/
mkdir -p data
cat > data/jobs.json <<'JSON'
[
  {
    "id": "restartjob01",
    "url": "https://youtu.be/example",
    "start": 0,
    "end": 30,
    "model_size": "small",
    "task": "translate",
    "source": "auto",
    "status": "downloading",
    "message": "fetching clip from YouTube",
    "error": null,
    "files": {},
    "created_at": 1710000000,
    "completed_at": null
  }
]
JSON
docker compose up -d --build 2>&1 | tail -1
sleep 4

STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$URL/")
[ "$STATUS" = "200" ] && pass "Homepage returns 200" || { fail "Homepage returned $STATUS"; docker compose logs; exit 1; }

python3 - <<'PY'
import json, urllib.request
job = json.load(urllib.request.urlopen("http://localhost:8077/api/jobs/restartjob01"))
assert job["status"] == "error"
assert job["message"] == "interrupted by restart"
print("ok")
PY
pass "Interrupted job is restored as restart error"

echo "[2/6] Health + settings..."
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$URL/api/health")
[ "$STATUS" = "200" ] && pass "Health endpoint" || fail "Health returned $STATUS"

python3 - <<'PY'
import json, urllib.request
data = json.load(urllib.request.urlopen("http://localhost:8077/api/settings"))
assert "llm_base_url" in data
assert "llm_api_key" in data
print("ok")
PY
pass "Settings endpoint"

echo "[3/6] Save settings..."
STATUS=$(curl -s -o /tmp/yt_sub_save.json -w "%{http_code}" \
  -H "Content-Type: application/json" \
  -d '{"llm_base_url":"http://127.0.0.1:5555/v1","llm_model":"demo-model","llm_api_key":"demo-key"}' \
  "$URL/api/settings")
[ "$STATUS" = "200" ] && pass "Save settings endpoint" || fail "Save settings returned $STATUS"

python3 - <<'PY'
import json, pathlib
saved = json.loads(pathlib.Path("data/settings.json").read_text())
assert saved["llm_base_url"] == "http://127.0.0.1:5555/v1"
assert saved["llm_model"] == "demo-model"
assert saved["llm_api_key"] == "demo-key"
PY
pass "Settings persisted"

echo "[4/6] Test settings should not persist..."
STATUS=$(curl -s -o /tmp/yt_sub_test.json -w "%{http_code}" \
  -H "Content-Type: application/json" \
  -d '{"llm_base_url":"http://127.0.0.1:9/v1","llm_model":"temp-model","llm_api_key":"temp-key"}' \
  "$URL/api/settings/test")
[ "$STATUS" = "200" ] && pass "Test settings endpoint" || fail "Test settings returned $STATUS"

python3 - <<'PY'
import json, pathlib
saved = json.loads(pathlib.Path("data/settings.json").read_text())
assert saved["llm_base_url"] == "http://127.0.0.1:5555/v1"
assert saved["llm_model"] == "demo-model"
assert saved["llm_api_key"] == "demo-key"
PY
pass "Test settings did not overwrite saved config"

echo "[5/6] Validation errors..."
STATUS=$(curl -s -o /tmp/yt_sub_bad.json -w "%{http_code}" \
  -H "Content-Type: application/json" \
  -d '{"url":"","start":"0:00","end":"0:30","model_size":"small","task":"translate","source":"auto"}' \
  "$URL/api/clip")
[ "$STATUS" = "400" ] && pass "Reject missing URL" || fail "Missing URL returned $STATUS"

STATUS=$(curl -s -o /tmp/yt_sub_bad2.json -w "%{http_code}" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://youtu.be/example","start":"0:30","end":"0:10","model_size":"small","task":"translate","source":"auto"}' \
  "$URL/api/clip")
[ "$STATUS" = "400" ] && pass "Reject inverted range" || fail "Inverted range returned $STATUS"

echo "[6/6] Clean runtime dirs..."
test -d data && pass "Data dir created" || fail "Data dir missing"
test -d models && pass "Model cache dir created" || fail "Model cache dir missing"

echo ""
echo "=================================="
echo "  PASSED: $PASS"
echo "  FAILED: $FAIL"
if [ $FAIL -gt 0 ]; then
    echo ""
    echo "  Failures:$ERRORS"
fi
echo "=================================="

exit $FAIL
