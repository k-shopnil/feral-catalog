#!/usr/bin/env bash
# Phase 1 gate (plan §4.5.6): prove a GitHub-hosted runner can reach the BDIX storage server
# and TMDB, and measure listing / HEAD / ranged-GET / ffprobe timings.
# Writes a Markdown report to stdout. Never prints secrets.
set -uo pipefail

BASE="https://movie.ctgfun.com/"
UA="Feral-Indexer/1.0 (reachability test)"
# Known file from recon (2026-11-25); a ~1.8 GB MP4 with a sidecar .srt.
SAMPLE_DIR="disk1/English%20Movies/2026/Zip%20Wire%20(2026)%20Hindi%20English%201080p%20WEBRip%20x264%20ESub%20%5bDDN%5d/"
SAMPLE_FILE="Zip%20Wire%20(2026)%20Hindi%20English%201080p%20WEBRip%20x264%20ESub%20%5bDDN%5d.mp4"
BIG_LISTING="disk4/South%20Indian%20Movies/"

fail=0
row() { echo "| $1 | $2 | $3 |"; }

echo "## feral-catalog reachability report"
echo
echo "- Runner: \`$(uname -sr)\`, public IP country: \`$(curl -s -m 10 https://ipinfo.io/country || echo unknown)\`"
echo "- Time (UTC): $(date -u '+%Y-%m-%d %H:%M:%S')"
echo
echo "| Check | Result | Detail |"
echo "|---|---|---|"

# 1. Root listing
out=$(curl -sS -m 30 -A "$UA" -o /tmp/root.html -w '%{http_code} %{time_starttransfer} %{time_total}' "$BASE" 2>&1) || true
code=${out%% *}
disks=$(grep -oE 'href="disk[0-9]+/"' /tmp/root.html 2>/dev/null | wc -l)
if [[ "$code" == "200" && "$disks" -ge 5 ]]; then row "Root listing" "✅" "HTTP $code, $disks disks, ttfb/total ${out#* }s"; else row "Root listing" "❌" "$out"; fail=1; fi

# 2. Large listing
out=$(curl -sS -m 60 -A "$UA" -o /tmp/big.html -w '%{http_code} %{size_download} %{time_total}' "$BASE$BIG_LISTING" 2>&1) || true
entries=$(grep -c '<td valign="top"><img' /tmp/big.html 2>/dev/null || echo 0)
if [[ "${out%% *}" == "200" ]]; then row "Large listing (South Indian Movies)" "✅" "bytes/time: ${out#* }s, ~$entries rows"; else row "Large listing" "❌" "$out"; fail=1; fi

# 3. HEAD on a video
hdr=$(curl -sS -m 30 -A "$UA" -I "$BASE$SAMPLE_DIR$SAMPLE_FILE" 2>&1 | tr -d '\r') || true
len=$(echo "$hdr" | awk -F': ' 'tolower($1)=="content-length"{print $2}')
ar=$(echo "$hdr" | awk -F': ' 'tolower($1)=="accept-ranges"{print $2}')
if echo "$hdr" | head -1 | grep -q ' 200'; then row "HEAD video" "✅" "Content-Length $len, Accept-Ranges $ar"; else row "HEAD video" "❌" "$(echo "$hdr" | head -1)"; fail=1; fi

# 4. Ranged GET 4 MiB (throughput from the runner)
out=$(curl -sS -m 60 -A "$UA" -r 0-4194303 -o /dev/null -w '%{http_code} %{size_download} %{speed_download} %{time_total}' "$BASE$SAMPLE_DIR$SAMPLE_FILE" 2>&1) || true
read -r c sz sp tt <<<"$out"
if [[ "$c" == "206" ]]; then row "Ranged GET 4 MiB" "✅" "HTTP 206, $(awk -v s="$sp" 'BEGIN{printf "%.2f", s*8/1e6}') Mbps, ${tt}s"; else row "Ranged GET" "❌" "$out"; fail=1; fi

# 5. ffprobe over HTTP (header-only read)
start=$(date +%s.%N)
probe=$(ffprobe -v error -user_agent "$UA" -probesize 2M -analyzeduration 3M -rw_timeout 20000000 \
  -print_format json -show_format -show_streams "$BASE$SAMPLE_DIR$SAMPLE_FILE" 2>&1) || true
end=$(date +%s.%N)
secs=$(awk -v a="$start" -v b="$end" 'BEGIN{printf "%.1f", b-a}')
if echo "$probe" | jq -e '.format.duration' >/dev/null 2>&1; then
  summary=$(echo "$probe" | jq -r '[.streams[] | "\(.codec_type):\(.codec_name)\(if .tags.language then "(" + .tags.language + ")" else "" end)"] | join(", ")')
  dur=$(echo "$probe" | jq -r '.format.duration | tonumber | floor')
  row "ffprobe over HTTP" "✅" "${secs}s, duration ${dur}s, streams: $summary"
else
  row "ffprobe over HTTP" "❌" "${secs}s: $(echo "$probe" | head -c 200)"; fail=1
fi

# 6. Sidecar subtitle
out=$(curl -sS -m 30 -A "$UA" -o /tmp/sub.srt -w '%{http_code} %{size_download}' "$BASE$SAMPLE_DIR${SAMPLE_FILE%.mp4}.srt" 2>&1) || true
if [[ "${out%% *}" == "200" ]]; then row "Sidecar .srt" "✅" "bytes ${out#* }, charset: $(file -bi /tmp/sub.srt | sed 's/.*charset=//')"; else row "Sidecar .srt" "❌" "$out"; fail=1; fi

# 7. TMDB token (secret is masked by Actions; never echoed)
if [[ -n "${TMDB_READ_ACCESS:-}" ]]; then
  tm=$(curl -sS -m 20 -H "Authorization: Bearer $TMDB_READ_ACCESS" -H 'accept: application/json' https://api.themoviedb.org/3/authentication | jq -r '.success' 2>/dev/null)
  if [[ "$tm" == "true" ]]; then row "TMDB token" "✅" "authentication ok"; else row "TMDB token" "❌" "authentication failed"; fail=1; fi
else
  row "TMDB token" "⚠️" "TMDB_READ_ACCESS not set"
  fail=1
fi

echo
if [[ $fail -eq 0 ]]; then echo "**RESULT: PASS** — serverless mode (plan §4.5) is viable."; else echo "**RESULT: FAIL** — see rows marked ❌; fallback is PC-scheduled runs (plan §4.5.6)."; fi
exit $fail
