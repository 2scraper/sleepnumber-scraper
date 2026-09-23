#!/usr/bin/env bash
# access_probe.sh — measure whether www.sleepnumber.com will serve THIS exit.
#
# Why this ships in the repo rather than living in a notebook: the README's
# central claim is a measurement — "every URL answers an identical 919-byte
# CloudFront 403 from a datacentre address, and 200 from a residential one" —
# and a number describing a living thing has to arrive with the command that
# reproduces it or not be written down at all (CLAUDE.md §13).
#
# It is also the first thing to run when the scraper suddenly returns exit 3:
# the question is almost always "is this address still being served", and
# that is one request rather than a debugging session.
#
#   ./tools/waf_probe.sh                 # 10 requests to a category
#   ./tools/waf_probe.sh 20              # 20 requests
#   ./tools/waf_probe.sh 10 product      # a different page kind
#   ./tools/waf_probe.sh 10 listing --direct   # ignore any proxy
#
# Page kinds: listing | collection | product | robots
#
# THE EXIT IT MEASURES
# --------------------
# By default this goes through SLEEPNUMBER_PROXY when .env sets one, because
# "is this address served" is a question about an EXIT, and measuring the
# machine you happen to be sitting at answers it for one exit only. With no
# proxy configured, or with --direct, it measures this machine.
#
# The proxy is resolved by env_config.py, the same loader every engine uses,
# so precedence is the documented one and only one place decides it. It
# reaches curl through `https_proxy`/`http_proxy` in the ENVIRONMENT — never
# a command-line flag, because `ps` reads argv. Only the masked host and port
# are ever printed.
#
# READING THE OUTPUT
# ------------------
# A page Sleep Number actually served answers `server: Fly/…` with
# `x-cache: Miss from cloudfront` and is a few hundred KB. A refusal answers
# `server: CloudFront` with `x-cache: Error from cloudfront` and is 919
# bytes: CloudFront generated it at the edge and the request never reached
# Sleep Number at all.
#
# There is no captcha either way. The refusal carries no `x-amzn-waf-action`
# header and no widget, so no solver can help — what is needed is a
# different exit. If `x-amzn-waf-action: captcha` ever DOES appear, that is a
# different and solvable situation, and worth reporting as a change.

set -u

N="${1:-10}"
MODE="${2:-listing}"
DIRECT="${3:-}"

case "$MODE" in
  listing)    URL="https://www.sleepnumber.com/categories/mattresses" ;;
  collection) URL="https://www.sleepnumber.com/collections/mattresses-climate-collection" ;;
  product)    URL="https://www.sleepnumber.com/products/cm-mattress" ;;
  robots)     URL="https://www.sleepnumber.com/robots.txt" ;;
  *) echo "unknown page kind: $MODE (expected listing|collection|product|robots)"; exit 2 ;;
esac

UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"

# --- which exit --------------------------------------------------------------
# The python below prints two shell assignments and nothing else: a MASKED
# label, safe to show, and the export lines carrying the real URL. `eval`
# consumes the second, so the credential never reaches argv, a log or the
# terminal. proxy_pool.mask() is the repo's own masker and is documented
# never to raise -- it is the last thing between a password and a log.
PROXY_LABEL="direct (this machine)"
if [ "$DIRECT" != "--direct" ]; then
  eval "$(cd "$(dirname "$0")/.." && python3 - <<'ENVPY'
import shlex
import env_config
import proxy_pool

env_config.load_env()
url = env_config.env_value("SLEEPNUMBER_PROXY")
if url:
    print("PROXY_LABEL=%s" % shlex.quote(proxy_pool.mask(url)))
    for name in ("https_proxy", "http_proxy", "HTTPS_PROXY", "HTTP_PROXY"):
        print("export %s=%s" % (name, shlex.quote(url)))
else:
    print("PROXY_LABEL=%s" % shlex.quote("direct (no SLEEPNUMBER_PROXY in .env)"))
ENVPY
)"
fi
HDR="$(mktemp)"; BODY="$(mktemp)"
trap 'rm -f "$HDR" "$BODY"' EXIT

echo "URL:  $URL"
echo "exit: $PROXY_LABEL"
echo "when: $(date -u '+%Y-%m-%d %H:%M:%SZ')   requests: $N"
echo

challenged=0
unreachable=0
served=0
for i in $(seq 1 "$N"); do
  # -L matters: a URL that answers 301 (the bare sleepnumber.com host does,
  # to www.) would otherwise be counted as a clean response.
  code=$(curl -sSL -o "$BODY" -D "$HDR" -w "%{http_code}" "$URL" \
           -H "User-Agent: $UA" --max-time 40 2>/dev/null)
  srv=$(grep -i '^server:' "$HDR" | tr -d '\r' | tail -1 | awk '{print $2}')
  waf=$(grep -i '^x-amzn-waf-action:' "$HDR" | tr -d '\r' | tail -1 | awk '{print $2}')
  title=$(grep -o -i '<title>[^<]*</title>' "$BODY" | head -1 \
            | sed 's/<[^>]*>//g' | cut -c1-46)
  [ -n "${waf:-}" ] && challenged=$((challenged + 1))
  # curl reports 000 when it never got an HTTP response at all -- a dead or
  # unauthenticated proxy, a DNS failure, a timeout. Counted SEPARATELY:
  # without this, ten failed connections and ten clean pages both print
  # "challenged: 0 of 10", which is the one thing this probe must never do.
  if [ "$code" = "000" ]; then
    unreachable=$((unreachable + 1))
  else
    served=$((served + 1))
  fi
  printf "%3d  http=%-4s server=%-11s waf=%-8s %s\n" \
    "$i" "$code" "${srv:-?}" "${waf:-none}" "$title"
  sleep 2
done

echo
echo "exit:        $PROXY_LABEL"
echo "requests:    $N"
echo "got an HTTP response: $served"
echo "never connected:      $unreachable"
echo "challenged:  $challenged of $served answered"
echo
if [ "$unreachable" -eq "$N" ]; then
  echo "INCONCLUSIVE: not one request reached the site, so this says nothing"
  echo "about the WAF. Every line above is a transport failure -- check the"
  echo "exit itself before reading anything into a challenge count of zero."
elif [ "$unreachable" -gt 0 ]; then
  echo "PARTIAL: $unreachable of $N never connected. The challenge rate below"
  echo "is over the $served that did answer; the rest measured nothing."
fi
if [ "$challenged" -gt 0 ]; then
  echo "This exit meets the WAF. A proxy or the Scraping Browser API is what"
  echo "gets the page from here; TWOCAPTCHA_KEY is what solves the challenge"
  echo "when one still appears (task type AmazonTaskProxyless)."
elif [ "$served" -gt 0 ]; then
  echo "This exit was not challenged in $served answered request(s). That is a"
  echo "fact about this address today, not about the site: on 2026-09-16 the"
  echo "same URL from one datacentre address returned 200 and 405 interleaved."
  echo "Run it again, and run at least 10 before concluding anything."
fi
