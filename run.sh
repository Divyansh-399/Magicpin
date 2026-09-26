#!/usr/bin/env bash
# One-command POSIX setup + run. On Windows PowerShell, use ./run.ps1 instead.
#   ./run.sh          -> sets up venv, generates dataset, starts the bot on :8080
#   ./run.sh test      -> same, then runs the full harness against it
set -e

cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

echo "Installing dependencies..."
./.venv/bin/python -m pip install --quiet -r requirements.txt

if [ ! -d expanded ]; then
  echo "Generating expanded dataset (50 merchants / 200 customers / 100 triggers)..."
  ./.venv/bin/python generate_dataset.py --seed-dir . --out expanded
fi

echo "Starting the bot on http://localhost:8080 ..."
./.venv/bin/python build_single_file.py
./.venv/bin/python vera_bot_single_file.py &
SERVER_PID=$!
sleep 2

if [ "$1" = "test" ]; then
  echo "Running the local harness..."
  ./.venv/bin/python run_local_harness.py
  echo ""
  echo "Server still running (PID $SERVER_PID). Ctrl+C or 'kill $SERVER_PID' to stop."
fi

wait $SERVER_PID
