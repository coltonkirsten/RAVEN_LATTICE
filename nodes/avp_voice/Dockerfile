# Thin proxy: no Claude CLI, no MCP — just Python + aiohttp + httpx.
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY avp_voice_node.py .

RUN useradd -m -u 1501 avpvoice && chown -R avpvoice /app
USER avpvoice

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD grep -q avp_voice_node.py /proc/1/cmdline || exit 1

ENTRYPOINT []
CMD ["python3", "-u", "avp_voice_node.py"]
