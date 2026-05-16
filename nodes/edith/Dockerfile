FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY edith.py .

# Run as non-root for safety
RUN useradd -m -u 1000 edith && chown -R edith /app
USER edith

CMD ["python3", "-u", "edith.py"]
