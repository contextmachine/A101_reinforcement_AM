#!/usr/bin/env bash
set -euo pipefail
python -m compileall -q A101 rebar_service
python -m pytest -q
