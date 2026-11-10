#!/usr/bin/env bash
# Refresh the contract snapshot from a running crawler and regenerate the typed models.
#   CRAWLER_OPENAPI_URL=https://crawdler.duckdns.org/api/openapi.json ./scripts/regenerate_client.sh
set -euo pipefail
cd "$(dirname "$0")/.."
URL="${CRAWLER_OPENAPI_URL:-https://crawdler.duckdns.org/api/openapi.json}"
curl -fsS "$URL" -o /tmp/crawler-openapi.json
python scripts/extract_bot_contract.py /tmp/crawler-openapi.json contracts/crawler-bot-v1.openapi.json
datamodel-codegen --input contracts/crawler-bot-v1.openapi.json --input-file-type openapi \
  --output src/vacancies_bot/crawler/models_generated.py --output-model-type pydantic_v2.BaseModel \
  --target-python-version 3.12 --use-standard-collections --use-union-operator --enum-field-as-literal all \
  --use-annotated --field-constraints --disable-timestamp --use-schema-description
python -m pytest -q tests/test_contract.py
git diff --stat contracts src/vacancies_bot/crawler/models_generated.py || true
