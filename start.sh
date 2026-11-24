#!/usr/bin/env bash
# ===================================================================
#  Salary & Payment Reconciliation System - start here (macOS / Linux)
# ===================================================================
set -euo pipefail
cd "$(dirname "$0")"

echo
echo "  Salary & Payment Reconciliation System"
echo "  ======================================="
echo

if [ -x ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
else
    if ! command -v python3 >/dev/null 2>&1; then
        echo "  Python 3.10 or newer is required but was not found."
        echo "  Please install it from https://www.python.org/downloads/"
        exit 1
    fi
    echo "  Setting up for the first time. This takes a few minutes..."
    python3 -m venv .venv
    PYTHON=".venv/bin/python"
    "$PYTHON" -m pip install --upgrade pip --quiet
    "$PYTHON" -m pip install -r requirements.txt

    echo
    echo "  Preparing the database..."
    "$PYTHON" manage.py init

    read -r -p "  Load demonstration data so you can look around? (y/n): " SEED
    if [ "$SEED" = "y" ] || [ "$SEED" = "Y" ]; then
        "$PYTHON" manage.py seed
        "$PYTHON" manage.py process
    fi
fi

echo
echo "  Starting at http://localhost:8000"
echo "  Press Ctrl+C to stop."
echo
exec "$PYTHON" manage.py serve --host 127.0.0.1 --port 8000
