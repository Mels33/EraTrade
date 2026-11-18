#!/usr/bin/env bash
# Управление EraTrade одной командой.
#
#   scripts/eratrade.sh up        — БД + API с панелью + бэкапы (Docker)
#   scripts/eratrade.sh down      — остановить всё (данные БД сохраняются в volume)
#   scripts/eratrade.sh restart   — пересобрать и перезапустить
#   scripts/eratrade.sh status    — контейнеры и сводка состояния бота
#   scripts/eratrade.sh logs [сервис]  — логи (по умолчанию всех сервисов)
#   scripts/eratrade.sh dev       — разработка: БД в Docker, API и панель локально с перезагрузкой
#   scripts/eratrade.sh mm-start  — маркет-мейкер с живыми заявками (котирует в режиме LIVE_ENABLED)
#   scripts/eratrade.sh mm-stop   — остановить маркет-мейкер (заявки снимаются, позиция остаётся)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PROFILE="core"
API_URL="http://127.0.0.1:8000"
DEV_URL="http://127.0.0.1:5173"
ENV_MAIN=".env.mainnet"
ENV_SECRETS=".env.mainnet.secrets"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
bold() { printf '\033[1m%s\033[0m\n' "$*"; }

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  red "Не найден docker compose / docker-compose"
  exit 1
fi

compose() { "${COMPOSE[@]}" --profile "$PROFILE" "$@"; }

check_env() {
  local missing=0
  for f in "$ENV_MAIN" "$ENV_SECRETS"; do
    if [[ ! -f "$f" ]]; then
      red "Нет файла $f (образец: .env.example.mainnet)"
      missing=1
    fi
  done
  [[ $missing -eq 0 ]] || exit 1
  if ! grep -q '^API_TOKEN=' "$ENV_SECRETS"; then
    printf '# Токен оператора для API/панели (вход в панель)\nAPI_TOKEN=%s\n' \
      "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" >>"$ENV_SECRETS"
    chmod 600 "$ENV_SECRETS"
    green "Сгенерирован API_TOKEN в $ENV_SECRETS"
  fi
}

wait_db() {
  printf 'Ожидание PostgreSQL'
  for _ in $(seq 1 60); do
    if [[ "$(docker inspect -f '{{.State.Health.Status}}' eratrade-pg 2>/dev/null)" == "healthy" ]]; then
      echo " — готов"
      return 0
    fi
    printf '.'
    sleep 1
  done
  echo
  red "PostgreSQL не стал healthy за 60 с"
  exit 1
}

wait_api() {
  local url="$1"
  printf 'Ожидание API'
  for _ in $(seq 1 90); do
    if curl -sf "$url/api/health/live" >/dev/null 2>&1; then
      echo " — готов"
      return 0
    fi
    printf '.'
    sleep 1
  done
  echo
  red "API не ответил за 90 с — смотрите: scripts/eratrade.sh logs api-mainnet"
  exit 1
}

token() { grep '^API_TOKEN=' "$ENV_SECRETS" | cut -d= -f2-; }

summary() {
  local url="${1:-$API_URL}"
  local json
  if ! json="$(curl -sf -H "Authorization: Bearer $(token)" "$url/api/status")"; then
    red "API недоступен ($url)"
    return 1
  fi
  python3 - "$json" <<'EOF'
import json, sys
d = json.loads(sys.argv[1])
blocked = f"ЗАПРЕЩЕНЫ ({d['block_reason']})" if d["entries_blocked"] else "разрешены режимом"
print(f"  Сеть: {d['network'].upper()} · режим {d['mode']} · входы {blocked}")
for c in d["components"]:
    state = c["details"].get("state") or ""
    ok = "ok " if c["last_beat_age_s"] < 60 else "НЕТ"
    print(f"  [{ok}] {c['component']:<6} heartbeat {c['last_beat_age_s']:.0f} с назад  {state}")
    if c["component"] == "mm":
        det = c["details"]
        print(f"         результат за сутки {det.get('pnl_today')} USDC, баланс {det.get('spot_usdc')} USDC")
EOF
}

cmd_up() {
  check_env
  bold "Запуск EraTrade (Docker)"
  "${COMPOSE[@]}" up -d postgres
  wait_db
  compose up -d --build
  wait_api "$API_URL"
  echo
  compose ps --format 'table {{.Name}}\t{{.Status}}' 2>/dev/null || compose ps
  echo
  green "Панель: $API_URL   (токен: API_TOKEN в $ENV_SECRETS)"
  echo "API docs: $API_URL/api/docs"
  summary
}

cmd_down() {
  bold "Остановка EraTrade"
  echo "Маркет-мейкер снимет заявки; позиция (если есть) останется."
  "${COMPOSE[@]}" --profile live stop mm-mainnet >/dev/null 2>&1 || true
  compose down
  green "Остановлено. Данные БД сохранены (volume eratrade_pg_data)."
}

cmd_status() {
  compose ps --format 'table {{.Name}}\t{{.Status}}' 2>/dev/null || compose ps
  echo
  summary || true
}

cmd_logs() {
  if [[ $# -gt 0 ]]; then
    compose logs -f --tail=100 "$@"
  else
    compose logs -f --tail=50
  fi
}

cmd_dev() {
  check_env
  command -v uv >/dev/null || { red "Нужен uv"; exit 1; }
  command -v npm >/dev/null || { red "Нужен npm"; exit 1; }
  bold "Режим разработки: БД в Docker, API и панель локально"
  "${COMPOSE[@]}" up -d postgres
  wait_db
  # Контейнер API занимает порт 8000 — на время разработки останавливаем только его.
  compose stop api-mainnet >/dev/null 2>&1 || true
  [[ -d frontend/node_modules ]] || (cd frontend && npm install --no-audit --no-fund)

  export ERATRADE_ENV_FILE="$ENV_MAIN,$ENV_SECRETS"
  uv run alembic upgrade head
  uv run eratrade api &
  local api_pid=$!
  (cd frontend && npm run dev -- --host 127.0.0.1) &
  local web_pid=$!
  trap 'echo; bold "Остановка dev-процессов"; kill $api_pid $web_pid 2>/dev/null || true; wait 2>/dev/null; green "Готово. Контейнер API: scripts/eratrade.sh up"' INT TERM EXIT

  wait_api "$API_URL"
  green "Панель (с перезагрузкой): $DEV_URL   API: $API_URL"
  echo "Ctrl+C — остановить"
  wait
}

cmd_mm_start() {
  check_env
  bold "Маркет-мейкер: живые заявки"
  "${COMPOSE[@]}" --profile live up -d --build mm-mainnet
  echo "Котирует только в режиме «Торгует» (панель → Управление → «Запустить торговлю»)."
  echo "Логи: scripts/eratrade.sh logs mm-mainnet"
}

cmd_mm_stop() {
  bold "Остановка маркет-мейкера"
  "${COMPOSE[@]}" --profile live stop mm-mainnet
  green "Остановлен: заявки сняты. Позицию (если есть) закрыть в панели или вручную."
}

case "${1:-}" in
  up) cmd_up ;;
  down) cmd_down ;;
  restart) cmd_down && cmd_up ;;
  status) cmd_status ;;
  logs) shift; cmd_logs "$@" ;;
  dev) cmd_dev ;;
  mm-start) cmd_mm_start ;;
  mm-stop) cmd_mm_stop ;;
  *)
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
