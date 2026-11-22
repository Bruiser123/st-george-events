#!/usr/bin/env bash
# Scheduled publishing helper for a parish-controlled HTTPS host.
# Writes ONLY the single local public file passed as its first argument.
set -euo pipefail
if (( $# < 1 )) || [[ "$1" != /* || ! -d "$(dirname -- "$1")" ]]; then
  printf '%s\n' 'Usage: refresh_web.sh /absolute/existing/web-directory/events.html [builder flags]' >&2
  exit 2
fi
target=$1
shift
for arg in "$@"; do
  case "$arg" in
    --output|--output=*|--format|--format=*) printf '%s\n' 'Use the publish path to select the HTML output.' >&2; exit 2;;
  esac
done
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
tmp=$(mktemp -d -- "$(dirname -- "$target")/.stg-events.XXXXXXXX")
trap 'rm -f -- "$tmp/fragment.html" "$tmp/events.html"; rmdir -- "$tmp"' EXIT
"${PYTHON:-python3}" "$root/build_events.py" "$@" --format html --output "$tmp/fragment.html"
{
  printf '%s\n' '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><base target="_blank"><title>St. George News &amp; Events</title>'
  printf '<meta name="refresh-epoch" content="%s">\n' "$(date +%s)"
  cat <<'EOF'
<style>
body{margin:0;padding:1rem;color:#362b28;background:#fffcf6;font:1rem/1.55 Georgia,serif}
h2,h3,a{color:#672d39} li{margin-bottom:.85rem} a:focus-visible{outline:2px solid #672d39}
</style>
<body>
EOF
  cat -- "$tmp/fragment.html"
  cat <<'EOF'
<p id="stg-feed-fallback" hidden>Listings could not be refreshed. Please check the <a href="https://www.saintgeorgeupperdarby.org/calendar.html" target="_blank" rel="noopener">parish calendar</a>.</p>
<script>
const refreshed = Number(document.querySelector('meta[name="refresh-epoch"]').content);
if (Date.now() / 1000 - refreshed > 72 * 60 * 60) {
  document.querySelector('.stg-news-events').hidden = true;
  document.getElementById('stg-feed-fallback').hidden = false;
}
</script>
</body></html>
EOF
} > "$tmp/events.html"
chmod 0644 -- "$tmp/events.html"
mv -f -- "$tmp/events.html" "$target"
