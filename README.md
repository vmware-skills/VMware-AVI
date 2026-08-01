<!-- mcp-name: io.github.vmware-skills/vmware-avi -->
# VMware AVI

> **Author**: Wei Zhou, VMware by Broadcom — wei-wz.zhou@broadcom.com
> This is a community-driven project by a VMware engineer, not an official VMware product.
> For official VMware developer tools see [developer.broadcom.com](https://developer.broadcom.com).

English | [中文](README-CN.md)

AVI (NSX Advanced Load Balancer) management and AKO Kubernetes operations tool — 28 tools across 10 categories.

> **Dual mode**: Traditional AVI Controller management + AKO K8s operations in one skill.
>
> **Companion skills** handle everything else:
>
> | Skill | Scope | Install |
> |-------|-------|---------|
> | **[vmware-aiops](https://github.com/vmware-skills/VMware-AIops)** | VM lifecycle, deployment, guest ops, cluster | `uv tool install vmware-aiops` |
> | **[vmware-monitor](https://github.com/vmware-skills/VMware-Monitor)** | Read-only: inventory, health, alarms, events | `uv tool install vmware-monitor` |
> | **[vmware-storage](https://github.com/vmware-skills/VMware-Storage)** | Datastores, iSCSI, vSAN management | `uv tool install vmware-storage` |
> | **[vmware-vks](https://github.com/vmware-skills/VMware-VKS)** | Tanzu Namespaces, TKC cluster lifecycle | `uv tool install vmware-vks` |
> | **[vmware-nsx](https://github.com/vmware-skills/VMware-NSX)** | NSX networking: segments, gateways, NAT | `uv tool install vmware-nsx-mgmt` |
> | **[vmware-nsx-security](https://github.com/vmware-skills/VMware-NSX-Security)** | DFW firewall rules, security groups | `uv tool install vmware-nsx-security` |
> | **[vmware-aria](https://github.com/vmware-skills/VMware-Aria)** | Aria Ops: metrics, alerts, capacity | `uv tool install vmware-aria` |

[![PyPI](https://img.shields.io/pypi/v/vmware-avi)](https://pypi.org/project/vmware-avi/)
[![Python](https://img.shields.io/pypi/pyversions/vmware-avi)](https://pypi.org/project/vmware-avi/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![ClawHub](https://img.shields.io/badge/ClawHub-vmware--avi-orange)](https://clawhub.ai/skills/vmware-avi)

---

## Quick Install

```bash
# Via uv (recommended)
uv tool install vmware-avi

# Or via pip
pip install vmware-avi

# China mainland mirror
pip install vmware-avi -i https://pypi.tuna.tsinghua.edu.cn/simple

# Verify installation
vmware-avi doctor
```

### Offline / Air-Gapped Install (from source)

This project uses the modern PEP 517 build system (hatchling), so there is **no
`setup.py`** by design — that is expected, not a missing file. If you cloned the
source and hit `ERROR: File "setup.py" or "setup.cfg" not found ... editable mode
currently requires a setuptools-based build`, your `pip` is older than 21.3 and
cannot do an *editable* (`-e`) install with a non-setuptools backend. Editable
mode is a developer convenience, not needed to run the tool — do one of:

```bash
# From the source tree — a normal (non-editable) install builds a wheel:
pip install .              # NOT  pip install -e .

# ...or upgrade pip first, and editable works too:
pip install --upgrade pip && pip install -e .
```

For a **truly air-gapped host**, build the wheels on a connected machine and copy
them over — the target then needs no network:

```bash
# On a connected machine, collect this package + its dependencies as wheels:
pip wheel . -w dist        # → dist/*.whl   (or: uv build, for just this package)

# Copy dist/ to the air-gapped host, then install offline:
pip install --no-index --find-links dist vmware-avi
```

---

## Capabilities Overview

### What This Skill Does

| Category | Tools | Count |
|----------|-------|:-----:|
| **Virtual Service** | list, status, enable/disable | 3 |
| **Pool Member** | pool discovery, member list, enable/disable member (drain/restore traffic) | 4 |
| **SSL Certificate** | list, expiry check | 2 |
| **Analytics** | VS metrics overview, request error logs | 2 |
| **Service Engine** | list, health check | 2 |
| **AKO Pod Ops** | status, logs, restart, version info | 4 |
| **AKO Config** | values.yaml view, Helm diff, Helm upgrade | 3 |
| **Ingress Diagnostics** | annotation validation, VS mapping, error diagnosis, fix recommendation | 4 |
| **Sync Diagnostics** | K8s-Controller comparison, inconsistency list, force resync | 3 |
| **Multi-cluster** | cluster list, cross-cluster AKO overview, AMKO status | 3 |

### CLI vs MCP: Which Mode to Use

| Scenario | Recommended | Why |
|----------|:-----------:|-----|
| **Local/small models** (Ollama, Qwen) | **CLI** | ~2K tokens vs ~8K for MCP |
| **Cloud models** (Claude, GPT-4o) | Either | MCP gives structured JSON I/O |
| **Automated pipelines** | **MCP** | Type-safe parameters, structured output |
| **AKO troubleshooting** | **CLI** | Interactive log tailing, Helm diff output |

> **Rule of thumb**: Use CLI for cost efficiency and small models. Use MCP for structured automation with large models.

### Architecture

```
User (Natural Language)
  |
AI CLI Tool (Claude Code / Gemini / Codex / Cursor / Trae)
  | reads SKILL.md
  |
vmware-avi CLI
  |--- avisdk (AVI REST API) ---> AVI Controller ---> Virtual Services / Pools / SEs
  |--- kubectl / kubernetes ---> K8s Cluster ---> AKO Pods / Ingress / Services
```

---

## Configuration

### Step 1: Create Config Directory

```bash
mkdir -p ~/.vmware-avi
vmware-avi init          # generates config.yaml and .env templates
chmod 600 ~/.vmware-avi/.env
```

### Step 2: Edit config.yaml

```yaml
controllers:
  - name: prod-avi
    host: avi-controller.example.com
    username: admin
    api_version: "22.1.4"
    tenant: admin
    port: 443
    verify_ssl: true

default_controller: prod-avi

ako:
  kubeconfig: ~/.kube/config
  default_context: ""
  namespace: avi-system
```

### Step 3: Set Passwords

Create `~/.vmware-avi/.env`:

```bash
# AVI Controller passwords
# Format: {CONTROLLER_NAME_UPPER}_PASSWORD
PROD_AVI_PASSWORD=your-password-here
```

Password environment variable naming convention:
```
{CONTROLLER_NAME_UPPER}_PASSWORD
# Replace hyphens with underscores, UPPERCASE
# Example: controller "prod-avi" -> PROD_AVI_PASSWORD
# Example: controller "staging-alb" -> STAGING_ALB_PASSWORD
```

### Step 4: Verify

```bash
vmware-avi doctor    # checks Controller connectivity + kubeconfig + avisdk
```

---

## CLI Usage

### Virtual Service Management

```bash
# List all virtual services
vmware-avi vs list [--controller prod-avi]

# Check status of a specific VS
vmware-avi vs status my-webapp-vs

# Enable / disable a VS (disable requires double confirmation)
vmware-avi vs enable my-webapp-vs
vmware-avi vs disable my-webapp-vs
```

### Pool Member Drain / Restore

```bash
# List pool members and health status
vmware-avi pool members my-pool

# Graceful drain (disable) — double confirmation required
vmware-avi pool disable my-pool 10.1.1.5

# Restore traffic (enable)
vmware-avi pool enable my-pool 10.1.1.5
```

### SSL Certificate Expiry Check

```bash
# List all certificates
vmware-avi ssl list

# Check certificates expiring within 30 days
vmware-avi ssl expiry --days 30
```

### Analytics and Error Logs

```bash
# VS analytics: throughput, latency, error rates
vmware-avi analytics my-webapp-vs

# Request error logs
vmware-avi logs my-webapp-vs --since 1h
```

### Service Engine Health

```bash
# Name, mgmt IP, operational status, SE group — status sourced from the
# serviceengine-inventory endpoint (config + runtime merged)
vmware-avi se list

# Per-SE operational status + connected-VS counts
vmware-avi se health
```

### AKO Troubleshooting

```bash
# Check AKO pod status
vmware-avi ako status [--context my-k8s-context]

# View AKO logs
vmware-avi ako logs [--tail 100] [--since 30m]

# Restart AKO pod (double confirmation)
vmware-avi ako restart

# Show AKO version
vmware-avi ako version
```

### AKO Helm Config Management

The AKO Helm release is discovered automatically (official installs use
`helm install --generate-name`, so the release is not named `ako`). Upgrades pull
the official Broadcom OCI chart
`oci://projects.packages.broadcom.com/ako/helm-charts/ako` with `--reuse-values`.

```bash
# View current AKO Helm values (release auto-discovered)
vmware-avi ako config show

# Show pending changes (diff against the official OCI chart)
vmware-avi ako config diff

# Helm upgrade (double confirmation + --dry-run default)
vmware-avi ako config upgrade
```

### Ingress Diagnostics

```bash
# Validate Ingress annotations
vmware-avi ako ingress check <namespace>

# Show Ingress-to-VS mapping
vmware-avi ako ingress map

# Diagnose why an Ingress has no VS
vmware-avi ako ingress diagnose <ingress-name>
```

### Sync Diagnostics

```bash
# Check K8s-Controller sync status
vmware-avi ako sync status

# Show inconsistencies between K8s and Controller
vmware-avi ako sync diff

# Force AKO resync (double confirmation)
vmware-avi ako sync force
```

### Multi-cluster AKO

```bash
# List clusters with AKO deployed
vmware-avi ako clusters

# Cross-cluster AKO status overview
vmware-avi ako cluster-overview

# AMKO GSLB status
vmware-avi ako amko status
```

---

## MCP Server

The MCP server exposes all 28 tools via the [Model Context Protocol](https://modelcontextprotocol.io). Works with any MCP-compatible client.

**After `uv tool install vmware-avi`, start the MCP server with one command** (v1.5.15+):

```bash
# Recommended — single command, no network re-resolve
vmware-avi mcp

# With custom config path
VMWARE_AVI_CONFIG=/path/to/config.yaml vmware-avi mcp
```

### Claude Desktop Config

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "vmware-avi": {
      "command": "vmware-avi",
      "args": ["mcp"],
      "env": {
        "VMWARE_AVI_CONFIG": "~/.vmware-avi/config.yaml"
      }
    }
  }
}
```

<details>
<summary>Alternative: uvx (no install) or legacy entry point</summary>

```bash
# Run without installing (requires PyPI access each launch)
uvx --from vmware-avi vmware-avi mcp

# Legacy entry point (still works, kept for backward compatibility)
vmware-avi-mcp
```

> **Behind a corporate TLS proxy?** uvx may fail with `invalid peer certificate: UnknownIssuer`.
> Use the recommended `vmware-avi mcp` form above (no network needed), or set `UV_NATIVE_TLS=true`.

</details>

### MCP Tools (28)

| Category | Tools |
|----------|-------|
| Virtual Service (3) | `vs_list`, `vs_status`, `vs_toggle` |
| Pool Member (4) | `pool_list`, `pool_members`, `pool_member_enable`, `pool_member_disable` |
| SSL Certificate (2) | `ssl_list`, `ssl_expiry_check` |
| Analytics (2) | `vs_analytics`, `vs_error_logs` |
| Service Engine (2) | `se_list`, `se_health` |
| AKO Pod (4) | `ako_status`, `ako_logs`, `ako_restart`, `ako_version` |
| AKO Config (3) | `ako_config_show`, `ako_config_diff`, `ako_config_upgrade` |
| Ingress Diagnostics (3) | `ako_ingress_check`, `ako_ingress_map`, `ako_ingress_diagnose` |
| Sync Diagnostics (3) | `ako_sync_status`, `ako_sync_diff`, `ako_sync_force` |
| Multi-cluster (2) | `ako_clusters`, `ako_amko_status` |

---

## Common Workflows

### 1. Maintenance Window -- Drain a Pool Member

When taking a backend server offline for patching:

1. List pool members and health status
   ```bash
   vmware-avi pool members my-pool
   ```
2. Disable the target server (graceful drain)
   ```bash
   vmware-avi pool disable my-pool 10.1.1.5
   ```
3. Monitor analytics to confirm active connections are draining
   ```bash
   vmware-avi analytics my-vs
   ```
4. Perform maintenance on the server
5. Re-enable the server
   ```bash
   vmware-avi pool enable my-pool 10.1.1.5
   ```
6. Verify health status is green
   ```bash
   vmware-avi pool members my-pool
   ```

### 2. AKO Ingress Not Creating VS

When a developer reports their Ingress is not producing a Virtual Service:

1. Verify AKO is running
   ```bash
   vmware-avi ako status
   ```
2. Validate Ingress annotations
   ```bash
   vmware-avi ako ingress check <namespace>
   ```
3. Check sync status between K8s and Controller
   ```bash
   vmware-avi ako sync status
   ```
4. If annotations are wrong, diagnose the specific Ingress
   ```bash
   vmware-avi ako ingress diagnose <ingress-name>
   ```
5. If sync drift is detected, review the diff and force resync if needed
   ```bash
   vmware-avi ako sync diff
   vmware-avi ako sync force
   ```

### 3. SSL Certificate Expiry Audit

Expired certificates cause outages. Run periodic checks:

1. Check all certificates expiring within 30 days
   ```bash
   vmware-avi ssl expiry --days 30
   ```
2. Review which VS uses each expiring certificate (output includes VS mapping)
3. Plan renewal with the certificate team
4. After renewal, verify the new certificate is in place
   ```bash
   vmware-avi ssl list
   ```

---

## Troubleshooting

### "Controller unreachable" error

1. Run `vmware-avi doctor` to verify connectivity
2. Check if the controller address and port are correct in `~/.vmware-avi/config.yaml`
3. For self-signed certs: set `verify_ssl: false` in config.yaml (lab environments only)

### AKO Pod in CrashLoopBackOff

1. Check logs: `vmware-avi ako logs --tail 50`
2. Common causes: wrong controller IP in values.yaml, network policy blocking AKO to Controller, expired credentials
3. Fix config: `vmware-avi ako config show` to inspect, then `vmware-avi ako config upgrade` with corrected values (release auto-discovered; pulls the official Broadcom OCI chart)

### Ingress created but no VS on Controller

1. Validate annotations: `vmware-avi ako ingress check <namespace>`
2. Check AKO logs for rejection reason: `vmware-avi ako logs --since 5m`
3. Run sync diff: `vmware-avi ako sync diff` to see if the object is stuck

### Pool member shows "down" after enable

Health monitor may still be failing. The member is enabled but unhealthy. Check the actual health status on the Controller side. Fix the backend service first, then the health status will auto-recover.

### SSL expiry check shows 0 certificates

Verify the controller connection has tenant-level access. Certificates are tenant-scoped in AVI. The configured user may only see certs in their tenant.

### AKO sync force has no effect

Force resync triggers AKO to re-reconcile all K8s objects. If the drift persists, the issue is likely in the K8s resource definition itself (bad annotation, missing secret). Use `vmware-avi ako ingress diagnose` to pinpoint the root cause.

---

## Safety Features

| Feature | Details |
|---------|---------|
| **Double Confirmation** | Destructive ops (VS disable, pool member disable, AKO restart, Helm upgrade, force resync) require 2 sequential confirmations |
| **Dry-Run Default** | `ako config upgrade` defaults to `--dry-run` mode -- user must explicitly confirm to apply |
| **Audit Trail** | All operations logged to `~/.vmware/audit.db` via vmware-policy (`@vmware_tool` decorator) |
| **Password Protection** | `.env` file loading with permission check; never in shell history |
| **SSL Support** | `verify_ssl: false` for self-signed certs in isolated lab environments only |
| **Prompt Injection Protection** | All API-sourced text truncated (500 chars max) and C0/C1 control characters stripped |
| **Input Validation** | Pool names, VS names, IP addresses, and namespace names validated before API calls |

### Security Details

- **Source Code**: [github.com/vmware-skills/VMware-AVI](https://github.com/vmware-skills/VMware-AVI)
- **Config File Contents**: `config.yaml` stores controller addresses, usernames, and AKO settings. No passwords or tokens. All secrets stored exclusively in `.env`
- **Webhook Data Scope**: Disabled by default. No third-party data transmission
- **TLS Verification**: Enabled by default. Disable only for self-signed certificate environments
- **Prompt Injection Protection**: `_sanitize()` truncation + control character cleanup on all AVI API responses
- **Least Privilege**: Use a dedicated AVI service account with minimal permissions. AKO operations require only namespace-scoped kubeconfig access

---

## Companion Skills

| Skill | Scope | Tools | Install |
|-------|-------|:-----:|---------|
| **[vmware-avi](https://github.com/vmware-skills/VMware-AVI)** | AVI load balancer, AKO K8s operations | 28 | `uv tool install vmware-avi` |
| **[vmware-aiops](https://github.com/vmware-skills/VMware-AIops)** | VM lifecycle, deployment, guest ops, cluster | 49 | `uv tool install vmware-aiops` |
| **[vmware-monitor](https://github.com/vmware-skills/VMware-Monitor)** | Read-only monitoring, alarms, events | 27 | `uv tool install vmware-monitor` |
| **[vmware-storage](https://github.com/vmware-skills/VMware-Storage)** | Datastores, iSCSI, vSAN | 11 | `uv tool install vmware-storage` |
| **[vmware-vks](https://github.com/vmware-skills/VMware-VKS)** | Tanzu Namespaces, TKC cluster lifecycle | 20 | `uv tool install vmware-vks` |
| **[vmware-nsx](https://github.com/vmware-skills/VMware-NSX)** | NSX segments, gateways, NAT, routing | 33 | `uv tool install vmware-nsx-mgmt` |
| **[vmware-nsx-security](https://github.com/vmware-skills/VMware-NSX-Security)** | DFW firewall, security groups, IDS/IPS | 21 | `uv tool install vmware-nsx-security` |
| **[vmware-aria](https://github.com/vmware-skills/VMware-Aria)** | Aria Ops: metrics, alerts, capacity | 28 | `uv tool install vmware-aria` |
| **[vmware-harden](https://github.com/vmware-skills/VMware-Harden)** | Compliance baselines, drift detection | 6 | `uv tool install vmware-harden` |

---

## Version Compatibility

| AVI Controller / Environment | Support | Notes |
|------------------------------|---------|-------|
| AVI 30.x in VCF 9.1 | ✅ Full | avisdk 30.x line covers VCF 9.1 bundle |
| AVI 30.x in VCF 9.0 | ✅ Full | Standard AVI / NSX ALB integration |
| AVI 22.x — 31.x standalone | ✅ Full | Pin `avisdk>=22.1,<31.0` |
| AKO 1.10+ | ✅ Full | Kubernetes integration via AKO ConfigMap / GatewayClass |

#### Official Broadcom References

- **SDKs**: <https://developer.broadcom.com/sdks> — VCF Python SDK
- **REST APIs**: <https://developer.broadcom.com/xapis> — AVI Controller REST API
- **CLI Tools**: <https://developer.broadcom.com/tools> — VCF PowerCLI 9.1

---

## Troubleshooting & Contributing

If you encounter any errors or issues, please send the error message, logs, or screenshots to **zhouwei008@gmail.com**. Contributions are welcome -- feel free to join us in maintaining and improving this project!

## License

MIT
