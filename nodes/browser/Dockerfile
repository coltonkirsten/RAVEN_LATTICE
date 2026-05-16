# browser node — mirrors EDITH's container shape, adds Chromium + Xvfb +
# browser-use so a natural-language task can drive a headed Chromium agent
# loop inside a container. Same nexus-cell base as EDITH for parity.
FROM nexus-cell:latest

ENV DEBIAN_FRONTEND=noninteractive

USER root

WORKDIR /app

# Browser stack: Chromium itself (via Playwright's bundled binary later) plus
# the OS libs every headless Chromium needs, Xvfb for the "headed in a
# container" pattern (browser-use's docs recommend this when sites detect
# pure --headless), and font packages so rendered pages don't blow up on
# unicode/web fonts.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        chromium \
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
        wget \
        gnupg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# Install Playwright's Chromium build under /opt/pw-browsers so it's owned
# by root and shared by every container user (browser-use uses Playwright
# under the hood; without this it will try to download chromium at runtime
# as a non-root user with no HOME write permission).
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN python3 -m playwright install chromium --with-deps || \
    python3 -m playwright install chromium

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
