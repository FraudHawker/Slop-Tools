#!/bin/bash
set -e

URL="http://localhost:8085"
PASS=0
FAIL=0
ERRORS=""

pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); ERRORS="$ERRORS\n  - $1"; }

cleanup() {
    docker compose down 2>/dev/null || true
    rm -rf data/
    rm -f /tmp/iv_test.* /tmp/iv_result.json
}

trap cleanup EXIT

echo "=== image-verify smoke test ==="
echo ""

echo "[1/7] Fresh build..."
docker compose down -v 2>/dev/null || true
rm -rf data/
docker compose up -d --build 2>&1 | tail -1
sleep 4

STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$URL/")
[ "$STATUS" = "200" ] && pass "Homepage returns 200" || { fail "Homepage returned $STATUS"; docker compose logs; exit 1; }

echo "[2/7] Create sample files..."
python3 -c "
from PIL import Image
img = Image.new('RGB', (640, 480), 'red')
img.save('/tmp/iv_test.jpg', 'JPEG')
img.save('/tmp/iv_test.png', 'PNG')
"
echo "not an image" > /tmp/iv_test.txt
pass "Sample files created"

echo "[3/7] Upload flows..."
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -F "files=@/tmp/iv_test.jpg" "$URL/analyze" -L)
[ "$STATUS" = "200" ] && pass "JPEG upload/analyze" || fail "JPEG analyze returned $STATUS"

STATUS=$(curl -s -o /dev/null -w "%{http_code}" -F "files=@/tmp/iv_test.png" "$URL/analyze" -L)
[ "$STATUS" = "200" ] && pass "PNG upload/analyze" || fail "PNG analyze returned $STATUS"

STATUS=$(curl -s -o /dev/null -w "%{http_code}" -F "files=@/tmp/iv_test.txt" "$URL/analyze" -L)
[ "$STATUS" = "200" ] && pass "Invalid upload handled gracefully" || fail "Invalid upload returned $STATUS"

echo "[4/7] Result history and JSON..."
python3 - <<'PY'
import json, pathlib
history = pathlib.Path('data/history.json')
assert history.exists(), 'history.json missing'
items = json.loads(history.read_text())
assert len(items) >= 2, f'expected at least 2 history items, got {len(items)}'
print(items[0]['id'])
PY
ANALYSIS_ID=$(python3 - <<'PY'
import json, pathlib
items = json.loads(pathlib.Path('data/history.json').read_text())
print(items[0]['id'])
PY
)
pass "History written"

STATUS=$(curl -s -o /tmp/iv_result.json -w "%{http_code}" "$URL/result/$ANALYSIS_ID/json")
[ "$STATUS" = "200" ] && pass "Result JSON endpoint" || fail "Result JSON returned $STATUS"

python3 - <<'PY'
import json
with open('/tmp/iv_result.json') as f:
    data = json.load(f)
required = ['metadata', 'c2pa', 'thumbnail', 'ela', 'noise', 'jpeg_ghosts', 'reverse_search', 'verdict']
missing = [k for k in required if k not in data]
assert not missing, f'missing keys: {missing}'
assert data['verdict']['level'] in {'verified','tampered','suspicious','inconclusive','likely_authentic'}
PY
pass "Result JSON structure looks right"

echo "[5/7] Result pages..."
for endpoint in "/result/$ANALYSIS_ID" "/result/$ANALYSIS_ID/original"; do
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$URL$endpoint")
    [ "$STATUS" = "200" ] && pass "GET $endpoint" || fail "GET $endpoint returned $STATUS"
done

echo "[6/7] Generated artifacts..."
python3 - <<'PY'
import json, pathlib
items = json.loads(pathlib.Path('data/history.json').read_text())
analysis_id = items[0]['id']
result_dir = pathlib.Path('data/results') / analysis_id
assert result_dir.exists(), f'missing result dir {result_dir}'
assert (result_dir / 'results.json').exists(), 'missing results.json'
PY
pass "Result artifacts saved"

echo "[7/7] Clear endpoint..."
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$URL/clear")
[ "$STATUS" = "302" ] && pass "Clear endpoint" || fail "Clear returned $STATUS"

python3 - <<'PY'
import pathlib
history = pathlib.Path('data/history.json')
assert not history.exists(), 'history.json still exists after clear'
PY
pass "Clear removed history"

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
