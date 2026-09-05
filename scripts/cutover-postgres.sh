#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${REBAR_NAMESPACE:-rebar-optimizer}"
ENVIRONMENT="${1:-prod}"
IMAGE="${2:-}"
HOST="${3:-rebar.example.com}"

if [ -z "$IMAGE" ]; then
  echo "usage: CONFIRM_REDIS_FLUSH=YES $0 <dev|prod|prod-private> <image> [host]" >&2
  exit 2
fi

if [ "${CONFIRM_REDIS_FLUSH:-}" != "YES" ]; then
  cat >&2 <<MSG
This clean cutover permanently deletes every key in the selected Redis database.
Re-run with CONFIRM_REDIS_FLUSH=YES when you are ready.
MSG
  exit 2
fi

echo "Checking PostgreSQL service/credentials and applying schema before the destructive step..."
kubectl -n "$NAMESPACE" get service a101-postgres >/dev/null
kubectl -n "$NAMESPACE" get secret a101-postgres-auth >/dev/null
"$(dirname "$0")/migrate-db.sh" "$IMAGE"

echo "Stopping old API and worker before destructive Redis cleanup..."
kubectl -n "$NAMESPACE" scale deployment/rebar-api --replicas=0
kubectl -n "$NAMESPACE" delete scaledobject rebar-worker --ignore-not-found=true
kubectl -n "$NAMESPACE" scale deployment/rebar-worker --replicas=0

kubectl -n "$NAMESPACE" wait --for=delete pod -l app=rebar-worker --timeout=5m || true
kubectl -n "$NAMESPACE" wait --for=delete pod -l app=rebar-api --timeout=5m || true

"$(dirname "$0")/clear-redis.sh"
"$(dirname "$0")/deploy-k8s.sh" "$ENVIRONMENT" "$IMAGE" "$HOST"

echo "Clean PostgreSQL cutover completed."
