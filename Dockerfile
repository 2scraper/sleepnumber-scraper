# Builds the Playwright engine (the one the README recommends) into a
# container.
#
#   docker build -t sleepnumber-scraper .
#   docker run --rm -v "$PWD/out:/out" sleepnumber-scraper \
#     --category mattresses --format both --out /out/mattresses \
#     --proxy "$SLEEPNUMBER_PROXY"
#
# Pass --proxy/--twocaptcha-key the same way as running locally, or mount a
# .env at /app/.env — nothing here bakes in a credential, and .dockerignore
# keeps one out of the build context. A .env baked into an image is a
# credential published to everyone who can pull it.
#
# YOU WILL ALMOST CERTAINLY NEED --proxy. Sleep Number answers an identical
# CloudFront 403 to every request from a datacentre address, and a container
# on a cloud host is one. See the README's "Access".
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements-playwright.txt ./

# `--with-deps` pulls Chromium's shared-library dependencies through apt;
# they are not pip packages and cannot ride in requirements.txt.
RUN pip install --no-cache-dir -r requirements.txt -r requirements-playwright.txt \
    && playwright install --with-deps chromium

# HEADLESS, and that is measured rather than assumed.
#
# Some sites in this family refuse a headless browser outright and their
# images need Xvfb and a headful Chromium to work at all. This one does not:
# a headless Playwright run through a US residential exit returned HTTP 200
# and the full 52-row mattresses catalogue on 2026-09-17, the same as an
# HTTP client through the same exit. So there is no virtual display here and
# no `xvfb-run` wrapper, because adding them would be several hundred
# megabytes and a second failure mode bought for a problem this site does
# not have.
#
# If that ever changes, the symptom is exit 3 on every invocation while
# `--help` still works — and the fix is xvfb + xauth + a headful default,
# not a longer timeout.

# Every module playwright_scraper.py imports, transitively, plus diff_runs.py
# as a useful companion in the same image. smoke_test.py checks this list
# against the entrypoint's real import graph by walking its imports: three
# repos in this family shipped an image missing a module the engine imports
# at module level, so it died with ModuleNotFoundError on every invocation
# INCLUDING `--help` — a broken container that nothing in the repo noticed,
# because nothing built it.
COPY captcha_solver.py env_config.py fingerprint_client.py output_writer.py \
     page_flow.py playwright_scraper.py product_parser.py proxy_pool.py \
     scraper_api_client.py diff_runs.py ./

# No test suite, no fixtures, no .github/ and no .env — see .dockerignore.
ENTRYPOINT ["python", "playwright_scraper.py"]
CMD ["--help"]
