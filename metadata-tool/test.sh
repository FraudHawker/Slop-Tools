#!/bin/bash
# Production readiness test for metadata-tool
# Simulates a fresh clone → docker compose up → use every feature

set -e

URL="http://localhost:8080"
PASS=0
FAIL=0
ERRORS=""

pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); ERRORS="$ERRORS\n  - $1"; }

cleanup() {
    docker compose down 2>/dev/null || true
    rm -rf data/
    rm -f /tmp/mt_test.* /tmp/mt_clean.* /tmp/mt_rand.* /tmp/mt_export.*
}

trap cleanup EXIT

echo "=== metadata-tool production test ==="
echo ""

# ── 1. Fresh build ──
echo "[1/10] Fresh build from clean state..."
docker compose down -v 2>/dev/null || true
sleep 1
rm -rf data/
docker compose up -d --build 2>&1 | tail -1
sleep 4

# Check container is running
if curl -s -o /dev/null -w "%{http_code}" "$URL/" 2>/dev/null | grep -q "200"; then
    pass "Container is running and responding"
else
    fail "Container not responding"
    docker compose logs
    exit 1
fi

# ── 2. Homepage loads ──
echo "[2/10] Homepage..."
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$URL/")
[ "$STATUS" = "200" ] && pass "Homepage returns 200" || fail "Homepage returned $STATUS"

# Check it shows 0 files (clean state)
curl -s "$URL/api/stats" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['total_files'] == 0, f'Expected 0 files, got {d[\"total_files\"]}'
" && pass "Clean state: 0 files" || fail "Clean state check"

# ── 3. Upload various file types ──
echo "[3/10] File uploads..."

# Create test files
python3 -c "
from PIL import Image
img = Image.new('RGB', (300, 200), 'red')
img.save('/tmp/mt_test.jpg', 'JPEG')
img.save('/tmp/mt_test.png', 'PNG')
"
echo "test content" > /tmp/mt_test.txt

# Upload JPEG
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -F "files=@/tmp/mt_test.jpg" "$URL/upload" -L)
[ "$STATUS" = "200" ] && pass "JPEG upload" || fail "JPEG upload returned $STATUS"

# Upload PNG
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -F "files=@/tmp/mt_test.png" "$URL/upload" -L)
[ "$STATUS" = "200" ] && pass "PNG upload" || fail "PNG upload returned $STATUS"

# Upload text
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -F "files=@/tmp/mt_test.txt" "$URL/upload" -L)
[ "$STATUS" = "200" ] && pass "TXT upload" || fail "TXT upload returned $STATUS"

# Bulk upload
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -F "files=@/tmp/mt_test.jpg" -F "files=@/tmp/mt_test.png" "$URL/upload" -L)
[ "$STATUS" = "200" ] && pass "Bulk upload (2 files)" || fail "Bulk upload"

# Verify count
COUNT=$(curl -s "$URL/api/stats" | python3 -c "import sys,json; print(json.load(sys.stdin)['total_files'])")
[ "$COUNT" = "5" ] && pass "File count correct ($COUNT)" || fail "Expected 5 files, got $COUNT"

# ── 4. API endpoints ──
echo "[4/10] API endpoints..."

for endpoint in "/api/stats" "/api/files" "/api/gps"; do
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$URL$endpoint")
    [ "$STATUS" = "200" ] && pass "GET $endpoint" || fail "GET $endpoint returned $STATUS"
done

# Get first JPEG file ID (randomize needs image files)
FILE_ID=$(curl -s "$URL/api/files" | python3 -c "
import sys,json
files = json.load(sys.stdin)['files']
jpeg = [f for f in files if f['mime_type'] and 'jpeg' in f['mime_type']]
print(jpeg[0]['id'] if jpeg else files[0]['id'])
")

STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$URL/api/file/$FILE_ID")
[ "$STATUS" = "200" ] && pass "GET /api/file/$FILE_ID" || fail "GET /api/file/$FILE_ID"

# ── 5. File detail page ──
echo "[5/10] File detail page..."
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$URL/file/$FILE_ID")
[ "$STATUS" = "200" ] && pass "Detail page renders" || fail "Detail page"

# Check metadata fields were extracted
FIELDS=$(curl -s "$URL/api/file/$FILE_ID" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['fields']))")
[ "$FIELDS" -gt "0" ] && pass "Metadata extracted ($FIELDS fields)" || fail "No metadata fields extracted"

# ── 6. Downloads ──
echo "[6/10] Download features..."

# Clean copy
STATUS=$(curl -s -o /tmp/mt_clean.jpg -w "%{http_code}" "$URL/file/$FILE_ID/strip")
[ "$STATUS" = "200" ] && pass "Download clean copy" || fail "Clean copy download returned $STATUS"

# Randomized copy
STATUS=$(curl -s -o /tmp/mt_rand.jpg -w "%{http_code}" "$URL/file/$FILE_ID/randomize")
[ "$STATUS" = "200" ] && pass "Download randomized copy" || fail "Randomized copy download returned $STATUS"

# Verify randomized has fake EXIF (check inside container)
MAKE=$(docker compose exec -T metadata-tool bash -lc 'exiftool -Make /app/data/randomized/* 2>/dev/null | head -1')
if echo "$MAKE" | grep -qE "Canon|Nikon|Sony|Fujifilm|Panasonic|Olympus|Leica|Pentax|Samsung|Hasselblad|Ricoh|Sigma"; then
    pass "Randomized metadata has plausible camera make"
else
    fail "Randomized metadata missing camera make: $MAKE"
fi

# ── 7. Bulk exports ──
echo "[7/10] Bulk exports..."

STATUS=$(curl -s -o /tmp/mt_export.csv -w "%{http_code}" "$URL/export/csv")
[ "$STATUS" = "200" ] && pass "CSV export" || fail "CSV export returned $STATUS"

STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$URL/export/json")
[ "$STATUS" = "200" ] && pass "JSON export" || fail "JSON export returned $STATUS"

STATUS=$(curl -s -o /tmp/mt_clean.zip -w "%{http_code}" "$URL/export/clean")
[ "$STATUS" = "200" ] && pass "Bulk clean export (zip)" || fail "Bulk clean export returned $STATUS"

STATUS=$(curl -s -o /tmp/mt_rand.zip -w "%{http_code}" "$URL/export/randomized")
[ "$STATUS" = "200" ] && pass "Bulk randomized export (zip)" || fail "Bulk randomized export returned $STATUS"

# ── 8. Pages ──
echo "[8/10] All pages load..."

for page in "/" "/map" "/settings"; do
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$URL$page")
    [ "$STATUS" = "200" ] && pass "GET $page" || fail "GET $page returned $STATUS"
done

# ── 9. Settings ──
echo "[9/10] Settings..."

# Save categories (disable timestamps) — 302 redirect = success
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -d "action=save_categories&categories=gps&categories=identity&categories=device&categories=content&categories=paths&categories=tracking_ids" "$URL/settings")
[ "$STATUS" = "302" ] && pass "Save PII categories" || fail "Save categories returned $STATUS"

# Add allowlist value
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -d "action=add_allowlist&value=TestValue123" "$URL/settings")
[ "$STATUS" = "302" ] && pass "Add allowlist value" || fail "Add allowlist returned $STATUS"

# Remove allowlist value
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -d "action=remove_allowlist&value=TestValue123" "$URL/settings")
[ "$STATUS" = "302" ] && pass "Remove allowlist value" || fail "Remove allowlist returned $STATUS"

# ── 10. Filters and wipe ──
echo "[10/10] Filters and wipe..."

STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$URL/?pii=1")
[ "$STATUS" = "200" ] && pass "PII filter" || fail "PII filter"

STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$URL/?gps=1")
[ "$STATUS" = "200" ] && pass "GPS filter" || fail "GPS filter"

STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$URL/?q=test")
[ "$STATUS" = "200" ] && pass "Search filter" || fail "Search filter"

# Wipe — 302 redirect = success
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -d "" "$URL/wipe")
[ "$STATUS" = "302" ] && pass "Wipe all files" || fail "Wipe returned $STATUS"

COUNT=$(curl -s "$URL/api/stats" | python3 -c "import sys,json; print(json.load(sys.stdin)['total_files'])")
[ "$COUNT" = "0" ] && pass "Wipe confirmed: 0 files" || fail "After wipe expected 0, got $COUNT"

# ── Results ──
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
