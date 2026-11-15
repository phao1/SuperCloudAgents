#!/usr/bin/env bash
set -euo pipefail

# Pass kubectl global options, e.g. --context kind-sca.
kubectl "$@" apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.28.2/manifests/calico.yaml
kubectl "$@" -n kube-system rollout status daemonset/calico-node --timeout=300s
kubectl "$@" -n kube-system rollout status deployment/calico-kube-controllers --timeout=300s
kubectl "$@" -n kube-system rollout status deployment/coredns --timeout=300s
kubectl "$@" -n kube-system wait --for=condition=Ready pod -l k8s-app=kube-dns --timeout=300s
