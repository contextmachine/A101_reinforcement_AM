#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

python -m compileall -q A101 rebar_service run3.py
python -m pytest -q
alembic upgrade head --sql > "$TMP_DIR/alembic.sql"

grep -q 'CREATE TABLE tasks' "$TMP_DIR/alembic.sql"
grep -q 'CREATE TABLE task_variants' "$TMP_DIR/alembic.sql"
grep -q 'CREATE TABLE solutions' "$TMP_DIR/alembic.sql"

if command -v kubectl >/dev/null 2>&1; then
  for overlay in dev prod prod-private; do
    kubectl kustomize "deploy/k8s/overlays/$overlay" > "$TMP_DIR/$overlay.yaml"
  done
fi
