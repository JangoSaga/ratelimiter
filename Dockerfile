FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /srv
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
ENV PATH="/srv/.venv/bin:$PATH"
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY scripts/start.sh ./scripts/start.sh
RUN useradd -r -u 10001 app && chmod +x scripts/start.sh
USER app
EXPOSE 8000
CMD ["./scripts/start.sh"]
