FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY . .

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

# --reload: app.py / tg_group.py edits (bind-mounted at runtime, see docker-compose.yml)
# restart the worker automatically. workflow.json and static/index.html need no
# restart at all - the app re-reads them on every request/poll.
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:8080", "--reload", "app:app"]
