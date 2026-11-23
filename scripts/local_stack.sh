#!/usr/bin/env bash
# Start the local AWS stack (moto server) for the NBA trading bot.
#
# moto is a pure-Python AWS emulator: no Docker, no Java, no LocalStack.
# Only AWS state is emulated - Gamma, CLOB and the Polymarket WebSockets stay
# real, so the local run exercises the same code paths as AWS.
#
# Usage:
#   scripts/local_stack.sh start    # start moto + create table/queue
#   scripts/local_stack.sh stop     # stop moto
#   scripts/local_stack.sh env      # print the export lines for a second shell
#   scripts/local_stack.sh status   # show what is running

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# NOTE: the default port is 5111, not 5000 - on macOS, port 5000 is the
# AirPlay Receiver and silently intercepts connections, which surfaces as a
# confusing HTTP 403 with an "AirTunes" server header.
MOTO_PORT="${MOTO_PORT:-5111}"
MOTO_HOST="${MOTO_HOST:-127.0.0.1}"
RUN_DIR="$PROJECT_ROOT/.local"
PID_FILE="$RUN_DIR/moto.pid"
LOG_FILE="$RUN_DIR/moto.log"

VENV_PY="$PROJECT_ROOT/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  echo "error: $VENV_PY not found. Create the venv first:" >&2
  echo "  python3.12 -m venv --copies .venv && .venv/bin/pip install -r requirements-dev.txt" >&2
  exit 1
fi

export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-east-1}"
export AWS_REGION="${AWS_REGION:-ap-east-1}"

DYNAMODB_TABLE="${DYNAMODB_TABLE:-SportsBotState}"
SQS_QUEUE_NAME="${SQS_QUEUE_NAME:-SportsBotTrades}"

endpoint_url() {
  echo "http://${MOTO_HOST}:${MOTO_PORT}"
}

is_running() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

start_moto() {
  if is_running; then
    echo "moto already running (pid $(cat "$PID_FILE")) on $(endpoint_url)"
    return 0
  fi
  mkdir -p "$RUN_DIR"
  echo "starting moto server on $(endpoint_url)..."
  # moto is started through a tiny Python launcher that calls setsid(2),
  # so it gets its own session and survives the shell that started it. A
  # plain `&` background job is killed when the parent shell goes away.
  "$VENV_PY" - "$VENV_PY" "$MOTO_PORT" "$MOTO_HOST" "$LOG_FILE" "$PID_FILE" <<'PYEOF'
import os
import sys

python, port, host, log, pid_file = sys.argv[1:6]

pid = os.fork()
if pid > 0:
    sys.exit(0)

os.setsid()
with open(pid_file, "w") as fh:
    fh.write(str(os.getpid()))
with open(log, "ab", buffering=0) as out:
    os.dup2(out.fileno(), 1)
    os.dup2(out.fileno(), 2)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
os.execv(python, [python, "-m", "moto.server", "-p", port, "-H", host])
PYEOF

  for _ in $(seq 1 40); do
    if curl -s -o /dev/null -w "%{http_code}" "$(endpoint_url)" 2>/dev/null | grep -q 200; then
      echo "moto server is up (pid $(cat "$PID_FILE"))"
      return 0
    fi
    sleep 0.25
  done

  echo "error: moto server did not become ready; see $LOG_FILE" >&2
  tail -20 "$LOG_FILE" >&2 || true
  exit 1
}

stop_moto() {
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    kill -TERM "$pid" 2>/dev/null || true
    sleep 0.5
    kill -KILL "$pid" 2>/dev/null || true
    echo "moto stopped"
  else
    echo "moto not running"
  fi
  rm -f "$PID_FILE"
}

bootstrap_resources() {
  DYNAMODB_ENDPOINT_URL="$(endpoint_url)" \
  SQS_ENDPOINT_URL="$(endpoint_url)" \
  "$VENV_PY" -m src.main bootstrap-local
}

print_env() {
  cat <<EOF
export DYNAMODB_ENDPOINT_URL="$(endpoint_url)"
export SQS_ENDPOINT_URL="$(endpoint_url)"
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=ap-east-1
export AWS_REGION=ap-east-1
export DYNAMODB_TABLE=$DYNAMODB_TABLE
export SQS_QUEUE_NAME=$SQS_QUEUE_NAME
EOF
}

case "${1:-start}" in
  start)
    start_moto
    bootstrap_resources
    echo
    echo "Local stack ready. In a second terminal run:"
    echo
    print_env | sed 's/^/  /'
    echo
    echo "Then, for example:"
    echo "  .venv/bin/python -m src.main fetch-daily --date $(date +%Y-%m-%d)"
    echo
    print_env
    ;;
  stop)
    stop_moto
    ;;
  env)
    print_env
    ;;
  status)
    if is_running; then
      echo "moto running (pid $(cat "$PID_FILE")) on $(endpoint_url)"
    else
      echo "moto not running"
    fi
    ;;
  *)
    echo "usage: $0 {start|stop|env|status}" >&2
    exit 2
    ;;
esac
