# OICM Al Ain <-> Abu Dhabi Cluster Interconnect (Submariner)

Submariner 0.24.0 deployed across two on-prem Kubernetes clusters using the WireGuard
cable driver. This directory holds the deployment guides, discovery scripts, image-mirror
Makefile, and tooling tarballs needed to reproduce the setup on air-gapped bastions.

Clusters:

| Cluster   | Context                       | CNI      | GlobalCIDR      | Gateway node          |
|-----------|-------------------------------|----------|-----------------|-----------------------|
| Al Ain    | kubernetes-admin@adeoaiengine | Cilium   | 242.0.1.0/24    | adeo-gpu-03 (10.34.104.19) |
| Abu Dhabi | default (RKE2)               | Canal    | 242.0.0.0/24    | prd-oi-k8worker01 (10.10.128.72) |

## Directory contents

| File / Dir                           | Purpose                                                  |
|--------------------------------------|----------------------------------------------------------|
| `submariner-ALAIN-guide.md`          | Step-by-step Al Ain deployment guide                     |
| `submariner-ABUDHABI-guide.md`       | Step-by-step Abu Dhabi deployment guide                  |
| `Makefile`                           | Pull / tag / push the 8 Submariner images to Harbor      |
| `ad-oicm-submariner-all-config.sh`   | Dump all Submariner CRs, endpoints, gateways, pods       |
| `discover-k8-oicm-alain-egress.sh`   | Discover Al Ain egress IPs and firewall path             |
| `discover-k8-oicm-intraconnect-v2.sh`| Discover intra-cluster connectivity (v2)                 |
| `discover-k8-oicm-intraconnect.sh`   | Discover intra-cluster connectivity                      |
| `discover-k8-oicm-ip-rules.sh`       | Discover iptables / IP rules on gateway nodes            |
| `export-submariner-creds-abudhabi.sh`| Export broker credentials from Abu Dhabi for Al Ain join |
| `submariner-operator/`               | Full clone of `submariner-io/submariner-operator` at tag `v0.24.0` (see below) |

## Download dependencies (on an internet-connected host)

The bastion hosts (`prd-oi-bstn` for Abu Dhabi, `aitdev00` for Al Ain) are air-gapped.
Download everything below on a machine with internet, then transfer the files in.

### 1. CLI tools

```bash
# Helm 3.16.3
curl -fLO https://get.helm.sh/helm-v3.16.3-linux-amd64.tar.gz

# subctl 0.24 (release asset URL; verify with the GitHub API if it moves)
curl -fsSL https://api.github.com/repos/submariner-io/subctl/releases \
  | grep -E 'browser_download_url.*linux-amd64' | grep -E '0\.24' | head
curl -fLO https://github.com/submariner-io/subctl/releases/download/subctl-release-0.24/subctl-release-0.24-linux-amd64.tar.xz
```

Install after transfer (no root needed):

```bash
tar -xzf helm-v3.16.3-linux-amd64.tar.gz
mkdir -p ~/bin && install -m 0755 linux-amd64/helm ~/bin/helm

tar -xJf subctl-release-0.24-linux-amd64.tar.xz
install -m 0755 "$(find . -maxdepth 2 -name subctl -type f | head -1)" ~/bin/subctl

export PATH="$HOME/bin:$PATH"
helm version && subctl version
```

### 2. Helm charts

```bash
helm repo add submariner-latest https://submariner-io.github.io/submariner-charts/charts
helm repo update
helm pull submariner-latest/submariner-k8s-broker --version 0.24.0
helm pull submariner-latest/submariner-operator   --version 0.24.0
```

This produces `submariner-k8s-broker-0.24.0.tgz` and `submariner-operator-0.24.0.tgz`.

### 3. submariner-operator source (for RBAC manifests)

The Al Ain guide §7 requires the globalnet RBAC manifests that the Helm chart does not
ship. Clone the operator source at the matching tag:

```bash
git clone https://github.com/submariner-io/submariner-operator
cd submariner-operator
git checkout v0.24.0
```

The relevant files live under `config/rbac/submariner-globalnet/`:

```
config/rbac/submariner-globalnet/
  service_account.yaml
  role.yaml
  role_binding.yaml
  cluster_role.yaml
  cluster_role_binding.yaml
```

### 4. Container images (8 total)

Pull from `quay.io/submariner`, tag for your Harbor, and push. The `Makefile`
automates this; see the next section.

## Mirror images to Harbor

The 8 operand images for Submariner 0.24.0 (there is no `metrics-proxy` image; the
operator maps it to `nettest`):

```
submariner-operator   submariner-gateway      submariner-route-agent
submariner-globalnet  lighthouse-agent        lighthouse-coredns
nettest               subctl
```

Edit the `DST_REGISTRIES` line in the `Makefile` to match your Harbor hosts, then:

```bash
make all      # pull + login + tag + push
make verify   # confirm every image is present in both registries
```

Or run individual targets: `make pull`, `make login`, `make tag`, `make push`.

The defaults in the Makefile are:

```makefile
TAG            ?= 0.24.0
SRC_REGISTRY   := quay.io/submariner
DST_REGISTRIES := registry.adeoaiengine.ecouncil.ae harbor.ai.ecouncil.ae
```

## Deployment order

1. Follow `submariner-ABUDHABI-guide.md` first; it deploys the broker and the Abu Dhabi
   operator. Note the PSK it sets; Al Ain must use the same one.
2. Open firewall: bidirectional UDP 4500 + 4490 between `10.34.104.19` and
   `10.10.128.72`, and Al Ain to `10.10.128.71:6443` TCP.
3. Follow `submariner-ALAIN-guide.md` to join Al Ain to the broker.
4. On the Al Ain gateway node (`adeo-gpu-03`), allow UDP 4800 on the host firewall
   (Submariner intra-cluster VXLAN). The node runs UFW with a default DROP policy:
   ```bash
   sudo ufw allow from 10.34.104.0/24 to any port 4800 proto udp
   sudo ufw allow from any to any port 4800 proto udp
   ```
5. If the `submariner-globalnet` DaemonSet is stuck pending, create the 5 RBAC
   resources from `submariner-operator/config/rbac/submariner-globalnet/` and restart it:
   ```bash
   kubectl -n submariner-operator apply -f submariner-operator/config/rbac/submariner-globalnet/
   kubectl -n submariner-operator rollout restart ds submariner-globalnet
   ```
6. Verify the tunnel:
   ```bash
   subctl show connections        # STATUS: connected, RTT ~4-5ms
   sudo wg show                   # latest handshake seconds ago, transfer counters
   ping 242.0.0.254               # Abu Dhabi globalnet gateway IP
   ```
