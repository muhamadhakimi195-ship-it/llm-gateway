FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY src ./src
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-deps . \
    && useradd --create-home --uid 10001 gateway \
    && mkdir /app/data && chown gateway:gateway /app/data
COPY config ./config
COPY alembic.ini ./
COPY migrations ./migrations
USER gateway
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)"
CMD ["python", "-m", "gateway.cli", "serve", "--host", "0.0.0.0"]
