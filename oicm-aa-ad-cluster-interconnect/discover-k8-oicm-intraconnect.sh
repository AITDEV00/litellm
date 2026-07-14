kubectl config current-context
kubectl get nodes -o wide          # note INTERNAL-IP subnets — this is what tunnels use

# --- CNI (decides whether Cilium Cluster Mesh is even on the table) ---
kubectl get pods -n kube-system -o wide | grep -Ei 'calico|cilium|flannel|weave|canal|antrea|kube-ovn'
kubectl get ds -n kube-system

# --- Pod CIDR (use whichever returns a value) ---
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.podCIDRs}{"\n"}{end}'
kubectl -n kube-system get cm kube-proxy -o jsonpath='{.data.config\.conf}' 2>/dev/null | grep -i clusterCIDR
kubectl -n kube-system get pod -l component=kube-controller-manager -o yaml 2>/dev/null | grep -- '--cluster-cidr'

# --- Service CIDR ---
kubectl get servicecidr 2>/dev/null                       # k8s >= 1.29, cleanest
kubectl -n kube-system get pod -l component=kube-apiserver -o yaml 2>/dev/null | grep -- '--service-cluster-ip-range'
# fallback: provoke the allocator to tell you the range
cat <<'EOF' | kubectl apply -f - 2>&1 | grep -i 'range of valid' || true
apiVersion: v1
kind: Service
metadata: { name: svc-cidr-probe }
spec: { type: ClusterIP, clusterIP: 1.2.3.4, ports: [{ port: 1 }] }
EOF

# --- CoreDNS upstreams / stub domains (needed for any DNS-forward approach) ---
kubectl -n kube-system get cm coredns -o yaml

# --- LB provider + what's exposed (is 10.x.x.81 MetalLB? kube-vip? external?) ---
kubectl get svc -A | grep -i LoadBalancer
kubectl get ns | grep -Ei 'metallb|kube-vip'
kubectl get svc -A | grep -Ei 'ingress|nginx|traefik|istio|envoy'