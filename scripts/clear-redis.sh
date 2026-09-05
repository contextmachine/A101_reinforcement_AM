#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${REBAR_NAMESPACE:-rebar-optimizer}"

POD="$(kubectl -n "$NAMESPACE" get pod -l app=rebar-redis -o jsonpath='{.items[0].metadata.name}')"
if [ -z "$POD" ]; then
  echo "no Redis pod with label app=rebar-redis found in namespace $NAMESPACE" >&2
  exit 1
fi

echo "Redis DB size before flush:"
kubectl -n "$NAMESPACE" exec "$POD" -c redis -- \
  sh -ec 'redis-cli -a "$REBAR_REDIS_PASSWORD" --no-auth-warning DBSIZE'

kubectl -n "$NAMESPACE" exec "$POD" -c redis -- \
  sh -ec 'redis-cli -a "$REBAR_REDIS_PASSWORD" --no-auth-warning FLUSHDB'

echo "Redis DB size after flush:"
kubectl -n "$NAMESPACE" exec "$POD" -c redis -- \
  sh -ec 'redis-cli -a "$REBAR_REDIS_PASSWORD" --no-auth-warning DBSIZE'
