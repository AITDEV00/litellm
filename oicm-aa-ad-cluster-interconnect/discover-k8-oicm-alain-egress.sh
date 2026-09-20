#!/usr/bin/env bash
# alain-egress-feasibility.sh
# Answers ONE question: can an Al Ain gateway node be configured to reach Abu Dhabi
# (API server 6443 for the Broker + the tunnel port for Submariner)?
#
# It does NOT change anything permanent. It launches short-lived diagnostic pods
# and deletes them. The reachability probe runs in the GATEWAY NODE's host network
# namespace (hostNetwork) because Submariner's tunnel originates from the node IP,
# NOT from a Cilium-masqueraded pod IP -- a normal pod test would mislead you.
#
# NOTE: this script cannot see Nutanix Flow (hypervisor microsegmentation). Flow is
# enforced outside the guest, so a Flow block would pass every check below EXCEPT the
# live packet probe in PART 2 -- which is therefore the authoritative verdict.
set -uo pipefail

# ------------------------------- knobs --------------------------------------
HARBOR_IMAGE="${HARBOR_IMAGE:-<HARBOR>/library/python:3.12-slim}"
CTX="${CTX:-kubernetes-admin@adeoaiengine}"     # Al Ain context
NS="${NS:-mlops}"                               # namespace to launch probes in
GATEWAY_NODE="${GATEWAY_NODE:-adeo-storage-01}" # intended Submariner gateway (NOT a GPU node)
AUH_API_IP="${AUH_API_IP:-10.10.128.71}"        # Abu Dhabi API server / kube-vip VIP
AUH_API_PORT="${AUH_API_PORT:-6443}"
AUH_GW_IP="${AUH_GW_IP:-10.10.128.72}"          # Abu Dhabi gateway-candidate node IP
RUN_HOST_CHECKS="${RUN_HOST_CHECKS:-1}"         # 1 = try privileged host firewall/WG check
K="kubectl --context ${CTX}"

echo "############################################################"
echo "# Al Ain egress feasibility  ->  Abu Dhabi"
echo "#   gateway node : ${GATEWAY_NODE}"
echo "#   AUH API      : ${AUH_API_IP}:${AUH_API_PORT}"
echo "#   AUH gateway  : ${AUH_GW_IP}  (UDP 4500/500 IPsec, 51820 WireGuard)"
echo "############################################################"

# ---- context sanity: do not get fooled into testing the wrong cluster ------
CUR="$(${K} config current-context 2>/dev/null)"
echo "[ctx] current-context=${CUR}"
case "${CUR}" in *adeoaiengine*) : ;; *)
  echo "  !! WARNING: context does not look like Al Ain. Set CTX=... and re-run." ;;
esac

echo
echo "===================== PART 1: policy inventory ====================="
echo "(empty output = nothing at this layer is denying egress)"
echo "--- standard NetworkPolicies ---"
${K} get networkpolicies -A 2>/dev/null || true
echo "--- CiliumNetworkPolicies ---"
${K} get ciliumnetworkpolicies -A 2>/dev/null || echo "(none)"
echo "--- CiliumClusterwideNetworkPolicies ---"
${K} get ciliumclusterwidenetworkpolicies 2>/dev/null || echo "(none)"
echo "--- host firewall / enforcement mode in cilium-config ---"
${K} -n oik8s-cilium-system get cm cilium-config -o yaml 2>/dev/null \
  | grep -iE 'enable-host-firewall|policy-enforcement|enable-policy|policy-audit|masquerade' | sort -u \
  || echo "(cilium-config not readable)"
echo "--- any clusterwide policy targeting the host/nodes? (would govern gateway egress) ---"
${K} get ciliumclusterwidenetworkpolicies -o yaml 2>/dev/null \
  | grep -iE 'nodeSelector|toEntities|reserved:host|egress:' || echo "(no host-targeting rules found)"
echo "--- probe namespace egress policy ---"
${K} -n "${NS}" get networkpolicies,ciliumnetworkpolicies 2>/dev/null || echo "(none)"
echo "--- existing Submariner install? ---"
${K} get ns 2>/dev/null | grep -iE 'submariner' || echo "(no submariner namespace -- clean slate)"

echo
echo "===================== PART 2: AUTHORITATIVE reachability ====================="
echo "(runs in ${GATEWAY_NODE} host netns; source IP should be that node's IP)"
${K} run egress-probe -n "${NS}" --restart=Never -i --rm \
  --image="${HARBOR_IMAGE}" \
  --overrides='{"spec":{"hostNetwork":true,"nodeName":"'"${GATEWAY_NODE}"'","tolerations":[{"operator":"Exists"}]}}' \
  -- python3 - <<PY || echo "  !! probe pod failed to run (image pull? privileges? PSA?)"
import socket, sys

API=("${AUH_API_IP}", ${AUH_API_PORT})
GW="${AUH_GW_IP}"

def src_ip(dst):
    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    try: s.connect((dst,80)); return s.getsockname()[0]
    except Exception: return "?"
    finally: s.close()

print(f"[host] hostname={socket.gethostname()} source_ip_towards_AUH={src_ip(GW)}")

# --- TCP 6443: Broker / discovery plane -- DEFINITIVE ---
ok_api=False
try:
    s=socket.create_connection(API,timeout=6)
    print(f"[TCP ] {API[0]}:{API[1]}  OK  (src={s.getsockname()[0]})"); s.close(); ok_api=True
except Exception as e:
    print(f"[TCP ] {API[0]}:{API[1]}  FAIL  {e}")

# --- UDP tunnel ports: best-effort (no listener in AUH yet) ---
def udp(ip,port,t=3):
    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(t)
    try:
        s.connect((ip,port)); s.send(b"\\x00")
        try: s.recv(64); return "REPLY (listener present) -> path OPEN"
        except socket.timeout: return "sent, no reply (open|filtered) -> egress not blocked locally"
        except ConnectionRefusedError: return "ICMP port-unreachable -> host REACHED, path OPEN"
    except OSError as e:
        return f"SEND FAIL ({e}) -> local egress blocked / no route  [RED FLAG]"
    finally: s.close()

any_send_fail=False
for p in (4500,500,51820):
    r=udp(GW,p); print(f"[UDP ] {GW}:{p:<5} {r}")
    if "SEND FAIL" in r: any_send_fail=True

print("\\n================= VERDICT =================")
print(f"  Broker path (TCP 6443)         : {'PASS' if ok_api else 'FAIL -> Al Ain cannot reach AUH API server'}")
print(f"  Tunnel egress (local send)     : {'BLOCKED locally' if any_send_fail else 'not blocked at this node'}")
if ok_api and not any_send_fail:
    print("  => FEASIBLE from this node. Definitive tunnel-port proof still needs an AUH")
    print("     listener (run the udp listener on the AUH gateway, then re-check :4500).")
else:
    print("  => NOT yet feasible. Fix the FAIL/BLOCKED item (firewall rule or wrong target IP),")
    print("     and remember Flow microsegmentation can cause this without any in-guest sign.")
PY

if [ "${RUN_HOST_CHECKS}" = "1" ]; then
echo
echo "===================== PART 3: host firewall + WireGuard (privileged, best-effort) ====================="
echo "(if this is rejected by Pod Security, run the 3 chroot commands via SSH on ${GATEWAY_NODE} instead)"
cat <<EOF | ${K} apply -f - >/dev/null 2>&1 && \
  ( ${K} -n "${NS}" wait --for=condition=Ready pod/host-egress-check --timeout=30s >/dev/null 2>&1; \
    sleep 2; ${K} -n "${NS}" logs host-egress-check 2>/dev/null; \
    ${K} -n "${NS}" delete pod host-egress-check --ignore-not-found >/dev/null 2>&1 ) \
  || echo "  !! privileged host-check pod could not run (likely PSA/PSS). Use SSH fallback below."
apiVersion: v1
kind: Pod
metadata: {name: host-egress-check, namespace: ${NS}}
spec:
  hostNetwork: true
  hostPID: true
  nodeName: ${GATEWAY_NODE}
  restartPolicy: Never
  tolerations: [{operator: Exists}]
  containers:
  - name: c
    image: ${HARBOR_IMAGE}
    securityContext: {privileged: true}
    command: ["/bin/sh","-c"]
    args:
    - |
      echo "== host OUTPUT chain (looking for DROP/REJECT) =="
      chroot /host sh -c 'iptables -S OUTPUT 2>/dev/null | grep -iE "drop|reject" || echo "OUTPUT: no drop/reject"'
      chroot /host sh -c 'nft list ruleset 2>/dev/null | grep -iE "chain output|drop|reject" | head || true'
      chroot /host sh -c 'ufw status 2>/dev/null || echo "ufw: not present"'
      echo "== WireGuard kernel support (simpler firewall = single UDP 51820) =="
      chroot /host sh -c 'ip link add wgtest type wireguard 2>/dev/null && ip link del wgtest && echo "WireGuard: OK" || echo "WireGuard: NOT available (use IPsec)"'
      echo "== IPsec/xfrm support =="
      chroot /host sh -c 'test -e /proc/sys/net/core/xfrm_acq_expires && echo "xfrm: present" || echo "xfrm: verify"'
    volumeMounts: [{name: host, mountPath: /host}]
  volumes: [{name: host, hostPath: {path: /}}]
EOF
echo
echo "SSH fallback (if the pod was blocked), run on ${GATEWAY_NODE}:"
echo "  sudo iptables -S OUTPUT | grep -iE 'drop|reject' || echo 'OUTPUT open'"
echo "  sudo ip link add wgtest type wireguard && sudo ip link del wgtest && echo 'WireGuard OK'"
echo "  ls /proc/sys/net/core/xfrm_* >/dev/null 2>&1 && echo 'xfrm present'"
fi

echo
echo "===================== REMINDERS ====================="
echo " * Flow/Prism Central microsegmentation is NOT visible here. If PART 2 says PASS,"
echo "   Flow is not blocking. If it says FAIL but firewalls look open, suspect Flow."
echo " * PART 2 is the source of truth. PART 1/3 only explain WHY if it fails."
echo " * For definitive UDP tunnel proof, start a temporary listener on the AUH gateway"
echo "   candidate (hostNetwork, udp/4500) and re-run PART 2 against it."
echo "Done."