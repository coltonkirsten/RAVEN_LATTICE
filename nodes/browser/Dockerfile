# browser node — mirrors EDITH's container shape, adds Chromium runtime
# libs + Xvfb so the spawned Claude CLI's Playwright MCP server can drive
# a real headed Chromium inside the container. Same nexus-cell base as
# EDITH for parity (already ships node 22 + python3 + claude CLI).
FROM nexus-cell:latest

ENV DEBIAN_FRONTEND=noninteractive

USER root

WORKDIR /app

# OS-level shared libs Chromium needs at runtime. We do NOT install the
# chromium binary itself — Playwright MCP (started via `npx
# @playwright/mcp@latest`) brings its own pinned Chromium under
# /opt/pw-browsers and that's what actually runs.
#
# arm64 bookworm has been seeing intermittent Hash Sum mismatch on the
# default deb.debian.org mirror; HTTPS + by-hash off + retries clears it.
RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries \
    && echo 'Acquire::By-Hash "no";' >> /etc/apt/apt.conf.d/80-retries \
    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/*.sources 2>/dev/null || true \
    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list 2>/dev/null || true \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        fonts-liberation \
        fonts-noto-color-emoji \
        libnss3 \
        libxss1 \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcups2 \
        libdrm2 \
        libgbm1 \
        libgtk-3-0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libxkbcommon0 \
        libpango-1.0-0 \
        libcairo2 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Tiny pip surface (just aiohttp + pyyaml for the node loop) — keeps the
# build robust on Docker Desktop's flaky-IPv6 path that bites bigger pip
# installs with SSL "DECRYPTION_FAILED_OR_BAD_RECORD_MAC". Force IPv4
# resolution as belt-and-suspenders.
RUN echo 'precedence ::ffff:0:0/96 100' >> /etc/gai.conf \
    && pip3 install --no-cache-dir --break-system-packages \
        --retries 10 --timeout 120 \
        -r requirements.txt

# Playwright's Chromium is downloaded at first runtime (~185MB). We do
# NOT pre-install it during the build because Docker Desktop on macOS
# arm64 has a flaky IPv6/MTU path that triggers
# `ERR_SSL_DECRYPTION_FAILED_OR_BAD_RECORD_MAC` mid-stream on the
# cdn.playwright.dev download — same failure mode that bit the earlier
# browser-use pip install. The Playwright MCP server invoked by the
# spawned Claude CLI will fetch Chromium on first use inside the running
# container, which uses a different network path (NAT'd bridge) that is
# generally fine. The download lands under PLAYWRIGHT_BROWSERS_PATH so a
# named volume can persist it across rebuilds.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN mkdir -p /opt/pw-browsers && chmod 0777 /opt/pw-browsers

COPY browser_node.py .
COPY mesh_mcp.py .
COPY entrypoint.sh /usr/local/bin/browser-entrypoint.sh
RUN chmod +x /usr/local/bin/browser-entrypoint.sh

# Dedicated browser user (parity with EDITH's pattern). HOME writable so
# Chromium's profile/cache dirs are happy.
RUN useradd -m -u 1501 browser && chown -R browser /app
USER browser

# Process-alive healthcheck — same approach as EDITH (nexus-cell base has
# no procps, probe /proc/1/cmdline directly).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD grep -q browser_node /proc/1/cmdline || exit 1

ENTRYPOINT ["/usr/local/bin/browser-entrypoint.sh"]
CMD ["python3", "-u", "browser_node.py"]
