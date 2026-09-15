# DSH / Pi：L1 观察与 Managed Runtime 选型

状态：有证据的实现评估，不是运行时晋级声明。
范围：[Reliability Diagnostics](./long-running-agent-reliability-diagnostics-governed-delivery-v0.zh-CN.md)
与 [Desktop Execution Frontends](./desktop-execution-frontends-v0.zh-CN.md) 的共同目标。
[English](./harness-selection-dsh-pi-v0.md)

## 决策

保留 **DSH 作为 L1 首个事件源**，但不据此宣布它已成为生产 Mode B 的最终首选。
Pi 保留为 managed runtime 候选。前者利用已存在的被动 observer 降低验证成本；后者
必须证明生命周期、provider、崩溃恢复和真实结果，插件事件 fixture 不能替代这些证据。
本评估不提供缺乏测量依据的评分或性能排名。

本文区分 DSH 的两个角色，二者不可混同：**托管有界 Turn 宿主**（LoopX 为一次受治理
Turn 选择的适配器）现已按凭据绑定并交付（见下文）；**L1 事件源与会话归属 runtime**
角色仍是 opt-in，不因前者被晋级，仍需本文 C0、C1、开销、保留与 Mode B 各行。

## 已交付的托管执行面（2026-09-15）

选型受仓库今天实际交付的能力约束，而不只取决于上游 harness 能做什么。托管的单次执行
单元是有界 Turn：

- `loopx turn run-once` 接受 `--host codex-cli|dsh|generic-cli` 与
  `--execution-mode isolated-headless`：LoopX 决定，宿主适配器调用 agent CLI，独立
  validator 证明后置条件，只有通过的结果才会被提交；
- `loopx host-mode-plan` 只有在宿主声明 `typed_host_adapter` 时，才为
  `continue_without_ui` 意图选择 `isolated_headless_turn`；缺少该声明时报该模式未就绪，
  并指出缺失的能力；
- 会话归属（`managed_runtime` 与 `attached_host`）不在本文决定，属于
  [Agent 会话执行模式](./agent-session-execution-modes-v0.zh-CN.md)，该文档同时拥有
  M1-M4 接入里程碑与跨前端投影行。

| 角色 | 来源 | 当前选型 | 晋级门槛 |
| --- | --- | --- | --- |
| 默认托管执行宿主 | LoopX Turn 加 `dsh` 宿主适配器，并绑定到运维方提供的模型端点 | 运维方配置了模型凭据时，托管有界 Turn 的已交付默认值；未配置时默认 `codex-cli` | 保持类型化 host request/result、独立验证与凭据归属运维方的边界；没有同等或更强的契约不替换 |
| 受支持的替代 Turn 宿主 | LoopX Turn 加 `codex-cli` 适配器 | 受支持，但同样必须绑定运维方提供的 provider | 任何托管通道都不得依赖某个人的 CLI 订阅 |
| L1 事件源与会话归属 runtime 候选 | DSH | opt-in，未晋级；有界 Turn 宿主角色见上一行默认值 | 本文 C0、C1、开销、保留与 Mode B 各行被真实执行并通过评审 |
| 可选的可见宿主循环 | Pi | 不是 managed runtime | 先声明按绑定持久化且可回读的会话模式，证明重启下的单执行器行为、"对话不是回执"、宿主本地状态非权威，并提供一条真实宿主重启行 |

### 托管宿主绑定与真实环境验证（2026-09-15）

一个托管宿主绑定要说明四件事：宿主适配器、provider、模型，以及凭据来自哪里。
DSH 绑定是 DSH Turn 宿主 + provider `deepseek-official` + 模型 `deepseek-flash`
（DeepSeek V4.1 Flash），端点取自运维方环境（`DEEPSEEK_BASE_URL`），凭据取自
运维方环境（`DEEPSEEK_API_KEY`）。

LoopX 用该凭据解析托管有界 Turn 的默认宿主
（`loopx/control_plane/turn_driver/host_binding.py`）：配置了 `DEEPSEEK_API_KEY`
时，`loopx turn plan` 与 `loopx turn run-once` 默认走 DSH 宿主；未配置凭据时
保持 `codex-cli`。因此解析到 DSH 宿主的托管通道不会依赖某个开发者本机 CLI
订阅是否可用、是否还有额度或是否已登录；仍然跑在 `codex-cli` 的通道则尚未满足
上表中"绑定运维方提供的 provider"这一门槛。

该绑定已验证：

- 默认值解析本身不需要任何 provider 调用即可验证：配置凭据时选中 `dsh`，
  空值或仅空白的值保持 `codex-cli`，显式 `--host` 仍然优先（PR #4409）；
- 两条 Turn 宿主路径都在真实 SDK 与 runtime（`deepseek-harness-sdk==0.1.5rc1`，
  当前发布通道的固定版本；同一对路径在 `0.1.2a3` 下也通过）下通过：进程内
  `--host dsh` 路径与 `generic-cli` 子进程路径；
- 一次真实托管 Turn 达到 `validated_progress`：宿主执行有界动作，独立 validator
  证明后置条件，随后才发生写回与配额扣减；
- 一次后置条件未被证明的真实 Turn 反向失败关闭：没有写回，配额槽消耗计数保持为 0。

在该绑定成为正式默认值之前仍存在的缺口：

- `deepseek-harness-runtime-bin==0.1.5rc1` 捆绑的 runtime 快照无法按原样启动
  `headless` profile：其中一行会拉起
  `@deepseek-ai/dsh-session-title-first-prompt-llm`，该包 import 了未被收录的
  `@deepseek-ai/dsh-session-title-llm`；解析发生在打包快照内部，因此把该包装进
  profile 目录不会改变结果。当前本地做法是用一条绑定 overlay 关闭受影响的行。
  托管宿主路径不受影响：它不选择 `headless` profile，默认 `sdk` profile 能正常
  启动并干净退出；
- LoopX 的 DSH Turn 组合必须显式列出托管动作所需的工具行
  （`@deepseek-ai/dsh-tool-fs`、`@deepseek-ai/dsh-tool-bash`）。缺少它们时，真实模型
  只能作答而无法动手，Turn 会以验证失败而不是产出工作结束。
- 宿主模式计划仍把无人值守意图映射到兼容路径：`isolated_headless_turn` 的
  `turn_host` 取 `generic-cli`（`loopx/host_mode_planner.py`），因此它打印的
  `loopx turn plan` 命令写的是 `--host generic-cli`，而不是上文记录的、按凭据解析出
  的 `dsh` 默认值。该计划的 `--host-identity` 列表只覆盖可见宿主是有意为之——像
  `dsh` 这种仅 headless 的宿主无法拥有可见会话；但无人值守映射本身仍需在"写出解析
  后的默认值 / 提供 `dsh` 变体 / 把该命令标注为回滚路径"之间做出决定。

## 证据基线

LoopX 检查基线为 `bf217e1e01bec79f357c9ecbd580cf2dfa73db8b`：

- `packages/dsh-loopx-plugin/src/observer.ts`：完整身份激活、事件压缩、首次落盘安全、
  有界 buffer 和 flush 隔离。
- `loopx/capabilities/reliability_diagnostics/{receipt,projection}.py`：独立验证、
  integrity 分类和无控制权限的诊断输出。
- `loopx/dsh_goal_mode/turn_host_adapter.py`：有界 Turn、session lineage、SDK 调用和
  失败映射，并不是完整 Desktop 外循环。
- `loopx/pi_goal_mode/{loopx-goal.ts,pi-goal-loop-runtime.mjs}`：有绑定及 continuation
  行为的可见宿主集成，不是被动 observer。
- `apps/desktop/loopx-control-plane/src-tauri/src/services.rs`：已有服务进程管理不等于
  RFC 所要求的完整 managed Agent 生命周期。

LoopX 自身的 dsh 固定版本跟随最新发布通道，而不是未发布的 tag：PyPI 上的
`deepseek-harness-sdk==0.1.5rc1` / `deepseek-harness-runtime-bin==0.1.5rc1`，与 npm
`@deepseek-ai/dsh` 的 `latest` 一致（2026-09-15 核对）。上游 `next` 与 `alpha` tag
比该通道更新，这里不采纳。

2026-09-06 独立检查的上游版本，不等同于 LoopX 已验证的安装版本：

- [DSH d347e703 README](https://github.com/deepseek-ai/deepseek-harness/blob/d347e703908d0406b7a7ef80e3a0e594d86b2215/README.md)：
  Cordis/plugin 架构，明确处于可能不兼容升级的 developer preview。
- [Pi 9767ba27 SDK](https://github.com/earendil-works/pi/blob/9767ba275f3e9a5ee0f5c5342249b629ab1b2282/packages/coding-agent/docs/sdk.md)：
  subscribe、session 操作及 runtime replacement API。
- [Pi 9767ba27 extensions](https://github.com/earendil-works/pi/blob/9767ba275f3e9a5ee0f5c5342249b629ab1b2282/packages/coding-agent/docs/extensions.md)：
  部分 hook 可以注入上下文、阻止工具调用、修改结果。

历史 Pi 仓库地址目前跳转至 `earendil-works/pi`，本次 SDK 文档使用
`@earendil-works/pi-coding-agent`。这是升级时要核对的差异，不是立即替换本地依赖
或假定新旧 API 兼容的理由。

## 按产品要求对比

| 要求 | DSH 证据 | Pi 证据 | 对选型的影响 |
| --- | --- | --- | --- |
| 被动观察 | 已有独立 observer entry、三个 session publication hook、首次落盘拒绝 | SDK 提供 subscribe，extensions 还提供干预型 hook | DSH 已有可验证切片；Pi 应优先订阅而非拦截，并证明隔离 |
| 身份与恢复 | Turn connector 派生 lineage；observer 另外要求精确 goal/session/run | SDK 将 AgentSession 与负责 replacement/resume 的 AgentSessionRuntime 分开 | 两边都要测重启、fork 后身份；有 API 不等于恢复可靠 |
| 单次有界执行 | 已有 timeout 和失败映射 | 当前 Pi Goal 集成含 continuation/pause | 不允许 native loop 与 Desktop supervisor 同时充当外循环 |
| 打包 | 独立 export/bundle、packed smokes | SDK resource loading 会发现 extensions | 检查实际加载的包及 profile；二者都不是 OS 进程隔离 |
| Provider | connector 版本与 SDK 约束明确 | SDK 暴露 runtime/model 构造 | 同 route/model/tools/budget 验证，harness 选择不代表 provider 兼容 |
| 数据安全 | producer/consumer 独立校验，共享反事实 | 工具/context hook 可接触和修改原文 | Pi 需补首次落盘安全及负向测试，不能复制 transcript |
| 开销 | 已有 buffer/count/flush 统计，没有本次匹配实测 | 有订阅接口，没有本次 LoopX observer 测量 | 未实测前不作数字排名 |
| 维护 | 上游明确可能 breaking，LoopX connector 有固定验证版本 | 当前包名与 runtime API 不能直接套用旧集成假设 | 两边升级分别固定版本，不拿已安装 DSH 对比未验证最新 Pi |

这是接入成本和合同差异，不是说 Pi 没有事件，或 DSH 不能使用其他模型。
两个 harness 都有控制 API；“被动”是具体 adapter 和实际加载依赖的性质。

依赖 dsh 的两个 LoopX 面并不一起移动：有界 Turn 宿主使用上文记录的 Python
SDK/runtime 固定版本（`0.1.5rc1`，已发布通道）；而 dsh 侧插件
（`packages/dsh-loopx-plugin`）的开发与客户端面仍构建在 `0.1.1-rc.2` 上，尽管其
clean-Docker smoke 已断言 `dsh --version == 0.1.5-rc.1`。0.1.5 线不再发布
`@deepseek-ai/dsh-client-runtime`（最后发布版本为 `0.1.1-rc.2`），客户端 runner 改为
`@deepseek-ai/dsh-cordis-client-runner`。该升级作为独立的 pin 项跟踪，不改变上文的
L1 observer 契约。

## 数据流与权限

用户需要区分“没有证据”“执行有异常”“观察过程不可信”，而不是只得到一个绿灯：

```text
native session publication
  -> isolated observer: compact / validate / count / append
  -> independent ledger validation
  -> integrity receipt + diagnostic projection
  -> 仅供操作者展示

canonical eligibility -> Desktop supervisor -> bounded Turn -> validation/writeback
```

诊断不得反向进入 eligibility。`valid` 只代表观察合同通过，不代表任务成功；stall
信号不是重试授权，observer 故障也不能被当成 worker 故障。

## 本次落地的读取增量

```bash
loopx reliability-diagnostics status --goal-id <goal-id> --with-receipt --format json --as-of "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

上述 POSIX shell 示例使用当前 UTC 时间评估年龄；其它客户端应传入带时区的当前时间。
仅在历史重放时省略 `--as-of`：默认使用最后事件时间，因此最后事件年龄为零，不能
作为实时存活检查。分别显示观察时间和评估时间；推进评估时钟不改变 integrity。

显式选项让 receipt 与 projection 来自同一次 ledger 读取的内存结果，避免分别执行
两次 CLI 时观察到不同追加状态。不加选项保持原输出。这不提供文件并发追加的原子
快照；末尾半行仍按无效输入报告，不能悄悄丢掉。命令不激活 observer、不发现绑定、
不写 ledger、不调用模型，也不改变 Goal/Todo/lease。

这是可执行的读取接口，**不是已交付的 Mode B 面板或 supervisor**。未来面板必须
绑定精确 goal/session/run，分别展示观察时间、integrity 与任务状态；多 run 或过期
goal ledger 不得被标成当前 session 健康。输出只供操作者，不得进入 prompt 或调度。
现有 CLI 全量 ledger 读取没有大小上限，在引入经过评审的读取预算／快照策略之前，
不能直接拿这个命令做自动轮询。

## 验收方案与停止条件

1. **C0 保真**：比较 native 与 observer 关闭的 managed adapter。固定 model、route、
   tools、prompt、环境、预算、包／adapter 版本及起始 session，计入失败和重试；
   treatment 不一致则不采纳比较结果。
2. **C1 被动观察**：在通过 C0 的 adapter 上只开启 observer。记录完整身份、
   accepted/persisted/rejected/drop 数、receipt、endpoint 和 worker/scheduler influence。
   fixture 通过不等于 C1；非 valid receipt 不作为 eligible C1。
3. **开销**：成对重复测 baseline/observer 的 wall time、CPU、peak RSS、写入字节、
   吞吐、flush latency；报告样本数、分布、不确定性、冷／热启动条件。预算及验收
   阈值在运行前约定，本次不虚构阈值或性能结果。
4. **保留／删除**：owner 选择最大年龄／字节、活跃 writer 处理、支持访问、备份范围
   和删除验证。先 dry-run 盘点再删除，不能为满足大小限制截断活跃 ledger。
5. **Mode B**：在可丢弃 runtime 验证 start/resume/interrupt/close、进程崩溃、过期身份、
   重复完成、超时及 provider 失败；同一时间一个 Turn，canonical validation/writeback
   通过后才扣 quota 或请求下一 Turn。

原始日志、凭据留在 owner-local；公共材料只保留通用方法、固定版本、聚合结果和
安全引用。本文件不授权真实模型执行或删除现有记录。

里程碑归属仍由
[Agent 会话执行模式](./agent-session-execution-modes-v0.zh-CN.md) 决定：本文负责
L1 observer 这条臂的 C0、C1、开销与保留证据，以及上面针对会话归属 runtime 的 Mode B
验收；M1-M4 接入里程碑与跨前端投影行仍归该文档，本文不定义模式推断，也不定义第二个
执行器。

## 后续交付次序

先评审对比结论和 CLI 读取增量；Mode B 面板必须先具备精确 session 读取与有界刷新，
而不是新造一套通用监控。C0/C1 与开销作为单独预算实验，仅把复用修复和安全证据
提交仓库。删除功能等 retention profile 决定后再做。若 Pi 在相同隔离及生命周期
验收下具有更低的实测接入／运维成本，或 DSH 无法通过，再调整偏好。
不得为了让 L1 实验通过而加入 L2 建议、重试权限或新 scheduler。

## 管家通道的会话传输（2026-09-15）

受治理的 Turn 面与管家（manager）会话通道都从同一份 operator 凭据解析默认宿主，
但两者需要的宿主形态不同：Turn 是一次有界工作片段，现有 DSH adapter 已支持；
管家通道还需要一个能持有交互会话的传输，而现有 DSH 面明确不承诺跨 turn 的 DSH
会话连续性。

已实现的行为：配置了 operator 凭据时，管家通道把默认模型绑到 operator provider
（`deepseek-flash`，来源 `operator_credential_default`），执行器也从同一凭据解析；
当解析出的托管宿主没有 chat 传输时，解析结果给出 typed
`dsh_chat_transport_unsupported`，而针对该宿主的会话请求以 typed
`managed_host_chat_transport_unsupported` host-tool gate 失败，不会静默回落到个人
CLI 登录。

| 路线 | 形态 | 代价与风险 |
| --- | --- | --- |
| A. turn-backed 管家传输（优先） | 每个管家 chat turn 在托管宿主上执行一次受治理 Turn（`loopx turn run-once --host dsh`，`isolated-headless`），把有界会话历史作为上下文 | 无双工流式、无跨 turn 宿主会话，每个 turn 都是新 segment；上线前需要明确的工具／沙箱权威与单 turn 成本上限 |
| B. ACP 或 stdio 适配 | 当托管宿主暴露此类接口时，复用 ACP stdio 适配路径（Kiro CLI chat 端点已走此路） | 传输成本最低，但依赖上游接口，目前没有已交付证据 |
| C. codex 端点绑定 operator provider | 让 Codex app-server 直接以 operator provider 启动，保留现有传输与工具面 | 保留流式，但必须证明会话不再以个人登录认证；provider 配置成为宿主状态权威，需要单独 gate |

选型规则：优先 A，因为它复用 LoopX 已经验证过的 Turn 权威、typed host failure、
journal 与配额语义；B 作为上游接口出现时的低成本替代；只有在管家体验确需双工
流式时才评估 C。无论采用哪条路线，都必须证明「一次管家会话的模型工作落在
operator 凭据上，且不存在任何默认指向个人订阅的路径」。本文件不授权为此新增
scheduler、重试权限或第二套监控子系统。
