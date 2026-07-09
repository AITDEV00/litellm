#!/usr/bin/env bash
# submariner-fw-ips.sh
# Collect every IP needed for the Al Ain <-> Abu Dhabi Submariner firewall request
# and print a paste-ready rule set (minimal + worst-case).
#
# Run where BOTH cluster APIs are reachable. Point each side at its kubeconfig/context:
#   AA_KUBECONFIG=~/.kube/alain.conf   AA_CTX=kubernetes-admin@adeoaiengine \
#   AUH_KUBECONFIG=~/.kube/abudhabi.conf AUH_CTX=default \
#   DRIVER=wireguard  ./submariner-fw-ips.sh
# If only one cluster is reachable from this host, set SINGLE=aa or SINGLE=auh and
# run it on each side, then combine by hand.
set -uo pipefail

AA_CTX="${AA_CTX:-kubernetes-admin@adeoaiengine}"
AUH_CTX="${AUH_CTX:-default}"
DRIVER="${DRIVER:-wireguard}"          # wireguard | ipsec
SINGLE="${SINGLE:-}"                    # aa | auh | (empty = both)
AA_GW="${AA_GW:-}"                      # optional explicit gateway IP if not labelled yet
AUH_GW="${AUH_GW:-}"

aa(){ kubectl ${AA_KUBECONFIG:+--kubeconfig "$AA_KUBECONFIG"} --context "$AA_CTX" "$@"; }
auh(){ kubectl ${AUH_KUBECONFIG:+--kubeconfig "$AUH_KUBECONFIG"} --context "$AUH_CTX" "$@"; }

TUNNEL_PORTS() { [ "$DRIVER" = wireguard ] && echo "UDP 51820" || echo "UDP 4500, UDP 4490, UDP 500"; }

subnets(){ awk -F. 'NF>=4{print $1"."$2"."$3".0/24"}' | sort -u; }
join_c(){ paste -sd, - ; }

gather(){  # $1=label  $2=kubectl-fn
  local L="$1" K="$2" T="/tmp/fw_${1}"
  echo "############ ${L^^} ############" >&2
  if ! $K get nodes >/dev/null 2>&1; then echo "  !! ${L}: cluster not reachable via its context" >&2; return 1; fi

  # NAME(1) STATUS(2) ROLES(3) AGE(4) VER(5) INTERNAL-IP(6)
  $K get nodes -o wide --no-headers 2>/dev/null | awk '{print $6"\t"$3"\t"$1}' > "${T}_nodes"
  awk -F'\t' '{print $1}'                      "${T}_nodes" | grep -E '^[0-9]' > "${T}_all_ips"
  awk -F'\t' '$2=="<none>"{print $1}'          "${T}_nodes" | grep -E '^[0-9]' > "${T}_worker_ips"
  awk -F'\t' '$2!="<none>"{print $1}'          "${T}_nodes" | grep -E '^[0-9]' > "${T}_cp_ips"

  # API / broker endpoint (what goes in broker.server)
  $K -n default get endpoints kubernetes \
     -o jsonpath='{range .subsets[*].addresses[*]}{.ip}{"\n"}{end}' 2>/dev/null > "${T}_api_ips"
  local APIPORT; APIPORT="$($K -n default get endpoints kubernetes \
     -o jsonpath='{.subsets[0].ports[0].port}' 2>/dev/null)"; echo "${APIPORT:-6443}" > "${T}_api_port"

  # Submariner gateway-labelled nodes (may be empty pre-join)
  $K get nodes -l submariner.io/gateway=true -o wide --no-headers 2>/dev/null \
     | awk '{print $6}' | grep -E '^[0-9]' > "${T}_gw_ips" || true

  # Pod / Service CIDRs -- for the DO-NOT-ROUTE warning only
  { $K -n kube-system get pod -l component=kube-controller-manager -o yaml 2>/dev/null \
       | grep -oE 'cluster-cidr=[0-9./]+' | cut -d= -f2 | head -1
    $K -n oik8s-cilium-system get cm cilium-config -o jsonpath='{.data.cluster-pool-ipv4-cidr}' 2>/dev/null
  } | grep -E '^[0-9]' | head -1 > "${T}_pod_cidr"
  { $K get servicecidr -o jsonpath='{.items[0].spec.cidrs[0]}' 2>/dev/null
    $K -n kube-system get pod -l component=kube-apiserver -o yaml 2>/dev/null \
       | grep -oE 'service-cluster-ip-range=[0-9./]+' | cut -d= -f2 | head -1
  } | grep -E '^[0-9]' | head -1 > "${T}_svc_cidr"

  echo "  ${L}: $(wc -l < "${T}_all_ips") nodes, $(wc -l < "${T}_worker_ips") workers, API $(paste -sd, "${T}_api_ips"):$(cat "${T}_api_port")" >&2
}

show(){  # $1=label
  local L="$1" T="/tmp/fw_${1}"
  echo "===== ${L^^} ====="
  echo "node subnets   : $(subnets < "${T}_all_ips" | join_c)"
  echo "all node IPs   : $(join_c < "${T}_all_ips")"
  echo "worker IPs     : $(join_c < "${T}_worker_ips")"
  echo "control-plane  : $(join_c < "${T}_cp_ips")"
  echo "API/broker     : $(join_c < "${T}_api_ips"):$(cat "${T}_api_port")"
  local gw; gw="$(join_c < "${T}_gw_ips")"
  echo "gateway (label): ${gw:-<none labelled yet>}"
  echo "pod CIDR       : $(cat "${T}_pod_cidr" 2>/dev/null)   <-- DO NOT ROUTE / DO NOT WHITELIST"
  echo "service CIDR   : $(cat "${T}_svc_cidr" 2>/dev/null)   <-- DO NOT ROUTE / DO NOT WHITELIST"
  echo
}

# ---- collect ----
[ "$SINGLE" = auh ] || gather aa  aa
[ "$SINGLE" = aa  ] || gather auh auh
echo

[ "$SINGLE" = auh ] || show aa
[ "$SINGLE" = aa  ] || show auh

# ---- firewall request (needs both sides) ----
if [ -z "$SINGLE" ] && [ -s /tmp/fw_aa_all_ips ] && [ -s /tmp/fw_auh_all_ips ]; then
  AA_SUB="$(subnets < /tmp/fw_aa_all_ips | join_c)"
  AUH_SUB="$(subnets < /tmp/fw_auh_all_ips | join_c)"
  AA_GW_IP="${AA_GW:-$(cat /tmp/fw_aa_gw_ips 2>/dev/null | head -1)}"
  AUH_GW_IP="${AUH_GW:-$(cat /tmp/fw_auh_gw_ips 2>/dev/null | head -1)}"
  AUH_API="$(join_c < /tmp/fw_auh_api_ips):$(cat /tmp/fw_auh_api_port)"
  AA_WORKERS="$(join_c < /tmp/fw_aa_worker_ips)"
  AUH_WORKERS="$(join_c < /tmp/fw_auh_worker_ips)"
  PORTS="$(TUNNEL_PORTS)"

  cat <<EOF
################################################################
# FIREWALL REQUEST  (Al Ain -> Abu Dhabi, Al Ain initiates)     
# cable driver: ${DRIVER}   tunnel ports: ${PORTS}
################################################################

--- MINIMAL (recommended: gateway <-> gateway) ---
 1. TUNNEL : ${AA_GW_IP:-<label AA gateway or set AA_GW>}  ->  ${AUH_GW_IP:-<label AUH gateway or set AUH_GW>}   ${PORTS}
 2. BROKER : ${AA_SUB}  ->  ${AUH_API}   TCP 6443
 3. HEALTH : ${AA_SUB}  ->  ${AUH_GW_IP:-<auh gw>}   ICMP echo   (optional; else --health-check=false)

--- WORST CASE (all worker nodes both sides) ---
 1. TUNNEL : ${AA_SUB}  ->  ${AUH_SUB}   ${PORTS}
 2. BROKER : ${AA_SUB}  ->  ${AUH_API}   TCP 6443
 3. HEALTH : ${AA_SUB}  ->  ${AUH_SUB}   ICMP echo

--- WORST CASE, explicit host IPs (if they refuse /24s) ---
 TUNNEL sources (AA workers): ${AA_WORKERS}
 TUNNEL dests   (AUH workers): ${AUH_WORKERS}
 BROKER dest    (AUH API)    : ${AUH_API}

NOTES for the network team:
 * Stateful firewall assumed: only the Al Ain->Abu Dhabi direction above is needed;
   return traffic rides the established flows. No inbound rule into Al Ain required.
 * Do NOT route or whitelist the pod/service CIDRs (10.42.0.0/16 / 10.43.0.0/16).
   All model traffic travels ENCAPSULATED inside the gateway tunnel above.
 * IPsec only: if using ESP (not UDP-encap), also allow IP protocol 50 between the
   two gateway IPs -- or pin ipsec.forceUDPEncaps=true to stay inside UDP 4500.
 * VXLAN 4800/UDP and metrics ports are INTRA-cluster (within each DC) -- not here.
EOF
else
  echo "(Single-cluster mode or one side unreachable: run on the other host too, then combine.)"
fi