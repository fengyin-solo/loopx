# DSH / Pi: L1 Observation and Managed Runtime Selection

Status: evidence-backed implementation assessment, not a runtime promotion.
Scope: the shared goals of [Reliability Diagnostics](./long-running-agent-reliability-diagnostics-governed-delivery-v0.md)
and [Desktop Execution Frontends](./desktop-execution-frontends-v0.md).
[中文](./harness-selection-dsh-pi-v0.zh-CN.md)

## Decision

Keep **DSH as the first L1 event source**. Do not infer that DSH is already the
preferred production Mode B runtime. Retain Pi as a managed-runtime candidate.
The first choice minimizes the cost of qualifying an existing passive observer;
the second requires lifecycle, provider, crash-recovery and outcome evidence
that a plugin event fixture cannot supply. No quantitative winner is claimed.

## Shipped Managed Execution Surface (2026-09-15)

Selection is constrained by what the repository ships today, not only by what an
upstream harness can do. The managed bounded execution unit is the governed Turn:

- `loopx turn run-once` accepts `--host codex-cli|dsh|generic-cli` with
  `--execution-mode isolated-headless`: LoopX decides, a host adapter invokes the
  agent CLI, an independent validator proves the postcondition, and only a passing
  result is committed;
- `loopx host-mode-plan` selects `isolated_headless_turn` for the
  `continue_without_ui` intent only when the host declares `typed_host_adapter`;
  without that declaration it reports the mode as not ready and names the missing
  capability;
- session ownership (`managed_runtime` versus `attached_host`) is not decided
  here. It belongs to
  [Agent Session Execution Modes](./agent-session-execution-modes-v0.md), which
  also owns the M1-M4 integration milestones and the cross-frontend projection row.

| Role | Source | Selection today | Promotion gate |
| --- | --- | --- | --- |
| Default managed execution host | LoopX Turn plus the `dsh` host adapter, bound to an operator-supplied model endpoint | shipped default for bounded managed Turns once the operator configured a model credential, otherwise `codex-cli` | keep the typed host request/result, independent validation, and the operator-owned credential boundary; do not replace it without an equal or stronger contract |
| Supported alternative Turn host | LoopX Turn plus the `codex-cli` adapter | supported, and must also be bound to an operator-supplied provider | no managed lane may depend on an individual's personal CLI subscription |
| Opt-in Turn host and first L1 event source | DSH | opt-in, not promoted | the C0, C1, overhead, retention and Mode B rows in this document being run and reviewed |
| Optional visible host loop | Pi | not a managed runtime | declare a per-binding session mode with readback, prove single-executor behavior under restart, "conversation is not a receipt", non-authoritative host-local state, and one real-host restart row |

### Managed host binding and live qualification (2026-09-15)

A managed host binding names four things: the host adapter, the provider, the
model, and where the credential comes from. The DSH binding is the DSH Turn host
with provider `deepseek-official`, model `deepseek-flash` (DeepSeek V4.1 Flash),
an endpoint from the operator environment (`DEEPSEEK_BASE_URL`) and a credential
from the operator environment (`DEEPSEEK_API_KEY`).

LoopX resolves the default host for bounded managed Turns from that credential
(`loopx/control_plane/turn_driver/host_binding.py`): with `DEEPSEEK_API_KEY`
configured, `loopx turn plan` and `loopx turn run-once` default to the DSH host;
with no configured credential they stay on `codex-cli`. A managed lane that
resolves to the DSH host therefore never depends on an individual developer's CLI
subscription being available, funded, or logged in, and a lane that still runs on
`codex-cli` has not yet met the operator-provider part of the binding gate above.

Verified for this binding:

- the resolved default is covered without any provider call: a configured
  credential selects `dsh`, an empty or whitespace-only value keeps `codex-cli`,
  and an explicit `--host` still wins (PR #4409);
- both Turn host paths pass with the real SDK and runtime
  (`deepseek-harness-sdk==0.1.5rc1`, the current released pin; the same pair
  also passed at `0.1.2a3`): the in-process `--host dsh` path and the
  `generic-cli` subprocess path;
- one live governed Turn reached `validated_progress`: the host executed the
  bounded action, an independent validator proved the postcondition, and only
  then did writeback and quota spend follow;
- a live Turn whose postcondition was not proved fail-closed instead: no
  writeback, and the quota slot spend count stayed at zero.

Open gaps before this binding is a promoted production default:

- the runtime snapshot bundled as `deepseek-harness-runtime-bin==0.1.5rc1`
  cannot boot the stock `headless` profile as shipped: a profile row pulls
  `@deepseek-ai/dsh-session-title-first-prompt-llm`, which imports the omitted
  `@deepseek-ai/dsh-session-title-llm`, and resolution runs inside the packaged
  snapshot, so installing that package into a profile directory does not change
  it. The current local workaround is a binding overlay that disables the
  affected row. The managed host path is unaffected: it does not select
  `headless`, and the default `sdk` profile boots and exits cleanly;
- the LoopX DSH Turn composition must name the tool rows a managed action needs
  (`@deepseek-ai/dsh-tool-fs`, `@deepseek-ai/dsh-tool-bash`). Without them a live
  model can answer but cannot act, and the Turn ends in a validation failure
  rather than in work.

## Evidence Baseline

LoopX was inspected at `bf217e1e01bec79f357c9ecbd580cf2dfa73db8b`.
The implementation paths below are repository-relative:

- `packages/dsh-loopx-plugin/src/observer.ts`: pinned activation, session event
  compaction, first-append safety, bounded buffering and flush isolation.
- `loopx/capabilities/reliability_diagnostics/{receipt,projection}.py`: independent
  validation, integrity classification and authority-free diagnostic readback.
- `loopx/dsh_goal_mode/turn_host_adapter.py`: a bounded Turn connector, opaque
  session lineage, SDK calls and failure translation, not a desktop outer loop.
- `loopx/pi_goal_mode/{loopx-goal.ts,pi-goal-loop-runtime.mjs}`: a visible-host
  integration with bindings and continuation behavior; not a passive observer.
- `apps/desktop/loopx-control-plane/src-tauri/src/services.rs`: service process
  management must not be mistaken for the complete managed Agent lifecycle.

LoopX's own dsh pin tracks the newest released upstream channel rather than an
unreleased tag: `deepseek-harness-sdk==0.1.5rc1` /
`deepseek-harness-runtime-bin==0.1.5rc1` on PyPI, matching `latest` for
`@deepseek-ai/dsh` on npm (checked 2026-09-15). Upstream `next` and `alpha` tags
are newer than that channel and are not adopted here.

Upstream references were inspected on 2026-09-06, pinned independently of the
versions validated by LoopX:

- [DSH README at d347e703](https://github.com/deepseek-ai/deepseek-harness/blob/d347e703908d0406b7a7ef80e3a0e594d86b2215/README.md):
  Cordis/plugin architecture and explicit developer-preview compatibility risk.
- [Pi SDK at 9767ba27](https://github.com/earendil-works/pi/blob/9767ba275f3e9a5ee0f5c5342249b629ab1b2282/packages/coding-agent/docs/sdk.md):
  event subscription, session operations and runtime replacement APIs.
- [Pi extensions at 9767ba27](https://github.com/earendil-works/pi/blob/9767ba275f3e9a5ee0f5c5342249b629ab1b2282/packages/coding-agent/docs/extensions.md):
  event hooks with context-injection, tool-blocking and result-modification power.

The historical Pi repository URL now redirects to `earendil-works/pi`; the
inspected SDK uses `@earendil-works/pi-coding-agent`. This is an upgrade-check
input, not permission to replace LoopX's installed package or assume API parity.

## Comparison by Product Requirement

| Requirement | DSH evidence | Pi evidence | Selection consequence |
| --- | --- | --- | --- |
| Passive observation | LoopX ships a separate observer entry, three session publication hooks and pre-append rejection | SDK offers `session.subscribe`; extensions also offer interception hooks | DSH has a qualified contract slice; a Pi adapter must choose subscription over intervention and prove isolation |
| Session identity / resume | Existing Turn connector derives session lineage; observer separately requires exact goal/session/run identity | SDK separates AgentSession from AgentSessionRuntime replacement/resume operations | Test identity after restart/fork for each adapter; method availability is not durable recovery proof |
| One bounded attempt | LoopX already has a DSH Turn host with timeout and failure mapping | Existing Pi goal mode includes continuation and pause behavior | Neither native loop may silently become the Desktop scheduler; avoid two outer loops |
| Packaging | Dedicated observer export/bundle and packed smokes exist | Extension discovery is part of SDK resource loading | Verify the actually loaded package/profile, not just source imports; neither boundary is OS isolation |
| Provider profiles | SDK connector/version constraints are explicit | SDK exposes runtime/model construction | Qualify the same route, model, tools and budget; harness choice does not establish provider compatibility |
| Public safety | Producer and Python consumer independently validate; shared counterfactuals exist | Tool/context hooks can expose or change raw content | A Pi observer needs first-append redaction and negative fixtures, not transcript copying |
| Performance | Buffer/count/flush accounting exists; no matched real overhead result established here | Subscription is available; no LoopX observer measurement established here | Reject numeric rankings until identical workloads and revisions are measured |
| Maintenance | DSH upstream explicitly warns of breaking changes; LoopX pins its validated connector surface | Current upstream package/runtime APIs differ from historical integration assumptions | Pin upgrades separately; do not compare an installed DSH against an unqualified latest Pi |

These are integration-cost and contract observations, not claims that Pi lacks
events or DSH cannot support other models. Both expose control-capable APIs;
passivity is a property of the selected adapter and its loaded dependencies.

## Data and Authority Flow

The operator needs to distinguish missing evidence, unhealthy execution and
an invalid observation treatment before deciding what to do:

```text
native session publication
  -> isolated observer: compact, validate, count, append
  -> independent ledger validation
  -> integrity receipt + diagnostic projection
  -> operator presentation only

canonical eligibility -> Desktop supervisor -> bounded Turn -> validation/writeback
```

There is no arrow from diagnostics back to eligibility. `valid` means the
observation contract passed, not that a task succeeded. A stall signal is not
permission to retry. Observer errors must not become worker failures.

## Implemented Readback Increment

The existing CLI now supports an explicit combined read:

```bash
loopx reliability-diagnostics status --goal-id <goal-id> --with-receipt --format json --as-of "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

The POSIX-shell example evaluates age against the current UTC time. Other
clients must supply a timezone-aware current timestamp. Omit `--as-of` only
for historical replay: it defaults to the last event time, producing zero
last-event age, not a live liveness check. Display the observation and evaluation
times separately; advancing the evaluation clock does not change integrity.

The receipt and projection derive from the same in-memory ledger reading,
avoiding two CLI calls observing different append states. Omitting the flag
preserves the original response. This does **not** make concurrent file append
atomic: a partial last line remains an invalid-input signal rather than being
silently dropped. The command does not activate an observer, discover a binding,
write a ledger, call a model, or change a Goal/Todo/lease.

This is an executable readback seam, **not a shipped Mode B panel or supervisor**.
A future panel must bind exact goal/session/run identity, show observation age
and integrity independently of task status, and refuse to label a multi-run or
stale goal ledger as the current session's health. It must remain operator-only,
with no diagnostic input passed into prompts or scheduler decisions. Existing
CLI ledger reads are unbounded; do not put this command on an automatic polling
loop before adding an owner-reviewed read budget/snapshot strategy.

## Qualification Plan and Stop Conditions

1. **C0 adapter fidelity:** compare native execution with the managed adapter,
   observer disabled. Pin model, route, tool definitions, prompts, environment,
   budget, package/adapter revisions and initial session state. Account for all
   retries and interruptions. Reject comparisons with unequal treatments.
2. **C1 passive arm:** enable only the observer on that qualified adapter. Record
   eligible run identity, persisted/accepted/rejected/drop counts, receipt status,
   endpoint/worker-context/scheduler influence and all failed runs. Fixture success
   does not establish this gate; non-valid receipt is not eligible C1 evidence.
3. **Overhead:** measure baseline and observer wall time, process CPU, peak RSS,
   bytes written, event throughput and flush latency using paired repeated runs.
   Report sample count, distributions, uncertainty and warm/cold conditions.
   Declare acceptance thresholds before running; no threshold is invented here.
4. **Retention/deletion:** owner chooses maximum age/bytes, active-writer handling,
   export/support access, backup scope and delete verification. Dry-run inventory
   must precede deletion; never truncate an active ledger to meet a size cap.
5. **Mode B acceptance:** separately exercise start/resume/interrupt/close,
   process crash, stale session identity, duplicate completion, timeout and
   provider failure in a disposable runtime. Verify one Turn at a time and
   canonical validation/writeback before spending quota or requesting another.

Keep raw logs and credentials owner-local. Public evidence should contain only
generalized methodology, pinned revisions, aggregate results and safe references.
No live model execution or retention deletion is authorized by this document.

## Delivery Sequence

This comparison plus combined CLI readback can be reviewed now. A Mode B panel
requires the exact-session read contract and bounded refresh path first; it must
not be a second generic monitoring subsystem. Run C0/C1 and overhead experiments
as separately budgeted work, then submit only reusable fixes and safe evidence.
Implement deletion only after the owner selects the retention profile. Revisit
runtime preference if Pi satisfies the same isolation/lifecycle tests at lower
measured integration and operational cost, or DSH fails them. Do not introduce
L2 advice, retry control or a new scheduler to make an L1 experiment pass.

## Steward Channel Chat Transport (2026-09-15)

The governed Turn surface and the steward (manager) chat channel resolve their
default host from the same operator credential, but they need different host
shapes. A Turn is one bounded work segment, which the shipped DSH adapter
serves today. The steward channel additionally needs a transport that can hold
an interactive session, and the shipped DSH surface explicitly does not promise
cross-turn DSH session continuity.

Shipped behaviour: with an operator credential configured, the steward channel
defaults its model to the operator provider (`deepseek-flash`, source
`operator_credential_default`) and resolves its executor from the same
credential. When the resolved managed host has no chat transport, resolution
reports the typed `dsh_chat_transport_unsupported` reason and a session request
for that host fails as the typed `managed_host_chat_transport_unsupported`
host-tool gate instead of silently falling back to an individual CLI login.

| Option | Shape | Cost and risk |
| --- | --- | --- |
| A. Turn-backed steward transport (preferred) | Each steward chat turn runs one governed Turn on the managed host (`loopx turn run-once --host dsh`, `isolated-headless`), with bounded chat history as context | No duplex streaming and no cross-turn host session; each turn is a fresh segment. Requires an explicit tool/sandbox authority and a per-turn cost bound before it ships |
| B. ACP or stdio adapter | Reuse the ACP stdio adapter path (as the Kiro CLI chat endpoint does) when the managed host exposes such an interface | Lowest transport cost, but depends on an upstream interface that no shipped evidence covers yet |
| C. Codex endpoint bound to the operator provider | Start the Codex app-server itself against the operator provider so the existing transport and tool surface stay | Keeps streaming, but must prove the session no longer authenticates with an individual login; the provider config becomes host-state authority and needs its own gate |

Selection rule: prefer A, because it reuses the Turn authority, typed host
failure, journal and quota semantics LoopX already validates; keep B as the
cheaper replacement if the upstream interface appears; evaluate C only if
duplex streaming is required for the steward experience. Whichever option ships
must demonstrate, for one steward session, that the model work lands on the
operator credential and that no default path reaches an individual
subscription. This document authorizes no new scheduler, retry authority or
second monitoring subsystem to make that demonstration pass.
