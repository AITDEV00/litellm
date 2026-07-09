{
echo "###### ABU DHABI SUBMARINER CONFIG DUMP ######"
echo "### date: $(date -u)"
echo; echo "===== SUBMARINER CR ====="
kubectl -n submariner-operator get submariner submariner -o yaml
echo; echo "===== ENDPOINTS (local view) ====="
kubectl -n submariner-operator get endpoints.submariner.io -o yaml
echo; echo "===== GATEWAYS ====="
kubectl -n submariner-operator get gateways.submariner.io -o yaml
echo; echo "===== CLUSTERS ====="
kubectl -n submariner-operator get clusters.submariner.io -o yaml
echo; echo "===== BROKER: registered clusters ====="
kubectl -n submariner-k8s-broker get clusters.submariner.io -o yaml
echo; echo "===== BROKER: registered endpoints (BOTH clusters) ====="
kubectl -n submariner-k8s-broker get endpoints.submariner.io -o yaml
echo; echo "===== PODS ====="
kubectl -n submariner-operator get pods -o wide
echo; echo "===== IMAGES ====="
kubectl -n submariner-operator get pods -o jsonpath='{range .items[*]}{.spec.containers[*].image}{"\n"}{end}' | sort -u
echo; echo "===== NAMESPACE PSA LABELS ====="
kubectl get ns submariner-operator -o jsonpath='{.metadata.labels}'; echo
echo; echo "===== GATEWAY NODE LABEL ====="
kubectl get nodes -l submariner.io/gateway=true -o wide
echo; echo "===== subctl show all ====="
subctl show all
} > /tmp/abudhabi-submariner-dump.txt 2>&1
echo "written: /tmp/abudhabi-submariner-dump.txt ($(wc -l < /tmp/abudhabi-submariner-dump.txt) lines)"