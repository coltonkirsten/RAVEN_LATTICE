# Base on the NEXUS cell image — it already has node 22, python3, and
# the Claude Code CLI globally installed. EDITH uses the CLI directly
# instead of the raw Anthropic SDK so its traffic looks like normal
# Claude Code usage to Anthropic's edge (avoids anti-abuse 429s on
# raw-curl-shaped requests).
FROM nexus-cell:latest

ENV DEBIAN_FRONTEND=noninteractive

# Drop back to root for installs (nexus-cell entrypoint normally drops
# to "agent" via gosu — we override here since EDITH has its own user).
USER root

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY edith.py .
COPY mesh_mcp.py .

# Run as a dedicated edith user (non-root, isolated from nexus-cell's
# "agent" user) — needs HOME writable for the claude CLI config dir.
RUN useradd -m -u 1500 edith && chown -R edith /app
USER edith

# Override the inherited nexus-cell HEALTHCHECK (which probes localhost:3100
# — a NEXUS port EDITH doesn't bind). Process-alive check is the right
# signal: catches edith.py crashing inside an otherwise-live container.
# nexus-cell base lacks procps (no pgrep/ps), so probe /proc/1/cmdline
# directly — edith.py runs as PID 1 since we override ENTRYPOINT.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD grep -q edith.py /proc/1/cmdline || exit 1

# Override the inherited nexus-cell ENTRYPOINT (which expects the cell
# engine layout) and run edith.py directly.
ENTRYPOINT []
CMD ["python3", "-u", "edith.py"]
