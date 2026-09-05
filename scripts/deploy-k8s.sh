#!/usr/bin/env bash
set -euo pipefail

ENVIRONMENT="${1:-dev}"
IMAGE="${2:-}"
HOST="${3:-rebar.example.com}"
OVERLAY="deploy/k8s/overlays/${ENVIRONMENT}"

case "$ENVIRONMENT" in
  dev|prod|prod-private) ;;
  *) echo "environment must be dev, prod or prod-private" >&2; exit 2 ;;
esac
[ -n "$IMAGE" ] || { echo "usage: $0 <dev|prod|prod-private> <image> [host]" >&2; exit 2; }
[ -d "$OVERLAY" ] || { echo "missing $OVERLAY" >&2; exit 2; }

kubectl kustomize "$OVERLAY" \
  | sed "s#ghcr.io/contextmachine/a101_reinforcement_am:am-super-branch#$IMAGE#g; s#rebar.example.com#$HOST#g" \
  > /tmp/rebar-rendered.yaml

kubectl apply -f /tmp/rebar-rendered.yaml
kubectl -n rebar-optimizer rollout status deployment/rebar-api --timeout=5m
kubectl -n rebar-optimizer get deployment/rebar-worker
kubectl -n rebar-optimizer get scaledobject/rebar-worker
