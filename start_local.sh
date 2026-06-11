#!/bin/bash
set -e
cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
BASE_PATH="${BASE_PATH:-/pjud}"
PORT="${PORT:-8091}"
HOST="${HOST:-127.0.0.1}"
PJUD_HEADLESS="${PJUD_HEADLESS:-0}"

if [ ! -d .venv ]; then
  "$PYTHON_BIN" -m venv .venv
fi

source .venv/bin/activate
python -m pip install --upgrade pip >/dev/null
python -m pip install -r requirements.txt

if [ -z "$PJUD_CHROME_PATH" ]; then
  if [ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then
    export PJUD_CHROME_PATH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  elif [ -x "/usr/bin/google-chrome" ]; then
    export PJUD_CHROME_PATH="/usr/bin/google-chrome"
  elif [ -x "/usr/bin/google-chrome-stable" ]; then
    export PJUD_CHROME_PATH="/usr/bin/google-chrome-stable"
  elif [ -x "/usr/bin/chromium" ]; then
    export PJUD_CHROME_PATH="/usr/bin/chromium"
  fi
fi

export BASE_PATH
export PORT
export HOST
export PJUD_HEADLESS

exec python app.py
