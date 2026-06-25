#!/usr/bin/env bash
# Deploy the NEF-shim and supporting resources to the k8s cluster.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
K8S_DIR="${SCRIPT_DIR}/../k8s"

echo "EdgeVision NEF-shim Deployment"
echo "=============================="
echo ""

# Check prerequisites
if ! command -v kubectl &>/dev/null; then
    echo "ERROR: kubectl not found"
    exit 1
fi

if ! kubectl cluster-info &>/dev/null; then
    echo "ERROR: Cannot reach Kubernetes cluster"
    exit 1
fi

echo "1. Applying k8s manifests..."
kubectl apply -k "${K8S_DIR}"
echo ""

echo "2. Waiting for NEF-shim deployment..."
kubectl rollout status deployment/nef-shim -n edgevision --timeout=120s
echo ""

echo "3. Deployment status:"
kubectl get all -n edgevision
echo ""

# Gateway info
GATEWAY_CLASS=$(kubectl get gatewayclass -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "unknown")
echo "GatewayClass in use: ${GATEWAY_CLASS}"

GATEWAY_IP=$(kubectl get gateway edgevision-gateway -n edgevision -o jsonpath='{.status.addresses[0].value}' 2>/dev/null || echo "pending")
echo "Gateway IP: ${GATEWAY_IP}"
echo ""

echo "=============================="
echo "NEF-shim ready at http://camara.5glab.control.lth.se"
echo ""
echo "Ensure DNS resolves camara.5glab.control.lth.se → ${GATEWAY_IP}"
echo "Or use port-forward for testing:"
echo "  kubectl port-forward -n envoy-gateway-system svc/\$(kubectl get svc -n envoy-gateway-system -l gateway.networking.k8s.io/owning-gateway-name=edgevision-gateway -o name | head -1 | cut -d/ -f2) 8080:80"
