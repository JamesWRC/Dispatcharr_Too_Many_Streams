#!/bin/bash
# USAGE: bash setup_dev.sh
# Sets up a Python virtual environment, installs dependencies, and loads environment variables from a .env file if present.
# Idempotent: can be run multiple times without adverse effects.
set -euo pipefail

# Clone the Dispatcharr repo (main)
echo "Cloning Dispatcharr repository..."
if [ -d "Dispatcharr" ]; then
    cd Dispatcharr; git pull; cd ..
else
    git clone --depth=1 -b main https://github.com/Dispatcharr/Dispatcharr.git Dispatcharr
fi


# 1) Create venv if missing
if [ ! -d ".venv" ]; then
  echo "[setup] Creating venv ..."
  python -m venv .venv
fi

# 2) Activate venv
# shellcheck disable=SC1091
echo "[setup] Activating venv ..."
# If windows 
if [ -f ".venv/Scripts/activate" ]; then
    echo "[setup] Detected Windows environment."
    source .venv/Scripts/activate
    else 
    echo "[setup] Detected POSIX environment."
    source .venv/bin/activate
fi


# 3) Upgrade pip tooling (idempotent/fast if already latest)
python -m pip install --upgrade pip wheel >/dev/null

# 4) Install requirements if present (idempotent)
if [ -f "requirements.txt" ]; then
  echo "[setup] Installing requirements ..."
  pip install -r requirements.txt
fi

# 5) Export environment from .env if present (POSIX-friendly)
if [ -f ".env" ]; then
  echo "[setup] Loading .env into current shell ..."
  # Export all variables set while sourcing .env
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

echo "[setup] Done. venv: $(python -V) | site: $(python -c 'import sys;print(sys.prefix)')"