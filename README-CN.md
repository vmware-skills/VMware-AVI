<!-- mcp-name: io.github.vmware-skills/vmware-avi -->
# VMware AVI

> **作者**: Wei Zhou, VMware by Broadcom — wei-wz.zhou@broadcom.com
> 本项目由 VMware 工程师维护的社区项目，非 VMware 官方产品。
> VMware 官方开发者工具请访问 [developer.broadcom.com](https://developer.broadcom.com)。

[English](README.md) | 中文

AVI（NSX 高级负载均衡器）管理与 AKO Kubernetes 运维工具 — 10 大类 28 个工具。

> **双模式**：传统 AVI Controller 管理 + AKO K8s 运维合二为一。
>
> **配套技能**负责其他领域：
>
> | 技能 | 范围 | 安装 |
> |------|------|------|
> | **[vmware-aiops](https://github.com/vmware-skills/VMware-AIops)** | VM 生命周期、部署、Guest Ops、集群 | `uv tool install vmware-aiops` |
> | **[vmware-monitor](https://github.com/vmware-skills/VMware-Monitor)** | 只读：资源清单、健康检查、告警、事件 | `uv tool install vmware-monitor` |
> | **[vmware-storage](https://github.com/vmware-skills/VMware-Storage)** | 数据存储、iSCSI、vSAN 管理 | `uv tool install vmware-storage` |
> | **[vmware-vks](https://github.com/vmware-skills/VMware-VKS)** | Tanzu 命名空间、TKC 集群生命周期 | `uv tool install vmware-vks` |
> | **[vmware-nsx](https://github.com/vmware-skills/VMware-NSX)** | NSX 网络：Segment、网关、NAT | `uv tool install vmware-nsx-mgmt` |
> | **[vmware-nsx-security](https://github.com/vmware-skills/VMware-NSX-Security)** | DFW 防火墙规则、安全组 | `uv tool install vmware-nsx-security` |
> | **[vmware-aria](https://github.com/vmware-skills/VMware-Aria)** | Aria Ops：指标、告警、容量 | `uv tool install vmware-aria` |

[![PyPI](https://img.shields.io/pypi/v/vmware-avi)](https://pypi.org/project/vmware-avi/)
[![Python](https://img.shields.io/pypi/pyversions/vmware-avi)](https://pypi.org/project/vmware-avi/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![ClawHub](https://img.shields.io/badge/ClawHub-vmware--avi-orange)](https://clawhub.ai/skills/vmware-avi)

---

## 快速安装

```bash
# 通过 uv 安装（推荐）
uv tool install vmware-avi

# 或通过 pip 安装
pip install vmware-avi

# 国内镜像加速
pip install vmware-avi -i https://pypi.tuna.tsinghua.edu.cn/simple

# 验证安装
vmware-avi doctor
```

### 离线 / 气隙环境安装（从源码）

本项目采用现代 PEP 517 构建系统（hatchling），因此**刻意不提供 `setup.py`**
—— 这是预期行为，不是文件缺失。如果你克隆源码后遇到
`ERROR: File "setup.py" or "setup.cfg" not found ... editable mode currently
requires a setuptools-based build`，说明你的 `pip` 版本低于 21.3，无法用非
setuptools 后端做**可编辑**（`-e`）安装。可编辑模式只是开发便利，运行工具
并不需要它 —— 二选一：

```bash
# 从源码树安装 —— 普通（非可编辑）安装会构建 wheel：
pip install .              # 不要用  pip install -e .

# ……或先升级 pip，可编辑安装同样可用：
pip install --upgrade pip && pip install -e .
```

对于**完全气隙的主机**，在联网机器上构建 wheel 再拷贝过去 —— 目标主机无需
任何网络：

```bash
# 在联网机器上，把本包及其依赖收集为 wheel：
pip wheel . -w dist        # → dist/*.whl   （或：uv build，仅构建本包）

# 把 dist/ 拷贝到气隙主机，然后离线安装：
pip install --no-index --find-links dist vmware-avi
```

---

## 功能概览

### 本技能包含

| 类别 | 工具 | 数量 |
|------|------|:----:|
| **虚拟服务** | 列表、状态、启用/禁用 | 3 |
| **池成员** | 池发现、成员列表、启用/禁用成员（流量排干/恢复） | 4 |
| **SSL 证书** | 列表、过期检查 | 2 |
| **分析** | VS 指标概览、请求错误日志 | 2 |
| **服务引擎** | 列表、健康检查 | 2 |
| **AKO Pod 运维** | 状态、日志、重启、版本信息 | 4 |
| **AKO 配置** | values.yaml 查看、Helm diff、Helm upgrade | 3 |
| **Ingress 诊断** | 注解验证、VS 映射、错误诊断、修复建议 | 4 |
| **同步诊断** | K8s-Controller 对比、不一致列表、强制同步 | 3 |
| **多集群** | 集群列表、跨集群 AKO 概览、AMKO 状态 | 3 |

### CLI 与 MCP：如何选择

| 场景 | 推荐 | 原因 |
|------|:----:|------|
| **本地/小模型**（Ollama、Qwen） | **CLI** | 约 2K token vs MCP 约 8K |
| **云端模型**（Claude、GPT-4o） | 均可 | MCP 提供结构化 JSON 输入输出 |
| **自动化流水线** | **MCP** | 类型安全参数、结构化输出 |
| **AKO 问题排查** | **CLI** | 交互式日志跟踪、Helm diff 输出 |

> **经验法则**：小模型用 CLI 省 token，大模型用 MCP 做结构化自动化。

### 架构

```
用户（自然语言）
  |
AI CLI 工具（Claude Code / Gemini / Codex / Cursor / Trae）
  | 读取 SKILL.md
  |
vmware-avi CLI
  |--- avisdk（AVI REST API）---> AVI Controller ---> 虚拟服务 / 池 / 服务引擎
  |--- kubectl / kubernetes ---> K8s 集群 ---> AKO Pod / Ingress / Service
```

---

## 配置

### 第 1 步：创建配置目录

```bash
mkdir -p ~/.vmware-avi
vmware-avi init          # 生成 config.yaml 和 .env 模板
chmod 600 ~/.vmware-avi/.env
```

### 第 2 步：编辑 config.yaml

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

### 第 3 步：设置密码

创建 `~/.vmware-avi/.env`：

```bash
# AVI Controller 密码
# 格式：VMWARE_AVI_{控制器名大写}_PASSWORD
PROD_AVI_PASSWORD=your-password-here
```

密码环境变量命名规则：
```
VMWARE_AVI_{控制器名大写}_PASSWORD
# 连字符替换为下划线，全部大写
# 示例：控制器 "prod-avi" -> PROD_AVI_PASSWORD
# 示例：控制器 "staging-alb" -> STAGING_ALB_PASSWORD
```

### 第 4 步：验证

```bash
vmware-avi doctor    # 检查 Controller 连通性 + kubeconfig + avisdk
```

---

## CLI 使用

### 虚拟服务管理

```bash
# 列出所有虚拟服务
vmware-avi vs list [--controller prod-avi]

# 查看特定 VS 状态
vmware-avi vs status my-webapp-vs

# 启用 / 禁用 VS（禁用需双重确认）
vmware-avi vs enable my-webapp-vs
vmware-avi vs disable my-webapp-vs
```

### 池成员排干/恢复

```bash
# 列出池成员及健康状态
vmware-avi pool members my-pool

# 优雅排干（禁用）— 需双重确认
vmware-avi pool disable my-pool 10.1.1.5

# 恢复流量（启用）
vmware-avi pool enable my-pool 10.1.1.5
```

### SSL 证书过期检查

```bash
# 列出所有证书
vmware-avi ssl list

# 检查 30 天内到期的证书
vmware-avi ssl expiry --days 30
```

### 分析与错误日志

```bash
# VS 分析：吞吐量、延迟、错误率
vmware-avi analytics my-webapp-vs

# 请求错误日志
vmware-avi logs my-webapp-vs --since 1h
```

### 服务引擎健康

```bash
# 名称、管理 IP、运行状态、SE 组 — 状态来自 serviceengine-inventory
# 端点（config + runtime 合并数据）
vmware-avi se list

# 每个 SE 的运行状态 + 关联 VS 数量
vmware-avi se health
```

### AKO 问题排查

```bash
# 检查 AKO Pod 状态
vmware-avi ako status [--context my-k8s-context]

# 查看 AKO 日志
vmware-avi ako logs [--tail 100] [--since 30m]

# 重启 AKO Pod（双重确认）
vmware-avi ako restart

# 查看 AKO 版本
vmware-avi ako version
```

### AKO Helm 配置管理

AKO 的 Helm release 会自动发现（官方安装方式使用 `helm install --generate-name`，
release 名称并不固定为 `ako`）。升级时从 Broadcom 官方 OCI chart
`oci://projects.packages.broadcom.com/ako/helm-charts/ako` 拉取，并附带 `--reuse-values`。

```bash
# 查看当前 AKO Helm 值（release 自动发现）
vmware-avi ako config show

# 显示待生效变更（与官方 OCI chart 对比 diff）
vmware-avi ako config diff

# Helm 升级（双重确认 + 默认 --dry-run）
vmware-avi ako config upgrade
```

### Ingress 诊断

```bash
# 验证 Ingress 注解
vmware-avi ako ingress check <namespace>

# 显示 Ingress 到 VS 的映射
vmware-avi ako ingress map

# 诊断为何 Ingress 没有生成 VS
vmware-avi ako ingress diagnose <ingress-name>
```

### 同步诊断

```bash
# 检查 K8s-Controller 同步状态
vmware-avi ako sync status

# 显示 K8s 与 Controller 之间的不一致
vmware-avi ako sync diff

# 强制 AKO 重新同步（双重确认）
vmware-avi ako sync force
```

### 多集群 AKO

```bash
# 列出部署了 AKO 的集群
vmware-avi ako clusters

# 跨集群 AKO 状态概览
vmware-avi ako cluster-overview

# AMKO GSLB 状态
vmware-avi ako amko status
```

---

## MCP Server

MCP server 通过 [Model Context Protocol](https://modelcontextprotocol.io) 暴露全部 28 个工具，适配任何 MCP 兼容客户端。

**v1.5.15+ 推荐方式**：完成 `uv tool install vmware-avi` 后，**一条命令启动 MCP**：

```bash
# 推荐 — 单命令，无网络依赖
vmware-avi mcp

# 指定配置路径
VMWARE_AVI_CONFIG=/path/to/config.yaml vmware-avi mcp
```

### Claude Desktop 配置

添加到 `claude_desktop_config.json`：

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
<summary>备选方案：uvx（不安装）或 legacy 入口</summary>

```bash
# 不想安装，临时运行（每次需要联网 resolve 依赖）
uvx --from vmware-avi vmware-avi mcp

# 旧 entry point（仍可用，向后兼容）
vmware-avi-mcp
```

> **公司 TLS 代理网络下？** uvx 可能报 `invalid peer certificate: UnknownIssuer`。
> 推荐使用上面的 `vmware-avi mcp`（无需联网），或 `export UV_NATIVE_TLS=true`。

</details>

### MCP 工具列表（28 个）

| 类别 | 工具 |
|------|------|
| 虚拟服务（3） | `vs_list`, `vs_status`, `vs_toggle` |
| 池成员（4） | `pool_list`, `pool_members`, `pool_member_enable`, `pool_member_disable` |
| SSL 证书（2） | `ssl_list`, `ssl_expiry_check` |
| 分析（2） | `vs_analytics`, `vs_error_logs` |
| 服务引擎（2） | `se_list`, `se_health` |
| AKO Pod（4） | `ako_status`, `ako_logs`, `ako_restart`, `ako_version` |
| AKO 配置（3） | `ako_config_show`, `ako_config_diff`, `ako_config_upgrade` |
| Ingress 诊断（3） | `ako_ingress_check`, `ako_ingress_map`, `ako_ingress_diagnose` |
| 同步诊断（3） | `ako_sync_status`, `ako_sync_diff`, `ako_sync_force` |
| 多集群（2） | `ako_clusters`, `ako_amko_status` |

---

## 常见工作流

### 1. 维护窗口 -- 排干池成员

服务器补丁维护前需优雅排干流量：

1. 列出池成员及健康状态
   ```bash
   vmware-avi pool members my-pool
   ```
2. 禁用目标服务器（优雅排干）
   ```bash
   vmware-avi pool disable my-pool 10.1.1.5
   ```
3. 监控分析确认活跃连接已排干
   ```bash
   vmware-avi analytics my-vs
   ```
4. 在服务器上执行维护操作
5. 重新启用服务器
   ```bash
   vmware-avi pool enable my-pool 10.1.1.5
   ```
6. 验证健康状态恢复为绿色
   ```bash
   vmware-avi pool members my-pool
   ```

### 2. AKO Ingress 未创建 VS

开发者反馈 Ingress 没有生成虚拟服务时的排查路径：

1. 确认 AKO 正在运行
   ```bash
   vmware-avi ako status
   ```
2. 验证 Ingress 注解
   ```bash
   vmware-avi ako ingress check <namespace>
   ```
3. 检查 K8s 与 Controller 同步状态
   ```bash
   vmware-avi ako sync status
   ```
4. 如果注解有误，诊断具体 Ingress
   ```bash
   vmware-avi ako ingress diagnose <ingress-name>
   ```
5. 如检测到同步偏差，查看 diff 并按需强制同步
   ```bash
   vmware-avi ako sync diff
   vmware-avi ako sync force
   ```

### 3. SSL 证书过期审计

证书过期会导致服务中断，定期检查至关重要：

1. 检查 30 天内到期的所有证书
   ```bash
   vmware-avi ssl expiry --days 30
   ```
2. 查看每个即将过期的证书关联的 VS（输出中包含 VS 映射）
3. 与证书团队协调续期计划
4. 续期后验证新证书已就位
   ```bash
   vmware-avi ssl list
   ```

---

## 常见问题排查

### "Controller unreachable" 错误

1. 运行 `vmware-avi doctor` 验证连通性
2. 检查 `~/.vmware-avi/config.yaml` 中的控制器地址和端口是否正确
3. 自签名证书场景：在 config.yaml 中设置 `verify_ssl: false`（仅限实验环境）

### AKO Pod 处于 CrashLoopBackOff

1. 查看日志：`vmware-avi ako logs --tail 50`
2. 常见原因：values.yaml 中控制器 IP 错误、网络策略阻止 AKO 访问 Controller、凭据过期
3. 修复配置：`vmware-avi ako config show` 检查当前配置，然后用 `vmware-avi ako config upgrade` 更新（release 自动发现；从 Broadcom 官方 OCI chart 拉取）

### 创建了 Ingress 但 Controller 上没有 VS

1. 验证注解：`vmware-avi ako ingress check <namespace>`
2. 检查 AKO 日志中的拒绝原因：`vmware-avi ako logs --since 5m`
3. 运行同步 diff：`vmware-avi ako sync diff` 查看对象是否卡住

### 池成员启用后仍显示 "down"

健康检查可能仍在失败。成员已启用但不健康。检查 Controller 端的实际健康状态，先修复后端服务，健康状态会自动恢复。

### SSL 过期检查显示 0 个证书

确认控制器连接具有租户级别的访问权限。AVI 中证书是按租户隔离的，配置的用户可能只能看到其所在租户的证书。

### AKO sync force 无效

强制同步会触发 AKO 重新协调所有 K8s 对象。如果偏差持续存在，问题很可能在 K8s 资源定义本身（注解错误、Secret 缺失）。使用 `vmware-avi ako ingress diagnose` 定位根因。

---

## 安全特性

| 特性 | 说明 |
|------|------|
| **双重确认** | 破坏性操作（VS 禁用、池成员禁用、AKO 重启、Helm 升级、强制同步）需要 2 次确认 |
| **默认 Dry-Run** | `ako config upgrade` 默认为 `--dry-run` 模式，用户必须显式确认才会执行 |
| **审计日志** | 所有操作通过 vmware-policy（`@vmware_tool` 装饰器）记录到 `~/.vmware/audit.db` |
| **密码保护** | `.env` 文件加载并检查权限；密码不会出现在 shell 历史记录中 |
| **SSL 支持** | `verify_ssl: false` 仅用于隔离实验环境中的自签名证书 |
| **注入防护** | 所有 API 返回文本截断（最多 500 字符）并清除 C0/C1 控制字符 |
| **输入验证** | 池名、VS 名、IP 地址、命名空间名在 API 调用前均经过验证 |

### 安全详情

- **源代码**：[github.com/vmware-skills/VMware-AVI](https://github.com/vmware-skills/VMware-AVI)
- **配置文件内容**：`config.yaml` 仅存储控制器地址、用户名和 AKO 设置。不含密码或 token。所有密钥仅存于 `.env`
- **Webhook 数据范围**：默认禁用，无第三方数据传输
- **TLS 验证**：默认启用。仅在自签名证书环境下禁用
- **注入防护**：对所有 AVI API 响应执行 `_sanitize()` 截断 + 控制字符清理
- **最小权限**：推荐使用最小权限的 AVI 专用服务账户。AKO 操作仅需命名空间级 kubeconfig 访问

---

## 配套技能

| 技能 | 范围 | 工具数 | 安装 |
|------|------|:------:|------|
| **[vmware-avi](https://github.com/vmware-skills/VMware-AVI)** | AVI 负载均衡、AKO K8s 运维 | 28 | `uv tool install vmware-avi` |
| **[vmware-aiops](https://github.com/vmware-skills/VMware-AIops)** | VM 生命周期、部署、Guest Ops、集群 | 49 | `uv tool install vmware-aiops` |
| **[vmware-monitor](https://github.com/vmware-skills/VMware-Monitor)** | 只读监控、告警、事件 | 27 | `uv tool install vmware-monitor` |
| **[vmware-storage](https://github.com/vmware-skills/VMware-Storage)** | 数据存储、iSCSI、vSAN | 11 | `uv tool install vmware-storage` |
| **[vmware-vks](https://github.com/vmware-skills/VMware-VKS)** | Tanzu 命名空间、TKC 集群生命周期 | 20 | `uv tool install vmware-vks` |
| **[vmware-nsx](https://github.com/vmware-skills/VMware-NSX)** | NSX Segment、网关、NAT、路由 | 33 | `uv tool install vmware-nsx-mgmt` |
| **[vmware-nsx-security](https://github.com/vmware-skills/VMware-NSX-Security)** | DFW 防火墙、安全组、IDS/IPS | 21 | `uv tool install vmware-nsx-security` |
| **[vmware-aria](https://github.com/vmware-skills/VMware-Aria)** | Aria Ops：指标、告警、容量 | 28 | `uv tool install vmware-aria` |
| **[vmware-harden](https://github.com/vmware-skills/VMware-Harden)** | 合规基线、Drift 检测 | 6 | `uv tool install vmware-harden` |

---

## 问题反馈与贡献

如果遇到任何错误或问题，请将错误信息、日志或截图发送至 **zhouwei008@gmail.com**。欢迎参与贡献，一起维护和改进本项目！

## 许可证

MIT
