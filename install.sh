#!/usr/bin/env bash
# DACH Job Bot 2.0 — installer: creates/updates .env, optionally issues the crawler Bot API key, starts the stack.
#
#   ./install.sh                              interactive
#   ./install.sh --crawler-dir /opt/Crawdler  bot runs on the crawler host: create the API key automatically
#   ./install.sh --no-start                   only write .env
#   BOT_TOKEN=... CRAWLER_API_TOKEN=... ./install.sh --non-interactive
#
# Existing .env values are kept as defaults (a backup .env.bak.<time> is written). Generated secrets
# (database password, CV encryption key) are never overwritten. Secrets are not echoed or put on command lines.
set -euo pipefail

cd "$(dirname "$0")"
ENV_FILE=.env
INTERACTIVE=1
START=ask
CRAWLER_DIR=""
DEFAULT_CRAWLER_URL="https://crawdler.duckdns.org/api/bot/v1"

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --non-interactive|-y) INTERACTIVE=0 ;;
    --no-start) START=no ;;
    --start) START=yes ;;
    --crawler-dir) CRAWLER_DIR="${2:?--crawler-dir needs a path}"; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown option: $1" >&2; usage 1 ;;
  esac
  shift
done
[[ -t 0 ]] || [[ $INTERACTIVE -eq 0 ]] || { echo "stdin is not a terminal; use --non-interactive" >&2; exit 1; }

c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_err=$'\e[31m'; c_dim=$'\e[2m'; c_off=$'\e[0m'
[[ -t 1 ]] || { c_ok=""; c_warn=""; c_err=""; c_dim=""; c_off=""; }
ok()   { printf '%s✔%s %s\n' "$c_ok" "$c_off" "$*"; }
warn() { printf '%s!%s %s\n' "$c_warn" "$c_off" "$*" >&2; }
die()  { printf '%s✘%s %s\n' "$c_err" "$c_off" "$*" >&2; exit 1; }
hdr()  { printf '\n%s== %s ==%s\n' "$c_dim" "$*" "$c_off"; }

# --- helpers -----------------------------------------------------------------------------------------------
rand_hex() { if command -v openssl >/dev/null; then openssl rand -hex "$1"; else python3 -c "import secrets;print(secrets.token_hex($1))"; fi; }
fernet_key() {  # 32 random bytes, url-safe base64 with padding (= cryptography.Fernet.generate_key())
  if command -v openssl >/dev/null; then openssl rand -base64 32 | tr '+/' '-_'
  else python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"; fi
}

# read the current .env without executing it
declare -A CUR=()
if [[ -f $ENV_FILE ]]; then
  while IFS= read -r line || [[ -n $line ]]; do
    [[ $line =~ ^[[:space:]]*([A-Z0-9_]+)=(.*)$ ]] || continue
    k=${BASH_REMATCH[1]}; v=${BASH_REMATCH[2]}
    v=${v%%[[:space:]]#*}                      # trailing comment
    v=${v%"${v##*[![:space:]]}"}               # trailing spaces
    if [[ $v =~ ^\'(.*)\'$ || $v =~ ^\"(.*)\"$ ]]; then v=${BASH_REMATCH[1]}; fi
    CUR[$k]=$v
  done < "$ENV_FILE"
fi
cur() { local k=$1; if [[ -n ${!k:-} ]]; then printf '%s' "${!k}"; else printf '%s' "${CUR[$k]:-${2:-}}"; fi; }

# ask VAR "question" default [secret] [validator]
ask() {
  local var=$1 q=$2 def=$3 secret=${4:-} validate=${5:-} ans shown
  while true; do
    if [[ $INTERACTIVE -eq 0 ]]; then ans=$def
    else
      if [[ -n $secret && -n $def ]]; then shown="${def:0:4}…(set)"; else shown=$def; fi
      if [[ -n $secret ]]; then read -rsp "$q${shown:+ [$shown]}: " ans; echo; else read -rp "$q${shown:+ [$shown]}: " ans; fi
      ans=${ans:-$def}
    fi
    if [[ -z $validate ]] || "$validate" "$ans"; then printf -v "$var" '%s' "$ans"; return; fi
    [[ $INTERACTIVE -eq 1 ]] || die "invalid value for $var"
  done
}
yesno() {  # yesno "question" default(y|n)
  local def=$2 ans
  [[ $INTERACTIVE -eq 0 ]] && { [[ $def == y ]]; return; }
  read -rp "$1 [$( [[ $def == y ]] && echo Y/n || echo y/N )]: " ans
  ans=${ans:-$def}; [[ $ans =~ ^[YyJjДд] ]]
}

v_bot_token() { [[ $1 =~ ^[0-9]{5,}:[A-Za-z0-9_-]{30,}$ ]] || { warn "expected a token from @BotFather like 123456789:AA…"; return 1; }; }
v_lang()      { [[ $1 =~ ^(ru|uk|de|en)$ ]] || { warn "one of: ru uk de en"; return 1; }; }
v_ids()       { [[ -z $1 || $1 =~ ^[0-9]+([[:space:]]*,[[:space:]]*[0-9]+)*$ ]] || { warn "comma separated numeric Telegram ids"; return 1; }; }
v_url()       { [[ $1 =~ ^https?://[^[:space:]]+$ ]] || { warn "an http(s) URL"; return 1; }; }
v_any()       { return 0; }
v_number()    { [[ $1 =~ ^[0-9]+([.][0-9]+)?$ ]] || { warn "a number"; return 1; }; }
v_provider()  { [[ $1 =~ ^(none|openai_compat)$ ]] || { warn "none or openai_compat"; return 1; }; }
v_noquote()   { [[ $1 != *"'"* ]] || { warn "single quotes are not allowed"; return 1; }; }

# value → .env syntax: plain when safe, otherwise single quotes (literal for docker compose and python-dotenv)
q() { if [[ $1 =~ ^[A-Za-z0-9_.:/@+=,\-]*$ ]]; then printf '%s' "$1"; else printf "'%s'" "$1"; fi; }

# GET <url> with Bearer token passed via stdin config (not visible in `ps`); prints the HTTP status
http_status() {
  local url=$1 token=$2
  command -v curl >/dev/null || { echo 000; return; }
  printf 'header = "Authorization: Bearer %s"\n' "$token" | curl -sS -o /dev/null -w '%{http_code}' -m 15 -K - "$url" 2>/dev/null || echo 000
}

# --- 0. prerequisites ----------------------------------------------------------------------------------------
hdr "Prerequisites"
command -v openssl >/dev/null || command -v python3 >/dev/null || die "need openssl or python3 to generate secrets"
HAVE_DOCKER=0
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then HAVE_DOCKER=1; ok "docker compose $(docker compose version --short 2>/dev/null)"
else warn "docker compose not found — .env will be written, the stack will not be started"; START=no; fi
command -v curl >/dev/null && ok "curl" || warn "curl not found — tokens cannot be verified online"

# --- 1. Telegram -----------------------------------------------------------------------------------------------
hdr "Telegram"
ask BOT_TOKEN "Bot token from @BotFather" "$(cur BOT_TOKEN)" secret v_bot_token
if command -v curl >/dev/null; then
  me=$(curl -sS -m 10 -K - <<<"url = \"https://api.telegram.org/bot${BOT_TOKEN}/getMe\"" 2>/dev/null || true)
  if [[ $me == *'"ok":true'* ]]; then ok "Telegram accepts the token: @$(sed -n 's/.*"username":"\([^"]*\)".*/\1/p' <<<"$me")"
  elif [[ -n $me ]]; then warn "Telegram rejected the token (getMe: $(sed -n 's/.*"description":"\([^"]*\)".*/\1/p' <<<"$me"))"
  else warn "Telegram not reachable from here — token not verified"; fi
fi
admin_cur=$(cur ADMIN_IDS); admin_cur=${admin_cur//[\[\] ]/}
ask ADMIN_IDS_RAW "Admin Telegram ids (comma separated, optional)" "$admin_cur" "" v_ids
ADMIN_IDS="[${ADMIN_IDS_RAW// /}]"
ask DEFAULT_LANGUAGE "Default interface language (ru/uk/de/en)" "$(cur DEFAULT_LANGUAGE ru)" "" v_lang

# --- 2. Crawler Bot API ----------------------------------------------------------------------------------------
hdr "Crawler Bot API"
ask CRAWLER_BASE_URL "Crawler Bot API base URL" "$(cur CRAWLER_BASE_URL "$DEFAULT_CRAWLER_URL")" "" v_url
CRAWLER_BASE_URL=${CRAWLER_BASE_URL%/}
CRAWLER_API_TOKEN=$(cur CRAWLER_API_TOKEN)

[[ -z $CRAWLER_DIR && -z $CRAWLER_API_TOKEN && -d /opt/Crawdler && $INTERACTIVE -eq 1 ]] && \
  yesno "Crawler found in /opt/Crawdler. Create a Bot API key there now?" y && CRAWLER_DIR=/opt/Crawdler

if [[ -n $CRAWLER_DIR ]]; then
  [[ $HAVE_DOCKER -eq 1 ]] || die "--crawler-dir needs docker compose"
  [[ -f $CRAWLER_DIR/.env ]] || die "$CRAWLER_DIR/.env not found"
  name="telegram"
  grep -Eq "^BOT_API_KEYS=(.*,)?[[:space:]]*${name}:" "$CRAWLER_DIR/.env" && name="telegram-$(date +%Y%m%d%H%M)"
  echo "Generating key '$name' in $CRAWLER_DIR …"
  out=$(cd "$CRAWLER_DIR" && docker compose exec -T api python -m gcc_core.botauth generate "$name") \
    || die "key generation failed (is the crawler 'api' container running?)"
  CRAWLER_API_TOKEN=$(sed -n 's/^TOKEN[^:]*: *//p' <<<"$out" | head -1)
  entry=$(sed -n 's/^BOT_API_KEYS entry[^:]*: *//p' <<<"$out" | head -1)
  [[ -n $CRAWLER_API_TOKEN && $entry == "$name":sha256:* ]] || die "unexpected output of gcc_core.botauth"
  if yesno "Add the key to $CRAWLER_DIR/.env (backup kept) and restart the crawler api?" y; then
    cp -p "$CRAWLER_DIR/.env" "$CRAWLER_DIR/.env.bak.$(date +%Y%m%d%H%M%S)"
    if grep -q '^BOT_API_KEYS=' "$CRAWLER_DIR/.env"; then
      current=$(sed -n 's/^BOT_API_KEYS=//p' "$CRAWLER_DIR/.env" | tail -1)
      newval=${current:+$current,}$entry
      tmp=$(mktemp); awk -v v="$newval" 'BEGIN{d=0} /^BOT_API_KEYS=/{ if(!d){print "BOT_API_KEYS=" v; d=1}; next } {print}' "$CRAWLER_DIR/.env" > "$tmp"
      cat "$tmp" > "$CRAWLER_DIR/.env"; rm -f "$tmp"
    else
      printf '\nBOT_API_KEYS=%s\n' "$entry" >> "$CRAWLER_DIR/.env"
    fi
    (cd "$CRAWLER_DIR" && docker compose up -d api >/dev/null) && ok "crawler api restarted with key '$name'"
  else
    warn "Add this line to the crawler's .env yourself, then restart its api:  BOT_API_KEYS=$entry"
  fi
elif [[ -z $CRAWLER_API_TOKEN || $INTERACTIVE -eq 1 ]]; then
  [[ $INTERACTIVE -eq 1 ]] && echo "${c_dim}Create it on the crawler host:  docker compose exec api python -m gcc_core.botauth generate telegram${c_off}"
  ask CRAWLER_API_TOKEN "Crawler Bot API token (empty = set later)" "$CRAWLER_API_TOKEN" secret v_noquote
fi

if [[ -n $CRAWLER_API_TOKEN ]]; then
  status=000
  for _ in 1 2 3 4 5 6; do  # the crawler api may still be restarting
    status=$(http_status "$CRAWLER_BASE_URL/facets/countries" "$CRAWLER_API_TOKEN")
    [[ $status == 200 || $status == 401 ]] && break
    [[ -n $CRAWLER_DIR ]] || break
    sleep 5
  done
  case $status in
    200) ok "crawler accepts the token" ;;
    401) warn "crawler rejected the token (401) — check BOT_API_KEYS on the crawler" ;;
    503) warn "crawler Bot API is disabled (503) — BOT_API_KEYS is not set on the crawler yet" ;;
    429) warn "crawler rate limit (429) — try again in a minute" ;;
    000) warn "crawler not reachable from here — token not verified" ;;
    *)   warn "crawler answered HTTP $status" ;;
  esac
else
  warn "CRAWLER_API_TOKEN is empty: the bot starts, but search and subscriptions fail until it is set"
fi

# --- 3. AI (optional) ------------------------------------------------------------------------------------------
hdr "AI (optional — everything works without it)"
AI_PROVIDER=$(cur AI_PROVIDER none)
[[ $INTERACTIVE -eq 1 ]] && echo "${c_dim}none = rules only; openai_compat = any OpenAI-compatible endpoint (Ollama, vLLM, LiteLLM, hosted APIs)${c_off}"
ask AI_PROVIDER "AI provider (none/openai_compat)" "$AI_PROVIDER" "" v_provider
AI_BASE_URL=$(cur AI_BASE_URL http://localhost:11434/v1); AI_API_KEY=$(cur AI_API_KEY)
AI_CHAT_MODEL=$(cur AI_CHAT_MODEL qwen2.5:7b-instruct); AI_EMBED_MODEL=$(cur AI_EMBED_MODEL)
AI_PROVIDER_LABEL=$(cur AI_PROVIDER_LABEL "external AI provider"); AI_MONTHLY_BUDGET_USD=$(cur AI_MONTHLY_BUDGET_USD 5)
AI_PRICE_IN_PER_1K=$(cur AI_PRICE_IN_PER_1K 0); AI_PRICE_OUT_PER_1K=$(cur AI_PRICE_OUT_PER_1K 0)
if [[ $AI_PROVIDER == openai_compat ]]; then
  ask AI_BASE_URL "Endpoint base URL (…/v1)" "$AI_BASE_URL" "" v_url
  ask AI_API_KEY "API key (empty for local Ollama)" "$AI_API_KEY" secret v_noquote
  ask AI_CHAT_MODEL "Chat model" "$AI_CHAT_MODEL" "" v_noquote
  ask AI_EMBED_MODEL "Embedding model (empty = no semantic reranking)" "$AI_EMBED_MODEL" "" v_noquote
  ask AI_PROVIDER_LABEL "Provider name shown to users in consent texts" "$AI_PROVIDER_LABEL" "" v_noquote
  ask AI_MONTHLY_BUDGET_USD "Monthly AI budget, USD" "$AI_MONTHLY_BUDGET_USD" "" v_number
  ask AI_PRICE_IN_PER_1K "Price per 1k input tokens, USD (0 for local)" "$AI_PRICE_IN_PER_1K" "" v_number
  ask AI_PRICE_OUT_PER_1K "Price per 1k output tokens, USD (0 for local)" "$AI_PRICE_OUT_PER_1K" "" v_number
  if command -v curl >/dev/null; then
    st=$(http_status "${AI_BASE_URL%/}/models" "${AI_API_KEY:-none}")
    [[ $st == 200 ]] && ok "AI endpoint reachable" || warn "AI endpoint answered HTTP $st on /models (the bot falls back to rules if it stays unavailable)"
  fi
fi

# --- 4. generated secrets (kept if present) -----------------------------------------------------------------------
hdr "Secrets"
BOT_DB_PASSWORD=$(cur BOT_DB_PASSWORD)
if [[ -z $BOT_DB_PASSWORD || $BOT_DB_PASSWORD == change-me ]]; then
  BOT_DB_PASSWORD=$(rand_hex 24); ok "generated database password"
  if [[ $HAVE_DOCKER -eq 1 ]] && docker volume ls -q 2>/dev/null | grep -qE '_bot-db$'; then
    warn "a bot-db volume already exists: a NEW password will not match it (restore the old .env or remove the volume)"
  fi
else ok "database password kept"; fi
CV_ENCRYPTION_KEY=$(cur CV_ENCRYPTION_KEY)
if [[ -z $CV_ENCRYPTION_KEY ]]; then
  if yesno "Generate a CV encryption key? (without it, extracted CV text is never stored)" y; then CV_ENCRYPTION_KEY=$(fernet_key); ok "generated CV encryption key"; fi
else ok "CV encryption key kept (changing it makes pending CV texts unreadable)"; fi

# --- 5. write .env ------------------------------------------------------------------------------------------------
hdr "Writing $ENV_FILE"
if [[ -f $ENV_FILE ]]; then cp -p "$ENV_FILE" "$ENV_FILE.bak.$(date +%Y%m%d%H%M%S)"; ok "backup of the previous .env written"; fi
umask 077
tmp=$(mktemp "$ENV_FILE.XXXXXX")
{
  echo "# Generated by install.sh on $(date -Iseconds). Re-run ./install.sh to change; see .env.example for all options."
  echo
  echo "# --- Telegram"
  echo "BOT_TOKEN=$(q "$BOT_TOKEN")"
  echo "ADMIN_IDS=$(q "$ADMIN_IDS")"
  echo "DEFAULT_LANGUAGE=$DEFAULT_LANGUAGE"
  echo
  echo "# --- database (docker compose builds DATABASE_URL from BOT_DB_PASSWORD)"
  echo "BOT_DB_PASSWORD=$BOT_DB_PASSWORD"
  [[ -n ${CUR[DATABASE_URL]:-} ]] && echo "DATABASE_URL=$(q "${CUR[DATABASE_URL]}")"
  echo
  echo "# --- crawler Bot API (read-only)"
  echo "CRAWLER_BASE_URL=$(q "$CRAWLER_BASE_URL")"
  echo "CRAWLER_API_TOKEN=$(q "$CRAWLER_API_TOKEN")"
  echo "CRAWLER_TIMEOUT_S=$(cur CRAWLER_TIMEOUT_S 20)"
  echo "FACETS_TTL_S=$(cur FACETS_TTL_S 900)"
  echo
  echo "# --- feed / notifications"
  for kv in FEED_POLL_INTERVAL_S:60 FEED_PAGE_SIZE:200 PROCESSOR_INTERVAL_S:15 SENDER_INTERVAL_S:5 INBOX_RETENTION_DAYS:14 \
            DIGEST_THRESHOLD:3 MORNING_DIGEST_HOUR:8 EVENING_DIGEST_HOUR:19; do
    echo "${kv%%:*}=$(q "$(cur "${kv%%:*}" "${kv#*:}")")"
  done
  echo
  echo "# --- AI (optional)"
  echo "AI_PROVIDER=$AI_PROVIDER"
  echo "AI_PROVIDER_LABEL=$(q "$AI_PROVIDER_LABEL")"
  echo "AI_BASE_URL=$(q "$AI_BASE_URL")"
  echo "AI_API_KEY=$(q "$AI_API_KEY")"
  echo "AI_CHAT_MODEL=$(q "$AI_CHAT_MODEL")"
  echo "AI_EMBED_MODEL=$(q "$AI_EMBED_MODEL")"
  echo "AI_MONTHLY_BUDGET_USD=$AI_MONTHLY_BUDGET_USD"
  echo "AI_DAILY_USER_CALLS=$(q "$(cur AI_DAILY_USER_CALLS 30)")"
  echo "AI_PRICE_IN_PER_1K=$AI_PRICE_IN_PER_1K"
  echo "AI_PRICE_OUT_PER_1K=$AI_PRICE_OUT_PER_1K"
  echo "AI_SEMANTIC_WEIGHT=$(q "$(cur AI_SEMANTIC_WEIGHT 0.2)")"
  echo
  echo "# --- CV"
  echo "CV_ENCRYPTION_KEY=$(q "$CV_ENCRYPTION_KEY")"
  echo "CV_RETENTION_HOURS=$(q "$(cur CV_RETENTION_HOURS 24)")"
  echo "CV_MAX_BYTES=$(q "$(cur CV_MAX_BYTES 5242880)")"
  echo
  echo "LOG_LEVEL=$(q "$(cur LOG_LEVEL INFO)")"
} > "$tmp"
chmod 600 "$tmp"
mv "$tmp" "$ENV_FILE"
ok "$ENV_FILE written (mode 600)"

# --- 6. start ------------------------------------------------------------------------------------------------------
if [[ $START == ask ]]; then
  if [[ $INTERACTIVE -eq 1 ]] && yesno "Build and start the bot now (docker compose up -d --build)?" y; then START=yes; else START=no; fi
fi
if [[ $START == yes ]]; then
  hdr "Starting"
  docker compose up -d --build
  echo "waiting for the bot container (migrations run on start) …"
  for _ in $(seq 1 30); do
    state=$(docker compose ps --format '{{.Service}} {{.State}}' 2>/dev/null | awk '$1=="bot"{print $2}')
    [[ $state == running ]] && break
    sleep 2
  done
  [[ $state == running ]] && ok "bot is running" || warn "bot is not running yet: docker compose logs bot"
  if [[ -n $CRAWLER_API_TOKEN ]]; then
    hdr "Crawler contract check"
    docker compose run --rm --no-deps bot vacancies-bot check-crawler || warn "some crawler checks failed (see above)"
  fi
  echo
  echo "Logs:  docker compose logs -f bot        Update: git pull && docker compose up -d --build"
else
  echo
  echo "Start later with:  docker compose up -d --build   (then: docker compose run --rm bot vacancies-bot check-crawler)"
fi
