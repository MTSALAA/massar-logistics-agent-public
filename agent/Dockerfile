FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY agent.manifest.json entrypoint.py ./

# SQLite file lives in a volume so data survives container restarts.
ENV DATABASE_PATH=/data/agent.db
ENV PYTHONDONTWRITEBYTECODE=1
RUN mkdir -p /data

EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import httpx; httpx.get('http://localhost:9000/health').raise_for_status()" || exit 1

# entrypoint.py hands the data volume to an unprivileged user, drops root, then starts uvicorn on port 9000.
CMD ["python", "entrypoint.py"]
