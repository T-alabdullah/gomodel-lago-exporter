FROM python:3.12.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN pip install -r requirements.lock
COPY exporter ./exporter
COPY scripts ./scripts
COPY demo ./demo
RUN pip install --no-deps --no-build-isolation . && useradd --uid 10001 --create-home exporter
USER exporter
EXPOSE 8000
CMD ["exporter", "serve"]
