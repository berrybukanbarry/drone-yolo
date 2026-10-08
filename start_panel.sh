#!/usr/bin/env bash
# Menyalakan Panel Drone. Dipakai manual (./start_panel.sh) maupun oleh drone-panel.service.
cd "$(dirname "$(readlink -f "$0")")" || exit 1

if [ -x .venv/bin/python ]; then
    PY=.venv/bin/python
else
    PY=python3
fi

exec "$PY" control_panel.py "$@"
