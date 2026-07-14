#!/usr/bin/env bash
# discover-k8-oicm-intraconnect-v2.sh
# Read-only cluster discovery for cross-cluster (AUH <-> Al Ain) planning.
# Safe: performs no mutations. Run once per cluster with the correct kubectl context.
set -uo pipefail

hr(){ printf '\n=== %s ===\n' "$1"; }
kq(){ kubectl "$@" 2>/dev/null; }

hr "CONTEXT"
kubectl config current-context 2>/dev/null || echo "(no context)"

hr "NODES (INTERNAL-IP = tunnel/route plane)"
kq get nodes -o wide

hr "CNI DETECTION"
kq get ds -A -o wide | grep -Ei 'cilium|canal|calico|flannel|weave|antrea|kube-ovn' \
  || echo "(no well-known CNI DaemonSet matched)"
CILIUM_NS="$(kq get ds -A --no-headers | awk '/cilium/{print $1; exit}')"
[ -n "${CILIUM_NS:-}" ] && echo "cilium namespace: $CILIUM_NS"

hr "POD CIDR (robust)"
echo "-- from Node.spec.podCIDRs (empty under Cilium IPAM):"
kq get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.podCIDRs}{"\n"}{end}'
if kq get ciliumnodes >/dev/null; then
  echo "-- from ciliumnodes.spec.ipam.podCIDRs:"
  kq get ciliumnodes -o custom-columns=NAME:.metadata.name,PODCIDRS:.spec.ipam.podCIDRs
fi
echo "-- from kube-controller-manager --cluster-cidr:"
kq -n kube-system get pod -l component=kube-controller-manager -o yaml | grep -- '--cluster-cidr=' | sort -u
if [ -n "${CILIUM_NS:-}" ]; then
  echo "-- from cilium-config (IPAM/cluster-pool/cluster identity/masquerade):"
  kq -n "$CILIUM_NS" get cm cilium-config -o yaml \
    | grep -iE 'cluster-pool-ipv4-cidr|^ *ipam:|cluster-name|cluster-id|masquerade' | sort -u
fi

hr "SERVICE CIDR"
kq get servicecidr
kq -n kube-system get pod -l component=kube-apiserver -o yaml \
  | grep -- '--service-cluster-ip-range=' | sort -u

hr "COREDNS CONFIGMAP (name varies: coredns / rke2-coredns-rke2-coredns)"
CM="$(kq get cm -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name --no-headers \
      | awk 'tolower($2) ~ /coredns/ {print $1"/"$2}' | head -1)"
if [ -n "${CM:-}" ]; then
  echo "found: $CM"
  kq -n "${CM%%/*}" get cm "${CM##*/}" -o yaml | sed -n '/Corefile/,/^kind:/p'
else
  echo "(no coredns configmap found)"
fi

hr "LOADBALANCER PROVIDER"
for ns in metallb metallb-system; do kq get ns "$ns" >/dev/null && echo "MetalLB present ($ns)"; done
kq get ds -A --no-headers | grep -i kube-vip && echo "kube-vip present"
kq get ciliumloadbalancerippools.cilium.io -A >/dev/null && echo "Cilium LB-IPAM pools:" && kq get ciliumloadbalancerippools.cilium.io -A

hr "LOADBALANCER SERVICES / INGRESS VIPs"
kq get svc -A | awk 'NR==1 || $3=="LoadBalancer"'

hr "INGRESS ROUTING RULES (host -> path -> backend service:port)"
kq get ingress -A -o custom-columns=\
'NS:.metadata.namespace,NAME:.metadata.name,CLASS:.spec.ingressClassName,HOSTS:.spec.rules[*].host' --no-headers
echo "-- detailed host/path/backend for any ingress mentioning 'infer':"
for ing in $(kq get ingress -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}' | grep -i infer); do
  echo ">> $ing"
  kq -n "${ing%%/*}" get ingress "${ing##*/}" -o yaml \
    | grep -E ' host:| path:| pathType:| name:| number:| service:' 
done
echo "-- ingress auth/tls-related annotations:"
kq get ingress -A -o yaml | grep -iE 'auth-|whitelist|tls:|secretName|cors' | sort -u

hr "DONE"