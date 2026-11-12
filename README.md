<!-- mcp-name: io.github.vmware-skills/vmware-avi -->

# VMware-AVI

<a id="readme-overview"></a>

Investigate and manage AVI / NSX Advanced Load Balancer services.

English · [简体中文](README-CN.md)

Community-maintained · MIT · Not an official VMware/Broadcom product.

[![18-second illustrative investigation demo](https://raw.githubusercontent.com/vmware-skills/marketplace/main/assets/demos/vmware-skills-en.gif)](https://github.com/vmware-skills/marketplace/blob/main/assets/demos/vmware-skills-en.mp4)

Synthetic-data animation, not a live recording. [Watch the AI-narrated family introduction (~2 min)](https://github.com/vmware-skills/marketplace/blob/main/assets/demos/vmware-skills-en.mp4)

<a id="readme-capabilities-and-workflows"></a>

## Why use it?

- Inspect virtual services, pools and service engines.
- Use health and certificate evidence for troubleshooting.
- Coordinate supported AKO operations.

<a id="readme-quick-start"></a>

## Get started

Install the published CLI and inspect its commands:

```sh
uv tool install vmware-avi==2.5.2
vmware-avi --help
```

Requires uv and Python >=3.10. Configure targets and credentials locally before remote queries. [Setup guide](skills/vmware-avi/references/setup-guide.md).

For Claude Code and other host installation paths, see the [family portal](https://github.com/vmware-skills/marketplace).

<a id="readme-safety-and-audit"></a>

## Know the boundaries

Includes load-balancer configuration and AKO writes. Controller permissions, local Policy and documented confirmation boundaries apply.

<a id="readme-mcp-configuration"></a> <a id="readme-troubleshooting"></a> <a id="readme-reference"></a>

## Go deeper

- [Capabilities](skills/vmware-avi/references/capabilities.md)
- [CLI reference](skills/vmware-avi/references/cli-reference.md)
- [Agent guardrails](skills/vmware-avi/references/agent-guardrails.md)
- [Detailed usage and architecture](skills/vmware-avi/references/detailed-guide.md)
- [MCP launch configuration](.mcp.json)
- [Canonical skill instructions](skills/vmware-avi/SKILL.md)

<a id="readme-companion-skills"></a>

Part of [VMware Skills](https://github.com/vmware-skills/marketplace): 15 skills, 14 MCP servers and one shared Policy library. If it helps your work, star the family portal.

<a id="readme-license"></a>

[Issues and feedback](https://github.com/vmware-skills/VMware-AVI/issues) · [MIT](https://opensource.org/license/mit/)
