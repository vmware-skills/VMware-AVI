## v1.8.11 — the policy gate and the operation it gated read different files

Found against a real VCF 9.1 estate.

`load_config()` never consulted `VMWARE_AVI_CONFIG`. In this skill the divide ran
*through the MCP server*: every tool reaches its controller via an `ops` function
calling `load_config()`, so no tool honoured the variable — while the policy
environment resolver went through `mtime_cached_loader`, which did. So the rule
that decides "this is production, it needs a second person" was scoped from one
file, and the operation it was gating was aimed at another. With the variable
set, the two could name different controllers.

The precedence now lives in one `resolve_config_path` that the loader, the
tools and the doctor all use. The doctor's config-directory row reported
`CONFIG_DIR` rather than the resolved path's parent, and now reports what it
actually read.

**This changes CLI behaviour**: `vmware-avi` now honours `VMWARE_AVI_CONFIG`,
where it previously ignored it. The variable is this skill's advertised
`primaryEnv`, so ignoring it was the defect.

Also: `server.json` never started the MCP server — it carried only the package
identifier, so a registry client composed `uvx vmware-avi`, which runs the CLI
and exits.

## v1.8.10 — two wrong numbers: the server's own version, and the advertised tool count

Both defects were invisible to the test suites and both were user-facing.

- **The MCP server reported the SDK's version as its own.** `FastMCP` accepts no
  `version` argument and leaves the lowlevel server's at `None`; with it `None`
  the SDK answers `initialize` with its OWN version. Every skill in the family
  therefore told its client it was mcp 1.29.1 — a number that exists for no
  package here, and one that would change with an SDK bump and no code change of
  ours. Verified end to end rather than by reading: unset the field and a probe
  server reports the installed SDK's version; set it and it reports ours.
- **server.json advertised a stale tool count.** That number is what MCP Registry
  publishes and what the plugin manifest and marketplace copy, so one stale
  integer was wrong in three public places. Corrected against the registered
  tools: 29 advertised, 28 real. README and SKILL.md were already right.

Also new: this repo is installable as a Claude Code plugin
(`/plugin install vmware-avi@vmware-skills`). The skill and its MCP server arrive in
one step; nothing is duplicated, the manifest points at the existing `skills/`
tree. family_smoke gained three gates — the server's reported version, the plugin
manifest's agreement with pyproject, and the advertised tool count against the
live registration.

## v1.8.9 — moved to vmware-skills org + MCP Registry namespace io.github.vmware-skills/vmware-avi

Repo transferred from github.com/zw008 to github.com/vmware-skills (redirects preserve old links).
MCP Registry server renamed to `io.github.vmware-skills/*`; the old `io.github.zw008/*` entry is deprecated.
All in-repo links updated. No functional code change on this line beyond the org move.

## v1.8.8 — CLI writes now route through policy + audit, exactly like the MCP tools

Every state-changing CLI command is now wrapped by `@guarded`, the CLI counterpart
to the MCP `@vmware_tool` decorator: it runs the same vmware-policy `guard()`
authorization and writes the same `audit_call()` row to `~/.vmware/audit.db`. A
`delete`/`disable`/destructive command run through a shell is now authorized and
recorded exactly like the equivalent MCP tool — closing the gap where CLI writes
bypassed policy and landed only in the legacy per-skill log (HLD I-1/I-8).

- a policy `deny` rule now refuses the operation on the CLI with a teaching line
  naming the rule that fired, not a traceback
- the legacy per-skill audit log is still written this release (dual-write); it is
  removed at 2.0
- **requires vmware-policy >= 1.8.8** (the release that adds the shared `guarded` core)
- a regression test derives the write-command set from the MCP `[WRITE]` markers and
  asserts every one is `@guarded`, so a new write command cannot ship unguarded

Also carries the environment-field docstring correction (an optional label a `deny`
rule may scope to — there is no "warn now / refuse next major" gate).

## v1.8.7 (2026-07-21) — the skill-level read-only switch is removed; read/write authorization is the vCenter account's job (RBAC)

### Removed: `VMWARE_READ_ONLY` / `read_only:` — give the agent a read-only service account instead

The skill-level read-only switch is gone. It was enforced only on the MCP tool
registry, and any agent with a shell (every SKILL.md grants `allowed-tools: Bash`)
could reach the same change one CLI command away — so it withheld the *tool*, not
the *capability*. It was never a real boundary.

To run an agent read-only, give it a **read-only vCenter/NSX service account
(RBAC)**. Writes are then refused at the platform, un-bypassably, regardless of
surface or shell — the one place read/write control cannot be stepped around. A
config still carrying `read_only: true` is ignored, with a one-time warning that
names the replacement (no silent behavior change).

### Removed: approval tiers and the declared-environment gate (via vmware-policy)

The graduated-autonomy approval tiers (`confirm`/`dual`/`review`) and the "declare
an environment or be refused" baseline are removed — they only ever fired on the
rarest configuration while carrying the family's most complex machinery. Opt-in
`deny` rules and the maintenance window remain, and apply identically wherever a
tool runs.

### Added: offline / air-gapped install docs

The README now covers installing from source without editable mode (for older
`pip`) and building wheels to carry onto an air-gapped host — the modern PEP 517
layout has no `setup.py` by design, which is expected, not a missing file.

This release also carries the accumulated fixes staged since 1.8.5.

## v1.8.5 (2026-07-20) — the two fixes v1.8.4 announced now actually work

Four adversarial reviews of v1.8.4 found that both of its headline fixes were
incomplete in ways the release notes did not reflect. This release makes them
real. If you are on 1.8.4, this is the one to take.

### Fixed — a failure that was *returned* was still audited as a success

vmware-policy 1.8.4 added `report_tool_failure()` for tools that catch an
exception and return an error payload instead of raising. **No skill called it.**

Every string-returning tool therefore kept doing exactly what 1.8.4 said it had
stopped doing: writing `status=ok` to `~/.vmware/audit.db` for an operation that
failed, recording an undo token for a change that never happened, and telling the
circuit breaker the call succeeded so repeated failures never tripped it.

The surface this covered is not marginal:

| Skill | What was mis-audited |
|---|---|
| vmware-aiops | 25 of 49 tools, including **every undo-bearing write** — a failed `vm_power_on` left an undo token saying "power it back off" |
| vmware-avi | all 28 tools, including `vs_toggle` and `ako_restart` |
| vmware-storage | all 4 write tools |
| vmware-nsx | the 5 delete tools |

vmware-avi is worth calling out: before 1.8.4 its exceptions propagated and the
audit was correct. 1.8.4 caught them and returned a string, so **that release made
its audit trail worse than it had been.**

Skills whose tools already return dict payloads (vmware-monitor, vmware-vks,
vmware-aria, vmware-log-insight, vmware-harden, vmware-debug, vmware-pilot) were
already detected correctly. They gained a test proving it rather than a redundant
call.

### Fixed — narrowing `OSError` did not close the leak it was meant to close

1.8.4 narrowed the `_safe_error` passthrough because bare `OSError` let TLS and
DNS failures reach the agent with hostnames and certificate subjects in them.
That narrowing had no effect on the error it was written for:

```
ssl.SSLCertVerificationError → ssl.SSLError → OSError, ValueError
```

`ValueError` has been on every allowlist since long before 1.8.4, so a
certificate failure kept passing through — the commonest self-signed-certificate
failure in this family, carrying the hostname it was checked against. An
allowlist structurally cannot express "not this one".

Where `ssl.SSLError` can actually surface — the pyVmomi skills — it is now
reduced *ahead* of the allowlist. In the httpx skills TLS arrives wrapped as
`httpx.ConnectError`, and in vmware-avi as `requests.exceptions.SSLError`, so the
guard cannot fire there; in those skills the leak was the raw exception
interpolated into an already-allowlisted `*ApiError`, and that is now authored
text naming the config target and `verify_ssl` instead of the exception.

The missing-password error — this family's most common first-run failure, whose
entire remedy is the environment variable name it carries — keeps its message
through a narrow `ConfigError(OSError)` rather than the base class. Connection
failures are translated at the connection layer into an authored remedy that
names the target and the setting to change, with the raw detail left on
`__cause__` for the server log.

### Also fixed

- **vmware-vks**: the quickstart documented a password variable the code never
  reads — following `README.md` verbatim produced "Password not found". Five
  places, plus six references to a `doctor` command this CLI has never had, two
  descriptions promising fields the tools do not return, and eight teaching
  messages that `RuntimeError` was masking.
- **vmware-nsx**: an error cited `--route-advertisement`; the flag is `--advertise`.
- **vmware-pilot**: `get_workflow_status` told the model to call `approve` — a
  tool the read-only gate withholds — as the required next step; and a hint
  pointed at a filename that could never appear in that message.
- **vmware-aiops**: `vm_task_status` polling a *failed task* returned
  `{"state": "error", "error": ...}` from a successful read, which the new
  detection read as the call itself failing. The field is now `task_error`.
  **This is a breaking change for anything parsing that payload.**
- Several remedies that were still being cut by the 300-character cap the 1.8.4
  notes claimed to have addressed.

### Known and not fixed

`ConnectionError` remains one type from two sources in several skills — a
skill's own authored message and urllib3's `HTTPSConnectionPool(host=..., port=...)`
share it, and an allowlist cannot separate them. vmware-vks is converted; the
rest need their own domain type and are deferred rather than half-done.

## v1.8.4 (2026-07-20) — errors that teach, and tool descriptions a small model can route from

A capability eval was rolled out across the family and asked two open questions:
when a call fails, is the model told enough to fix it, and can it pick the right
tool from the description alone? Both answers were worse than anyone thought, and
in several places the reason was that the measurement was looking somewhere other
than where the model reads.

### Fixed — teaching messages were being discarded on the way to the agent

`_safe_error` reduces unrecognised exceptions to `"<Class>: operation failed."`
so raw API text, credentials in URLs and internal paths cannot reach an agent.
Its allowlist held only the builtin validation errors — so this skill's **own**
domain exceptions, the ones that exist precisely to carry a corrected next step,
had their messages replaced by their class names.

The effect was invisible from the CLI, which prints those messages in full.

The worst case was shared by nine skills: `config.py` raises exactly one
`OSError`, the missing-password error, whose entire remedy is the environment
variable name it names. An agent hitting an unconfigured target received
`OSError: operation failed.` and had nothing to act on. That is the family's most
common first-run failure, and it landed one release after the documented variable
names were corrected — so the message that would have unstuck the operator was
the one being thrown away.

The rule is now the property it always meant: **every exception this skill raises
on purpose passes through**, and only genuinely unplanned ones are reduced.
`RuntimeError` stays reduced — it is the generic catch-all and in several skills
carries raw upstream text.

### Fixed — error messages now carry the correction

Every message that reported a failure without saying how to recover was
rewritten: it names the offending value, gives an imperative remedy, and names
something concrete to act on — a tool that exists, a real CLI command, a config
file, an environment variable. Recovery becomes an instruction-following problem
rather than an inference one, which is what a weak model can still do.

Three classes of defect surfaced while doing it:

- **Remedies that were never delivered.** `_safe_error` truncates with no
  ellipsis, so a message longer than the cap loses its closing sentence
  silently. One message had been shipping at 396 characters against a 300-char
  cap — its remedy had never once reached an agent. Messages now lead with the
  remedy so a long interpolated value truncates the expendable detail instead.
- **Commands that do not exist.** One skill's error hints named a `doctor`
  subcommand it does not have.
- **Tools that do not exist.** A tool description pointed at two sibling-skill
  tools that had been renamed, and another named a tool that had moved to a
  different skill entirely.

### Improved — tool descriptions state when to use them and what to call next

The description is the API for a small model: an unstated routing rule is a
routing rule that does not exist, and a tool with no stated next hop is one the
model stops at. Descriptions now say when to prefer this tool over a sibling,
what shape comes back, the caveat that bites, and which tool to call after.

**Manifest size did not grow.** Descriptions load into every session, so the
routing clauses were paid for by cutting duplicated reference material —
repeated boilerplate, examples that restated the parameter list, and prose
copies of the pagination contract.

### Note

Every tool and CLI command named anywhere in this release was verified against
the live MCP registry and the live command tree, not against documentation.

## v1.8.3 (2026-07-20) — credentials resolve as a pair; documented env vars now exist

### Added — the per-target username can come from the environment

Adapted from [VMware-AIops#33](https://github.com/vmware-skills/VMware-AIops/pull/33) by
@wright-bench, with thanks. The password already resolved from an env var; the
username did not, so a deployment injecting credentials from a secret store
(systemd `EnvironmentFile`, container secrets, a vault sidecar) could externalise
only half of the pair — and a config-file username paired with an env password
from a different account logs in as nobody.

`<PASSWORD-KEY-PREFIX>_USERNAME` now overrides the `username:` in config.yaml,
using that skill's own password-key convention. Absent, config.yaml still wins;
nothing changes for anyone not setting it.

**Resolved on every access, like the password.** The contributed version read the
username once at load time while the password stayed a property, which
reintroduces exactly the split the override exists to prevent: a sidecar rotating
both halves mid-process moves the password and leaves the username behind. A test
pins that both halves resolve at the same moment.

### Fixed — documented credential variables that the code never read

Rolling the above across the family surfaced a separate defect: four skills
documented a password variable their own loader does not look up. An operator
following the documentation exactly — correct file, correct place, correct-looking
name — got "Password not found".

| Skill | Documented | Actually read |
|---|---|---|
| vmware-nsx | `VMWARE_NSX_<TARGET>_PASSWORD` for target `nsx-prod` → `VMWARE_NSX_PROD_PASSWORD` | `VMWARE_NSX_NSX_PROD_PASSWORD` |
| vmware-nsx-security | `VMWARE_<TARGET>_PASSWORD` | `VMWARE_NSX_SECURITY_<TARGET>_PASSWORD` |
| vmware-aria | `VMWARE_<TARGET>_PASSWORD` | `VMWARE_ARIA_<TARGET>_PASSWORD` |
| vmware-vks | `VMWARE_<TARGET>_PASSWORD` | `VMWARE_VKS_<TARGET>_PASSWORD` |
| vmware-avi | three different forms across three files | `<CONTROLLER>_PASSWORD` |

The prefixes genuinely differ per skill, so nothing could be fixed by
standardising a pattern — each repo's docs were corrected against its own code.
The code was left alone: changing a key would break every existing deployment.

`family_smoke.sh` now compares the credential variables named in each repo's docs
against the ones that repo's code builds, so the two cannot drift apart again.

## v1.8.2 (2026-07-20) — the MCP server moves into the package namespace

### Fixed — co-installing two skills broke all but the last one

Every skill shipped its MCP server as a **top-level `mcp_server` package**. Python
has one top-level namespace, so installing any two of them into one environment let
the second overwrite the first — silently, with no error and no warning.

    uv tool install vmware-aiops   ->  49 tools   (correct)
    uv pip  install vmware-aiops   ->  27 tools   (Monitor's read-only server)

vmware-aiops depends on vmware-monitor, so this was not an edge case: **every pip
install hit it**, and the operator got 27 read-only tools where 49 were expected,
with all 35 write tools missing. Docker images, shared MCP hosts and CI runners that
install more than one skill were affected the same way.

The server now lives at `vmware_<skill>/mcp_server/`, a name only this package can
claim. Introduced 2026-02-26; it survived 70 releases because every test ran against
a single package in its own repo, where the local directory shadows site-packages —
the conflict was invisible by construction.

**Migration.** Console scripts are unchanged: `vmware-<skill>` and
`vmware-<skill>-mcp` work exactly as before, as does `"command": "vmware-<skill>",
"args": ["mcp"]` in an MCP client config. Only a direct `python -m mcp_server`
breaks; use `python -m vmware_<skill>.mcp_server`.

### Added — `references/agent-guardrails.md` in every skill

The operating rules for local and small models (Llama 3.3 70B, Qwen, Mistral via
Goose / Ollama / OpenShift AI) existed in two skills. They now ship in all 13, each
with its own tool counts and failure modes, and are linked from every SKILL.md.

## v1.8.1 (2026-07-19) — read-only mode reaches the surfaces that teach it

v1.8.0 put read-only mode in the code and documented it in the README only.
Every other layer was empty, and each serves a different reader: SKILL.md is what
the agent loads, setup-guide is what an operator reads while configuring, `doctor`
is where they verify it took. The gap had two concrete costs.

An agent read SKILL.md, called a write tool the gate had withheld, and got nothing
back — with no way to learn that the absence was a deliberate lockdown rather than
a fault. It reads as a broken tool, so the model retries or hunts for a workaround.

An operator who set the switch had no way to confirm it. The only signal was a line
in the MCP server's start-up log.

### Added — the feature is now documented where each reader looks

- **SKILL.md** — a short section telling the agent that a missing write tool is a
  lockdown, not a fault: name the blocked operation, do not retry, do not route
  around it.
- **references/setup-guide.md** — the operator's view: how to enable it, the
  precedence chain, and how to verify.
- **references/capabilities.md** — which tools the gate withholds.

### Added — `doctor` reports the read-only state

`vmware-avi doctor` now shows whether read-only mode is on, **which** of the three
switches decided it, and the value as written. A typo'd value (`ture`) is called
out as a typo rather than reported as a confident ON — it resolves to on, which is
fail-closed but almost never what was meant.

The resolution runs through `vmware_policy.read_only_status()` rather than a local
copy of the precedence chain: a doctor that disagrees with the gate it reports on is
worse than no doctor. Requires `vmware-policy>=1.8.1`.

## v1.8.0 (2026-07-18) — read-only mode, working policy defaults, declared environments

Family release driven by [VMware-AIops#31](https://github.com/vmware-skills/VMware-AIops/issues/31),
where an operator running Llama 3.3 70B (Goose / OpenShift AI, on-prem H100) had to
hand-write 17 prompt guardrails to make tool calling reliable. A prompt is advisory — a
model can ignore it. Every guardrail that could move into the harness has.

### Added
- **Read-only mode.** Set `VMWARE_READ_ONLY=true` (or `VMWARE_<SKILL>_READ_ONLY`, or
  `read_only: true` in config.yaml) and every write tool is removed from the MCP registry
  at start-up. `list_tools()` never offers them, so the model cannot call what it cannot
  see. **Off by default** — nothing changes unless you turn it on. Fail-closed: if the
  mode is requested but cannot be guaranteed, the server refuses to start rather than
  running open.
- **`environment:` on each config target**, declaring which environment it is
  (production / staging / lab). Policy rules scope by this value.

### Not changed — the list envelope does not apply here

The rest of the family moved its `[READ]` list tools to a result envelope stating
`returned` / `total` / `truncated`. vmware-avi deliberately did not: its list tools render
Rich tables and return the rendered string, so there are no rows to wrap, and `api_get_all`
walks every page, so `truncated` would be permanently false. Converting four of thirty
render-style functions would fragment the response style inside one server for no gain.

Separately worth knowing: an ASCII table is a mediocre agent payload (box-drawing burns
tokens, colour markup leaks into the text). Restructuring AVI's ops layer into data +
render is real work with its own risk budget, tracked separately.

### Changed — migration, read this
- **Approval tiers now actually run.** They shipped in v1.6.0 but the engine only ever
  read `~/.vmware/rules.yaml`, and a fresh install has no such file — so every deny rule,
  maintenance window and approval tier had been inert on every install that never
  hand-authored one. A packaged baseline now loads when you have written no rules of your
  own. Writes at medium risk and above are stamped with their tier in the audit log;
  irreversible work and guest execution against a target declared `production` require a
  named approver via `VMWARE_AUDIT_APPROVED_BY`.
- **`environment:` will become required for writes.** Today a state-changing operation
  against a target that declares none still runs and logs a warning. **The next major
  release refuses it.** Declare it now and that upgrade is a no-op:

      targets:
        prod-vc01:
          host: vc01.corp.local
          environment: production

  Read-only operations are never affected, in this release or the next. Check what applies
  to your targets before upgrading: `vmware-audit policy --operation vm_delete --env <env>`.

### Fixed
- **Policy glob patterns with a leading wildcard silently matched nothing.** A rule written
  `operations: ["*_delete"]` parsed fine, read correctly, and never fired — only a trailing
  `*` was honoured. Now full glob matching, for operations and environments alike.
- Config-path overrides (`VMWARE_<SKILL>_CONFIG`) are honoured when reading `read_only`
  and `environment`, so a setting in a custom config file is no longer silently ignored.

### Notes
- Requires `vmware-policy>=1.8.0`; publish that package first.
- `vmware-audit policy` reports which rules are in force and where they came from —
  including the case where your rules file exists but failed to parse, which previously
  looked identical to "policy is working".

### Fixed — pre-release review (2026-07-19)

- **`ako_config_diff` did not preview what `ako_config_upgrade` applies.** The upgrade
  ran with `--reuse-values`; the diff did not, so it rendered the chart's defaults and
  reported every local customisation as a pending change. The two were presented as
  preview-then-apply while answering different questions. The diff now issues the same
  command as the upgrade.
- **Both commands now accept `chart_version`.** An unpinned OCI reference resolves to
  whatever the registry currently tags latest, so a diff and the upgrade that follows it
  could target different charts, and the same diff could change between runs with no
  local edit. Empty keeps the previous behaviour; read the installed version with
  `ako_version` and pass it to both when the preview needs to bind.

## v1.7.5 (2026-07-13) — internal lint cleanup + family version alignment

### Internal
- Style-only lint cleanup (58 findings: long lines, empty f-strings, unused
  imports, import ordering). No behavior change; MCP tool surface unchanged (28).

## v1.7.4 (2026-07-13) — family version alignment

## v1.7.3 (2026-07-03) — family version alignment

## v1.7.2 (2026-07-02) — pool/ingress N+1 + honest pagination

### Fixed
- **Pool & ingress round-trip storms.** `list_pools` (with a VS filter) issued a
  per-pool-group GET, and `check_ingress_annotations` read one Kubernetes secret
  per Ingress TLS entry. Both collapse to a single bulk list + in-memory join
  (the `check_se_health` pattern). List operations and AKO sync now page through
  results (new `api_get_all`) so counts/diffs are accurate instead of silently
  capped at 1000. A per-cluster `kubectl` probe failure no longer aborts the whole
  multi-cluster listing. Output shape unchanged.

## v1.7.1 (2026-07-02) — family version alignment

No code changes. Version bump to stay aligned with the v1.7.1 family release
(VMware-AIops + VMware-Monitor large-inventory scale fix — PropertyCollector
batching to stop per-object lazy SOAP round-trips, GitHub issue #31).

## v1.7.0 (2026-06-27) — guided onboarding + teaching auth errors

### Added
- **`vmware-avi init` — interactive first-run setup wizard.** Prompts for host /
  username / password and writes `config.yaml` + `.env` for you. The password is
  stored grep-safe (`b64:`, never plaintext on disk) and `.env` is locked to
  0600, then the connection is verified. Replaces the manual "mkdir + cp
  config.example.yaml + edit YAML + chmod 600" dance.
- `.env.example` added; the old static `init` that wrote a plaintext
  `MY_AVI_PASSWORD=changeme` is gone.

### Changed
- `doctor` now points to `vmware-avi init` when config/credentials are missing
  (previously suggested a command that did not exist), keeping the manual steps
  as a fallback.
- Authentication and TLS failures now print a teaching message naming the exact
  file and env var to fix (`~/.vmware-avi/.env` password var, `config.yaml`
  username) plus a `verify_ssl: false` hint for self-signed labs.
- Write commands (vs/pool/ako enable/disable/restart) now get the same
  auth/TLS teaching as read commands (previously a raw traceback).

## v1.6.1 (2026-06-24)

### Added
- **`.env` passwords are auto-obfuscated to a grep-safe `b64:` form** on first
  load and decoded transparently at runtime — plaintext no longer sits in
  `~/.<skill>/.env` for a casual `grep` to find. Values are read/written through
  python-dotenv's own parser, so the stored secret never drifts from the
  configured one (handles quotes, inline comments, trailing whitespace, and a
  password that literally starts with `b64:`). **Obfuscation, not encryption** —
  for real at-rest secrecy, inject the password from a secret manager instead of
  storing `.env`. New regression suite (10 cases) covers dotenv parity, the
  `b64:`-prefixed edge case, idempotency, and 0600 preservation.

## v1.6.0 (2026-06-22) — trust architecture: undo tokens

### Added
- **Undo-token recording** (vmware-policy 1.6.0): `vs_toggle` (records the opposite-state toggle as its inverse).
- Inherits harness budget guard, audit accountability fields, and graduated risk tiers.

### Changed
- Requires **vmware-policy >= 1.6.0**.

## v1.5.39 (2026-06-22) — family version alignment

No code changes. Version bump to stay aligned with the v1.5.39 family release
(AIops snapshot-delete async + honest-timeout token-burn fix; Storage datastore-browse timeout fix).

## v1.5.38 (2026-06-12) — release alignment

No functional changes — version bumped to keep the VMware skill family aligned at v1.5.38.

## v1.5.37 (2026-06-12) — backlog: AKO sync-diff accuracy

### Fixed
- `show_sync_diff` no longer falsely flags shard-mode Ingresses as "Missing on Controller" — it now also
  matches AKO-created pool names and prints a best-effort caveat. (#13)

## v1.5.36 (2026-06-12) — AKO release-blocker fix, honest write results, error translation

### Fixed
- **All AKO Kubernetes operations were crashing** (`AttributeError`) — `K8sConnectionManager` was
  built with the wrong config type at 10 call sites; now via `from_config()`. Status/logs/restart/
  version, sync, and ingress all work again. (Class-level test mocks had hidden the break.)
- **Write operations no longer report success on a failed PUT** — avisdk doesn't raise on 4xx/5xx,
  so `vs enable/disable` and pool-member drain now check the status code and surface a teaching error.
- **MCP `ako_config_upgrade` no longer corrupts the stdio channel** — added a `confirmed=False`
  preview gate (it previously printed to real stdout and blocked on stdin).
- Null-safe analytics log fields; pool `vs_filter` ref normalization.

### Added
- Centralized `AviApiError` translation in the connection layer (404 teaching hint, GET-only
  retry-once on 502/503/504, unreachable-controller hint).
- CLI write operations are now audited.

### Changed
- Removed two duplicate MCP tools (`ako_ingress_fix_suggest`, `ako_cluster_overview`); tool count
  is now **28 (22 read / 6 write)**, reflected across SKILL.md and READMEs.

## v1.5.35 (2026-06-10) — security fix: TLS verification now actually enforced

### Fixed
- **`verify_ssl` is now passed to avisdk** (`ApiSession.get_session(verify=…)`). Previously
  the flag was dropped and avisdk silently defaulted to no verification — the documented
  "TLS on by default" behaviour was non-functional. TLS verification now works as documented.
- **Output sanitization** centralized in `_safety.sanitize` and applied consistently to
  pool / Service Engine / SSL-cert / AKO-ingress / VS names from the API.

This release aligns the whole family back to a single version (1.5.35); vmware-policy and vmware-pilot return to the shared number after sitting at 1.5.22.

## v1.5.32 (2026-06-08) — Metric/field corrections + AKO helm/AMKO discovery fixes

### Fixed
- Latency metric is `l7_client.avg_client_txn_latency` (`avg_resp_latency`
  never existed in the AVI metrics catalog).
- SE listing reads `serviceengine-inventory` (the config object has no
  `oper_status` — the Status column was always N/A).
- SE→VS mapping derives the SE UUID from the `ref` URL (VipSeAssigned has no
  `uuid` field — VS counts were always 0).
- AKO helm: upgrades pull the official OCI chart
  (`oci://projects.packages.broadcom.com/ako/helm-charts/ako`) and the release
  name is discovered dynamically (official installs use `--generate-name`;
  the previous `avi/ako` alias + hardcoded `ako` release never worked).
- AMKO detection uses the chart's real labels (`app.kubernetes.io/name=amko`).
- Controller logout via POST (was DELETE → 405, silently swallowed; sessions
  never invalidated server-side).
- Error-log filter `ge(response_code,400)` (no longer flags 2xx/3xx as errors).

### Tests & docs
- +19 shape regression tests incl. a source scan that fails if the invented
  metric ID reappears; test-infra repairs; docs synced (30 tools).

## v1.5.30 (2026-06-07) — Tool description quality (Glama TDQS)

### Improved
- Rewrote MCP tool descriptions flagged by Glama's Tool Description Quality Score review:
  per-parameter semantics (format, defaults, valid values), return-field documentation,
  sibling-tool routing guidance, and behavioral transparency (side effects, audit logging,
  async semantics). Corrected descriptions that overstated or misstated actual behavior.
- No functional changes; descriptions only.

## v1.5.29 (2026-05-29) — Version Compatibility Table + Smithery/Container Docs

### Documentation
- `references/capabilities.md`: new "Version Compatibility" section with 4 sub-tables (AVI Controller 22.1.x/30.x, VCF 9.0/9.1, Python 3.11+, MCP transport modes). Mirrors NSX capabilities.md style with ✅/⚠ markers. Closes v1.5.23 and v1.5.11 doc gaps.
- `references/setup-guide.md`: replaced hardcoded "v1.4.0" header reference with "current release" + RELEASE_NOTES pointer; added "Alternative Deployment: Container / Smithery" section covering Docker build/run, Smithery registry, and a "When to use which" comparison table.
- `SKILL.md`: 1-line "Supported versions" callout pointing to capabilities.md; extended setup-guide pointer to mention container/Smithery.

### No code changes
Documentation-only release.

## v1.5.28 (2026-05-20)

**Fix `subclass() arg 1 must be a class` in goose/old mcp environments** —
v1.5.25–1.5.27 replaced `X | None` with `Optional[X]` but kept
`from __future__ import annotations` at the top of `mcp_server/server.py`.
Under mcp 1.10–1.13 (which Goose and some sandboxes pin), `Tool.from_function`
calls `issubclass(param.annotation, Context)` without resolving forward refs,
so string annotations crash the entire server load. Removed
`from __future__ import annotations` from `mcp_server/server.py` so annotations
are real classes; verified all tools load under mcp 1.10 and 1.14.

Traceback location: `mcp/server/fastmcp/tools/base.py:67`. CLAUDE.md 踩坑 #33
updated. family_smoke.sh Check 4b now installs `mcp==1.10.0` to catch this
regression class.

## v1.5.27 (2026-05-20)

**Loosen Python requirement: now supports Python >= 3.10** — v1.5.25/26 fixed
the PEP 604 root cause in MCP tool signatures (Optional[X] instead of X | None),
but kept `requires-python = ">=3.11"` and a 3.11 hard guard in `mcp_cmd`. Both
relaxed to 3.10 so users on Python 3.10 (e.g. Goose default sandbox, Ubuntu
22.04 system python) can install and run directly without a Python upgrade.

- `pyproject.toml`: `requires-python = ">=3.10"` (was `>=3.11`; VMware-VKS
  was `>=3.12`, now also `>=3.10` for family alignment).
- `<pkg>/cli.py` `mcp_cmd()`: version guard now triggers on `< (3, 10)`.
- Behavior on Python 3.10 matches 3.11/3.12 — the Optional[X] fix from v1.5.25
  is what actually enables this; this release just stops blocking installs.

---

## v1.5.26

**Family-wide MCP server fix — Python 3.10 compatibility (踩坑 #33)** — `vmware-avi mcp`
crashed at decorator time on Python 3.10 with `subclass() arg 1 must be a class`.
Root cause: `mcp_server/server.py` used PEP 604 `X | None` in tool signatures
plus `from __future__ import annotations`; on Python 3.10 + older mcp/pydantic
combos, `typing.get_type_hints()` evaluates `"str | None"` to a
`types.UnionType` instance, which FastMCP/Pydantic then feeds to `issubclass()`.
Reported by a goose user (qwen3.6:27, Python 3.10).

- `mcp_server/server.py`: all `X | None` → `Optional[X]`; ops layer untouched.
- `<pkg>/cli.py` `mcp_cmd()`: hard guard — exits with installation fix command
  if Python < 3.11 (defense in depth, our actual lower bound).
- `pyproject.toml`: `mcp[cli]>=1.10,<2.0` (was `>=1.0`) so uv doesn't pick
  an ancient version that has the same issubclass bug.

**Tooling — family smoke gains MCP schema-build check** — `scripts/family_smoke.sh`
new Check 4b runs `asyncio.run(mcp.list_tools())` per skill, forcing FastMCP to
build Pydantic models for every declared tool. Supports both module-level `mcp`
and `build_server()` factory patterns.

**Docs — CLAUDE.md gains 踩坑 #33 (PEP 604 / Python 3.10) and #34 (CLI/MCP exposure parity).**

---

## v1.5.24 (2026-05-19)

**Family version alignment** — no code changes in this skill. Bumped together
with VMware-AIops and VMware-VKS, which received a pyVmomi 8.x `ManagedObject`
setattr fix (踩坑 #32). `family_smoke.sh` now enforces the no-setattr rule
across all 9 skills.

## v1.5.23 (2026-05-19)

**VCF 9.0 / 9.1 compatibility declared.**

- **docs:** README now lists AVI / NSX ALB on VCF 9.0 / 9.1 as ✅ Full. The avisdk dependency (`>=22.1,<31.0`) covers AVI Controller versions bundled with VCF 9.x.
- **docs:** Added `Official Broadcom References` pointer to [VCF Python SDK](https://developer.broadcom.com/sdks) and the [AVI Controller REST API docs](https://developer.broadcom.com/xapis).
- **align:** Family v1.5.23 — all 9 skills tracking VCF 9.0 / 9.1 compatibility declaration.

## v1.5.22 (2026-05-08)

**Smithery onboarding** — `vmware-avi` is now installable via Smithery.

- **feat:** Added `Dockerfile` (Python 3.12-slim + uv) for containerized stdio MCP server.
- **feat:** Added `smithery.yaml` declaring stdio transport + config schema for the Smithery registry.
- **feat:** Added `mcp_server/__main__.py` so `python -m mcp_server` works inside the container.
- **align:** Tracks v1.5.22 family bump.

## v1.5.21 (2026-05-08)

**Family alignment** — no source changes in this skill.

- **deps:** Bumped `python-multipart` 0.0.26 → 0.0.27 (transitive, fixes GHSA HIGH DoS via unbounded multipart headers).
- **align:** Tracks v1.5.21 family bump driven by vmware-monitor folder_path feature (community PR #11).

## v1.5.20 (2026-05-08)

**Family alignment** — no source changes in this skill.

- **align:** Tracks v1.5.20 family bump driven by vmware-nsx-security and vmware-aria PyPI README `mcp-name:` ownership marker fix required by MCP Registry validation. Other 7 skills already had the marker; this release re-publishes them to keep the family version aligned per CLAUDE.md policy.
- **registry:** All 9 skills now registered on registry.modelcontextprotocol.io as `isLatest=true`.

## v1.5.19 (2026-05-06)

**Family alignment** — no source changes in this skill.

- **build:** Bumped `requires-python` from `>=3.10` to `>=3.11` (regression eval uses `tomllib`).
- **smoke:** Family `scripts/family_smoke.sh` adds Check 3b — recursive `--help` on every subcommand to surface broken lazy imports (yjs review 2026-05-06; 踩坑 #27).
- **align:** Tracks v1.5.19 fixes in vmware-nsx (CRITICAL CLI imports), vmware-vks (ApiClient leak), vmware-harden (Twin indexes + LEFT JOIN), vmware-policy (approval gate + singleton lock).

## v1.5.18 (2026-05-02)

**Family alignment + tooling normalization** — no source changes in this skill.

- **dev:** Migrated `[project.optional-dependencies] dev` → `[dependency-groups] dev` (PEP 735) so `uv sync --group dev` works uniformly across the family. Canonical set: `pytest>=8.0,<10.0`, `pytest-cov`, `ruff`.
- **test:** New `tests/eval/regression/test_release_blockers.py` (5 evals) catches the v1.5.x release blockers — missing `mcp_server` in wheel, AST-detected unimported runtime names, Typer app load failure, module import errors. Run via `pytest tests/eval/regression/`.
- **align:** Family version bump to v1.5.18.

## v1.5.17 (2026-05-01)

**Family alignment** — no source changes in this skill.

This release tracks vmware-pilot v1.5.17 (new `investigate_alert` template + `review_workflow` MCP tool + `parallel_group` step type) and vmware-policy v1.5.17 (L5 pattern matcher integrated into `@vmware_tool`). Both work with the existing skill MCP surface unchanged.

- **align:** Family version bump to v1.5.17.

## v1.5.16 (2026-04-30)

**Enterprise Harness Engineering alignment** — adapted from the Linkloud × addxai framework articles ([part 1](https://mp.weixin.qq.com/s/hz4W7ILHJ1yz_pG0Z1xP-A), [part 2](https://mp.weixin.qq.com/s/F3qYbyB3S8oIqx-Y4BrWNQ)).

- **docs:** "Automation Level Reference" section in `references/capabilities.md` — every tool tagged L1-L5 per the EHE framework.
- **docs:** Common Workflows in `SKILL.md` rewritten with pre-flight judgment for pool drain (capacity check / persistence behavior / observability baseline), AKO failure-layer triage, and SSL expiry audit.
- **align:** Family version bump to v1.5.16.

## v1.5.15 (2026-04-29)

**UX improvements from real user feedback**

- **feat:** New top-level CLI subcommand `vmware-avi mcp` starts the MCP server. Single command after `uv tool install vmware-avi` — no more `uvx --from`, no PyPI re-resolve, no TLS-proxy issues.
- **feat:** Default `verify_ssl: true` on new targets (was `false`). AVI Controller with default self-signed certs requires explicit `verify_ssl: false` in `config.yaml`.
- **docs:** README, SKILL.md, setup-guide.md, and `examples/mcp-configs/*.json` switched to `command: "vmware-avi"`, `args: ["mcp"]`. uvx form moved to fallback with TLS-proxy troubleshooting note.
- **compat:** Legacy `vmware-avi-mcp` console script kept — existing user configs continue to work.

## v1.5.14 (2026-04-21)

**Bug fixes from code review by @yjs-2026 (follow-up)**

- **fix:** `ako_ingress.py` — TLS secret checks now distinguish 404 (not found) from other errors (network, auth) instead of treating all exceptions as "not found"
- **fix:** `ako_ingress.py` — backend service path now checks for None on `backend`, `service`, and `name` before accessing

## v1.5.13 (2026-04-21)

**Bug fixes from code review 2026-04-20**

- **fix:** `ako_sync.py` — sync diff now uses exact + suffix match instead of substring match, preventing false negatives (e.g. ingress "ing" no longer matches "staging")
- **fix:** `ako_config.py` / `ako_multi_cluster.py` — all 7 `subprocess.run()` calls now have explicit `timeout` (30s kubectl, 120s helm, 300s upgrade) to prevent indefinite hangs

## v1.5.12 (2026-04-17)

- Align with VMware skill family v1.5.12 (security & bug fixes from code review by @yjs-2026)

## v1.5.11 (2026-04-17)

- Fix: vs_analytics HTTP 404 — AVI 22.x requires POST for `/analytics/metrics/collection` with `metric_requests[]` array wrapping (PR #4, credit @timwangbc)
- Fix: vs_error_logs HTTP 400 "VirtualService ID required" — added `virtualservice` URL param for VS UUID (PR #5, credit @timwangbc)
- Fix: pool_list vs_filter returned 0 matches for K8S-managed VSes — switched to `/virtualservice-inventory` which exposes the real VS→pool graph including pool groups (PR #6, credit @timwangbc)
- Fix: se_health VS count always 0 on 22.x — reconstructed SE→VS mapping by inverting `vip_summary[].service_engine[]` from `/virtualservice-inventory` (PR #7, credit @timwangbc)
- Tests: added unit tests for analytics, error logs, pool discovery, and SE health (14 new test cases)
- Result: 12/12 non-AKO MCP tools now pass against AVI Controller 22.x

## v1.5.10 (2026-04-16)

- Security: bump python-multipart 0.0.22→0.0.26 (DoS via large multipart preamble/epilogue)
- Align with VMware skill family v1.5.10

## v1.5.8 (2026-04-15)

- Feat: MCP server migrated from low-level `mcp.server.Server` to `FastMCP`, matching the rest of the family. All 30 tool names and input schemas preserved.
- Fix: 4 destructive MCP tools bypassed CLI's interactive `double_confirm` in MCP mode. Added `confirmed: bool = False` parameter with preview-by-default: `vs_toggle` (when disabling), `pool_member_disable`, `ako_restart`, `ako_sync_force`. Ops-layer functions now take `skip_prompt` so MCP callers that already validated `confirmed=True` bypass the stdio-blocking prompt.
- Align with VMware skill family v1.5.8

## v1.5.7 (2026-04-15)

- Align with VMware skill family v1.5.7 (Pilot `__from_step_N__` fix + VKS SSL/timeout fix)

## v1.5.6 (2026-04-15)

- Fix: CRITICAL — `mcp_server` module missing from PyPI wheel due to missing hatch packages config. MCP server would fail with `ModuleNotFoundError: No module named 'mcp_server'`. Added `[tool.hatch.build.targets.wheel] packages = ["vmware_avi", "mcp_server"]` to pyproject.toml
- Fix: FQDN in controller host config — avisdk rejected non-IP hostnames with "Invalid Controller IP6 Address". Connection manager now resolves FQDN -> IP via socket.getaddrinfo() at connect time
- Fix: vs_analytics returned empty series — switched from per-entity endpoint to analytics/metrics/collection with entity_uuid for consistent results across Controller 22.x/30.x versions
- Fix: vs_error_logs HTTP 400 — duration field now parsed via shorthand helper (accepts '1h', '30m', '24h', '7d' or raw seconds) and sent as integer seconds to match API contract
- Feat: vs_status now returns VIP(s), Pool, PoolGroup, health (oper_status), reason, throughput, new conn/s, latency, and SE placement
- Feat: se_health VS count derived from multiple known runtime fields (se_vs_list, vs_ref) for accurate counts across Controller versions
- Feat: new pool_list MCP tool — discover pools on controller with optional vs_filter substring; solves "pool names differ from VS names" discovery gap
- Credit: Tim (Taiwan TAM) for comprehensive v1.5.4 bug report

## v1.5.5 (2026-04-15)

- Align with VMware skill family v1.5.5

## v1.5.4 (2026-04-14)

- Security: bump pytest 9.0.2→9.0.3 (CVE-2025-71176, insecure tmpdir handling)

## v1.5.0 (2026-04-12)

### Anthropic Best Practices Integration

- **[READ]/[WRITE] tool prefixes**: All MCP tool descriptions now start with [READ] or [WRITE] to clearly indicate operation type
- **Read/write split counts**: SKILL.md MCP Tools section header shows exact read vs write tool counts
- **Negative routing**: Description frontmatter includes "Do NOT use when..." clause to prevent misrouting
- **Broadcom author attestation**: README.md, README-CN.md, and pyproject.toml include VMware by Broadcom author identity (wei-wz.zhou@broadcom.com) to resolve Snyk E005 brand warnings

### AVI-specific

- **Full tool description rewrite**: All 29 tool descriptions rewritten from 2-7 words to full When/What/Gotchas format with R/W column in SKILL.md

## v1.4.9 (2026-04-11)

- Fix: narrow AKO ingress trigger — only fire for Tanzu/vSphere AKO ingress, not generic nginx-ingress
- Fix: clarify vmware-policy compatibility field (Python transitive dep, not required standalone binary)

## v1.4.8 (2026-04-09)

- Security: bump cryptography 46.0.6→46.0.7 (CVE-2026-39892, buffer overflow)
- Security: bump urllib3 2.3.0→2.6.3 (multiple CVEs) [VMware-VKS]
- Security: bump requests 2.32.5→2.33.0 (medium CVE) [VMware-VKS]

## v1.4.7 (2026-04-08)

- Fix: align openclaw metadata with actual runtime requirements
- Fix: standardize audit log path to ~/.vmware/audit.db across all docs
- Fix: update credential env var docs to correct VMWARE_<TARGET>_PASSWORD convention
- Fix: declare .env config and vmware-policy optional dependency in metadata

# Release Notes

## v1.4.5 — 2026-04-03

- **Security**: bump pygments 2.19.2 → 2.20.0 (fix ReDoS CVE in GUID matching regex)
- **Infrastructure**: add uv.lock for reproducible builds and Dependabot security tracking

## v1.4.3 (2026-04-01)

Initial release — AVI (NSX Advanced Load Balancer) management and AKO Kubernetes operations.

### Features

**Traditional Mode (AVI Controller via avisdk)**
- Virtual Service list, status, enable/disable
- Pool member list, enable/disable (graceful drain)
- SSL certificate list and expiry check
- VS analytics and error logs
- Service Engine list and health check

**AKO Mode (Kubernetes via kubectl/K8s API)**
- AKO pod status, logs, restart, version info
- AKO Helm config view, diff, upgrade
- Ingress annotation validation and diagnosis
- K8s-to-Controller sync status, diff, force resync
- Multi-cluster AKO overview and AMKO status

**Infrastructure**
- Dual authentication: AVI Controller (config.yaml + .env) + K8s (kubeconfig)
- Multi-controller profile support
- Double confirmation for destructive operations
- Audit logging via vmware-policy
- 29 MCP tools + CLI commands
- Doctor command for environment diagnostics