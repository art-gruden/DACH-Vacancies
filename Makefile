.PHONY: setup install test test-pg lint run migrate check demo contract
setup:     # interactive .env + start
	./install.sh
install:
	pip install -e ".[dev]"
test:
	pytest -q
test-pg:   # needs a scratch PostgreSQL database
	BOT_TEST_PG_URL=$${BOT_TEST_PG_URL:-postgresql+asyncpg://postgres@localhost/bot_test} pytest -q
lint:
	ruff check src tests
run:
	alembic upgrade head && vacancies-bot run
migrate:
	alembic upgrade head
check:
	vacancies-bot check-crawler
demo:
	vacancies-bot demo --with-ai
contract:
	./scripts/regenerate_client.sh
