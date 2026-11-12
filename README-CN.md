<!-- mcp-name: io.github.vmware-skills/vmware-avi -->

# VMware-AVI

调查和管理 AVI / NSX Advanced Load Balancer 服务。

[English](README.md) · 简体中文

社区维护 · MIT 开源 · 非 VMware/Broadcom 官方产品。

[![18 秒调查示意演示](https://raw.githubusercontent.com/vmware-skills/marketplace/main/assets/demos/vmware-skills-zh.gif)](https://github.com/vmware-skills/marketplace/blob/main/assets/demos/vmware-skills-zh.mp4)

使用合成数据的示意动画，非实时录屏。[观看家族 AI 配音介绍（约 2 分钟）](https://github.com/vmware-skills/marketplace/blob/main/assets/demos/vmware-skills-zh.mp4)

## 能解决什么问题？

- 检查虚拟服务、资源池与服务引擎。
- 结合健康和证书证据排障。
- 处理受支持的 AKO 操作。

## 快速开始

安装已发布 CLI，并查看可用命令：

```sh
uv tool install vmware-avi==2.5.2
vmware-avi --help
```

需要 uv 与 Python >=3.10。远程查询前，在本地配置目标与凭据。[配置指南](skills/vmware-avi/references/setup-guide.md)。

Claude Code 及其他宿主安装方式见[家族门户](https://github.com/vmware-skills/marketplace/blob/main/README-CN.md)。

## 使用边界

包含负载均衡配置与 AKO 写操作，受控制器权限、本地 Policy 及文档中的确认边界约束。

## 详细文档

- [能力范围](skills/vmware-avi/references/capabilities.md)
- [CLI 用法](skills/vmware-avi/references/cli-reference.md)
- [Agent 使用约束](skills/vmware-avi/references/agent-guardrails.md)
- [详细用法与架构](skills/vmware-avi/references/detailed-guide-CN.md)
- [唯一技能指令正本](skills/vmware-avi/SKILL.md)

属于 [VMware Skills 家族](https://github.com/vmware-skills/marketplace/blob/main/README-CN.md)：15 个 skill、14 个 MCP server 与一个共享 Policy 库。如果对你有用，欢迎 Star 家族门户。

[问题与反馈](https://github.com/vmware-skills/VMware-AVI/issues) · [MIT](https://opensource.org/license/mit/)
