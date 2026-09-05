#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${REBAR_NAMESPACE:-rebar-optimizer}"
IMAGE="${1:-}"
TEMPLATE="deploy/k8s/base/db-migrate-job.yaml"
JOB="rebar-db-migrate"

if [ -z "$IMAGE" ]; then
  echo "usage: $0 <image>" >&2
  exit 2
fi

if [ ! -f "$TEMPLATE" ]; then
  echo "missing migration job template: $TEMPLATE" >&2
  exit 2
fi

TMP_FILE="$(mktemp)"
trap 'rm -f "$TMP_FILE"' EXIT

sed "s#ghcr.io/contextmachine/a101_reinforcement_am:am-super-branch#${IMAGE}#g" "$TEMPLATE" > "$TMP_FILE"

kubectl -n "$NAMESPACE" delete job "$JOB" --ignore-not-found=true >/dev/null
kubectl apply -f "$TMP_FILE"

if ! kubectl -n "$NAMESPACE" wait --for=condition=complete "job/$JOB" --timeout=5m; then
  echo "database migration failed; job logs:" >&2
  kubectl -n "$NAMESPACE" logs "job/$JOB" --all-containers=true >&2 || true
  exit 1
fi

kubectl -n "$NAMESPACE" logs "job/$JOB" --all-containers=true
