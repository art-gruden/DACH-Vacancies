FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN useradd --create-home --uid 10001 bot
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .
COPY alembic.ini ./
COPY migrations ./migrations
COPY contracts ./contracts
USER bot
# migrations first, then polling + background workers in one process
CMD ["sh", "-c", "alembic upgrade head && vacancies-bot run"]
