# 统一配置系统设计方案：一方案一单文件 + 指针切换 + 积木式组合

- 状态：设计稿（本文档为设计交付，不含任何代码改动）
- 版本：v3.0（最终定版：v2.0 全部机制 + 第 10 章并发任务、对讲机集群部署、快速部署与本地 PostgreSQL）
- 关联：`docs/ARCHITECTURE.md`、《海能达行业智能体平台_专注阅读与全模块编辑_V6.html》第 04/06/10 章、`docs/ARCHITECTURE_COMPONENTS.md`（积木↔代码组成↔功能对照）
- 适用范围：`adk_gateway_x86` / `agent_server` / `mcp_service` / `terminal_access/{repeater,multimode}` 全部已实现链路，以及未来接入侧与第三方扩展

---

## 0. 摘要

本方案将当前散落在 `.env.agent`、`.env.gateway`、`config/gateway.ini`、`MULTIMODE_*` 环境变量、JSON 目录文件和未装载行业包中的全部部署参数，收敛为**方案库**：每种部署方案保留一个全量单文件（`config/profiles/<方案id>.yaml`），文件内声明该方案的全部链路、设备与参数。**切换方案 = 改一个指针参数（`HYTERA_PROFILE=<方案id>` 或启动器 `--profile <方案id>`）+ 重启，全程不编辑任何配置文件。**

核心机制是四层解耦：

1. **一方案一单文件全量声明**——每个方案文件全量描述该方案的全部参数（不做差量叠加、不跨文件共享锚点，杜绝静默拼装错误）；
2. **指针选择 + 启动期物化**——启动器按指针参数选出方案文件，在拉起任何进程之前把 YAML 渲染为各进程今天就在读的 env / ini 文件（或直接注入环境变量），**进程内读取配置的代码零改动**，天然向后兼容；
3. **积木式组合层（第 7、8 章）**——积木目录定义每块积木的类型化端口（能接什么、不能接什么）；方案文件用 `composition` 段自由选择进程积木（modules）、能力目录积木（catalogs）、流程积木（flows）并声明接线；**不合法的连接与积木冲突在校验期直接拒绝并给出可接/冲突清单**，接线与顺序自动派生启动编排；统一能力层由此支持按行业/场景切换不同工具目录与校验规则，外部工作流以流程积木或外部流程能力两种形态注入；
4. **冻结既有读取路径**——端口、路由常量、wire 协议、MCP 工具契约、兼容性清单全部不动；切换永远走"停旧起新"重启，不做热加载。

在此之上叠加**终端/设备级覆盖段**（`terminals` / `devices`），使同一套部署内不同终端（调度台、多模 APK、测试入口）与不同设备（机器狗、机器人）可以拥有差异化的模型、策略与能力视图，覆盖只收紧、不放宽。

**v2.0 完成五项评审**（组合路径清晰性、功能与组合结果直观性、冲突与重复开发治理、低耦合高内聚、演进充分性，结论见第 9 章）；**v3.0 补齐生产化三要素**：并发任务、对讲机集群部署、快速部署与本地 PostgreSQL（非 Docker），见第 10 章。

---

## 1. 现状调研结论

### 1.1 两条已实现链路

**链路 A：窄带对讲链路（repeater，已完整运行）**

```text
窄带终端 ⇄ 中转台(ADK/UDP-RTP) ⇄ adk_gateway_x86 (18081, config/gateway.ini)
        → agent_server (18082, /voice/callback, outbox 回调)
        → mcp_service (18083) → 网关下发 → 终端
```

- 配置源：`config/gateway.ini`（adk/network/ports/audio/behaviour/rrs/server/protobuf_tools/channel/recording 各段）+ `.env.gateway`（token、CONTROL_IDS、VOICE_CALLBACK_* outbox 族、EVENT_JOURNAL_* 等）。
- 下行投递由 `agent_server/feedback/terminal_delivery.py` 经 `shared_contracts/routes.py` 常量路由到 18081。

**链路 B：多模 PTT 链路（multimode，已实现，当前终端无底层通信）**

```text
PTTAgentDemo APK → WSS /stream + HTTP /api/tasks (18084, terminal_access/multimode/)
        → 既有 /voice/callback + /results/latest 契约桥接 agent_server（零 agent 侧改动）
        → 设备控制类工具经网关下发到 MULTIMODE_ACTOR_TERMINAL_ID 对应对讲机
        → 结果文本经任务轮询回 APK
```

- 配置源：`terminal_access/multimode/config.py::MultimodeConfig.from_env()` 读取的全套 `MULTIMODE_*` 环境变量（经 `.env.agent` 注入），外加 `start_dual_process.bat` 的 `MULTIMODE_ENABLE` 开关。

### 1.2 配置散点清单（现状）

| 配置源 | 影响进程 | 内容 |
|---|---|---|
| `.env.agent` | agent_server + mcp_service + multimode | token、回调、ASR provider/端点、模型、planner 模式、`MULTIMODE_*` 全部键 |
| `.env.gateway` | adk_gateway_x86 | token、中转台身份、outbox 回调族、事件日志 |
| `config/gateway.ini` | adk_gateway_x86 | ADK runtime、网卡/端口矩阵、音频、通道策略 |
| OS 环境变量 / Windows 注册表直读 | 全部 | 凭据（DASHSCOPE_API_KEY 等）注入通道 |
| `voice_contact_map.json` / `voice_device_directory.json` | agent_server 目录解析 | 联系人 / 区域-信道-设备目录 |
| `DEVICE_EXECUTOR_MODE`（`mcp_service/config.py`） | mcp_service | print_stub / http_forward / gateway_terminal / resource_domain(预留) |
| `industry_profiles/default/` | （未被运行时装载） | manifest / ontology / prompts / policies / bindings 模板 |
| `mcp_service/tools/*.yaml` | mcp_service | 工具契约唯一来源（冻结，不在本方案范围内改动） |

### 1.3 现状问题

1. **一次切换要改 3~4 处**：换一台中转台/网卡需要同时改 `gateway.ini` 的 `[network]`、`.env.gateway`、可能还有 `.env.agent` 中的 `HYTERA_ADK_BASE_URL`；漏一处即静默故障。
2. **跨进程一致性靠人肉**：`HYTERA_LOCAL_API_TOKEN` 在两个 env 文件中要求一致，`CONTROL_IDS`/`LOCAL_CONTROL_ID` 要求一致，均无机器校验。
3. **注册表直读与文件配置来源混杂**，排障时无法确定"当前生效值"来自哪里。
4. **行业包/第三方适配无统一装载入口**，未来接入侧和第三方扩展没有声明式登记位。

---

## 2. 核心设计决策

### 2.1 设计目标与约束

- **G1** 每种方案保留一个全量单文件（`config/profiles/<方案id>.yaml`）；仅通过一个指针参数（`HYTERA_PROFILE` 或启动器 `--profile`）即可切换到对应方案，切换过程不编辑任何配置文件。
- **G2** 整套代码向后兼容：不改任何进程内读取配置的既有代码路径，旧部署方式（无 system.yaml）行为与今天完全一致。
- **G3** 高内聚低耦合：接入侧差异收敛在 `terminal_access/<link>/` + system.yaml 一段；厂商差异收敛在 `third_party_adapters/` + 厂商包；行业差异收敛在 `industry_profiles/` 行业包；核心编排零感知。
- **G4** 承接 HTML 第 06 章配置层级："平台标准契约 → 厂商适配包 → 行业场景包 → 站点/客户覆盖 → 运行环境凭据"，安全底线不可被下级配置放宽。
- **G5** 积木式组合：在方案文件内可自由选择启用/禁用哪些模块；模块间承接关系由类型化契约目录（`config/module_catalog.yaml`）强制——能接的组合才可配置，不能接的组合在校验期直接拒绝并说明原因与可接清单。
- **G6** 内容积木与冲突治理：能力目录、流程、包按全局限名（`<ns>:<type>/<name>@<semver>`）注册，标识全局唯一；注册期/装载期/运行期三层冲突检测（重名、资源域双归属、wire 越权、overlay 互斥、流程引用缺失、排他资源并发等），任何冲突在校验期拒绝并输出双方全名与修复建议。

### 2.2 四层架构

```text
┌─────────────────────────────────────────────────────────────────┐
│  指针参数：HYTERA_PROFILE=<方案id>（或启动器 --profile <方案id>）      │
└──────────────────────────────┬──────────────────────────────────┘
                               ▼  按指针选出方案文件（切换不编辑文件）
┌─────────────────────────────────────────────────────────────────┐
│  config/profiles/<方案id>.yaml   方案库：一方案一个全量单文件           │
│  全量声明：meta / composition / links / agent / mcp / models /     │
│           security / terminals / devices / industry_profile /      │
│           third_party                                             │
│  composition 段 ◄── 校验时对照 config/module_catalog.yaml（积木目录）  │
└──────────────────────────────┬──────────────────────────────────┘
                               │ 启动器前置一步：组合校验 → 校验 → 物化
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│  物化产物（runtime/generated/，进程启动前生成）                        │
│   env.agent  env.gateway  gateway.ini  +  启动器环境变量直接注入        │
└──────────────────────────────┬──────────────────────────────────┘
                               │ 各进程既有读取路径（零改动）
                               ▼
     agent_server/common/env.py::load_dotenv     ← env.agent
     terminal_access/repeater/settings/model.py ← gateway.ini
     mcp_service/config.py                      ← env.agent / 环境变量
     terminal_access/multimode/config.py        ← MULTIMODE_* 环境变量
```

**为什么选"启动期物化"而不是"运行时配置访问器"**（延续既有工程权衡决策）：

- 现有代码在模块 import 时以常量形式读取配置（如 `mcp_service/config.py`、repeater settings），运行时访问器改动面巨大且易引入半新半旧状态；
- 物化方案让"单文件"与"零改动"同时成立：方案文件是唯一编辑面（编辑只发生在方案创建/维护时，日常切换不编辑任何文件），物化产物是唯一运行面，两者由确定性渲染器保证一致；
- 切换永远走 `校验 → 停旧 → 物化 → 按序重启 → 冒烟` 的整体动作，不做热加载。

**冻结清单（不在本方案内改动）**：端口 18081-18084；`shared_contracts/routes.py` 全部路由常量；`terminal_access/wire/` 协议与 `contracts/gateway_compatibility.json`；`mcp_service/tools/*.yaml` 工具契约；`shared_contracts/` 数据模型。

### 2.3 三种变更粒度：方案级切换 / 方案内微调 / 终端级覆盖

| 粒度 | 修改位置 | 生效方式 | 典型用途 |
|---|---|---|---|
| **方案级切换** | 仅指针参数：`HYTERA_PROFILE=<方案id>`（或启动器 `--profile <方案id>`） | 组合校验 → 启动校验 → 停旧 → 物化 → 按组合拓扑序（今天等价 MCP→Agent→Gateway→Multimode）重启 → 冒烟；**不编辑任何方案文件** | 整套切换：窄带实验室方案 ↔ 多模现场方案、生产态 ↔ 评估态、含整块积木增减的重组方案 |
| **方案内微调** | 编辑 `config/profiles/<方案id>.yaml` 对应段（仅发生在方案维护时） | 同上动作链，重启一次生效 | 调整某方案的 ASR provider、中转台 IP、执行器模式 |
| **终端/设备级覆盖** | 方案文件内 `terminals` / `devices` 段 | 重启一次生效，运行期按请求解析自然分流 | 调度台用更强模型、测试入口策略收紧、机器狗只开放巡检能力子集 |

覆盖规则遵守既有设计原则：**覆盖只收紧、不放宽**；权限/风险上限/能力范围取"全局默认与覆盖"的严格侧；未知键回落全局默认并告警；执行器模式（gateway_terminal/print_stub/http_forward/resource_domain）属进程装配项，只能随方案级切换或方案内微调改变，不可下放到终端级。

### 2.4 明确不做的事

- 不做运行时热加载 / 不做微服务拆分 / 不改冻结契约（对齐 HTML 1.2 节建设边界）。
- 切换动作不修改任何方案文件内容；方案文件之间不做跨文件 YAML 锚点/差量共享（复制为普通值），防止"改 A 方案悄悄影响 B 方案"。
- 不把凭据写进 system.yaml 或物化产物文件：凭据一律 `${ENV:VAR}` 引用，由启动器从进程环境/用户级注册表注入（对齐 HTML 6.2 安全要求）。
- 不把行业包内容内联进主文件：主文件只登记"启用哪个包"，内容治理留在包内（对齐 HTML 6.1 两类配置包边界）。
- 方案文件不能发明积木目录之外的新模块：新模块（新接入链路/新模型提供方/新插件）只能随代码版本登记进 `module_catalog.yaml`（见第 7 章），配置层只做选择与接线。

---

## 3. 方案文件结构设计

### 3.0 方案库布局与指针解析（一方案一文件）

```text
config/
  profiles/
    lab-repeater.yaml        # 方案A：实验室窄带链路（全量单文件）
    lab-multimode.yaml       # 方案B：多模 APK 链路 + 网关下发（全量单文件）
    field-site-a.yaml         # 方案C：现场站点（未来扩展，同样只是一个文件）
  system.yaml                 # 兼容入口：无指针时使用（等价于 profiles/system.yaml）
  system_mapping.yaml         # 物化映射声明表（全局唯一，被所有方案共用）
  active_profile              # 可选指针文件：一行方案 id（环境变量不便使用时的替代）
```

**指针解析顺序（高 → 低）**：

1. 启动器命令行参数：`start_dual_process.bat --profile lab-multimode`（一次性，优先级最高）；
2. 环境变量 `HYTERA_PROFILE`（`setx` 设置一次，日常切换只改它）；
3. 指针文件 `config/active_profile`（内容为一行方案 id）；
4. `config/system.yaml`（v1.0 兼容形态：无指针且存在该文件时直接使用）；
5. 全部缺失 → 回退现状路径（`.env.agent` / `.env.gateway` / `config\gateway.ini`），行为与今天逐字节一致。

**切换动作（不编辑任何配置文件）**：

```text
setx HYTERA_PROFILE lab-multimode
→ 重启启动器（或临时 --profile lab-multimode 启动一次）
→ 校验目标方案（附录 B）→ 停旧（draining）→ 物化
→ MCP→Agent→Gateway(→Multimode) 按序重启 → 冒烟确认
→ 失败则指针切回上一个已验证方案，并按其重新物化启动
```

**规则**：

- 方案 id 仅允许 `[a-z0-9_-]`，与 `config/profiles/<方案id>.yaml` 文件名一一对应；
- 每个方案文件独立全量、独立校验；方案之间不共享跨文件 YAML 锚点（复制粘贴为普通值）；
- 指针指向不存在的方案 → 启动器 fail-fast 并打印可用方案清单；物化器提供 `--list` 列出方案库及各方案 meta 摘要；
- 同一方案 id 下 `meta.config_version` 只在方案文件被编辑时递增；指针切换不改任何 `config_version`，两类变更在审计上可区分；
- 若客户坚持物理上仅一个文件：可把方案库合并为单文件内的 `profiles:` 映射段（同一 schema 按 `<方案id>:` 分节），指针语义不变，由同一加载器支持。默认推荐一方案一文件，便于 diff、评审与回退。

### 3.1 顶层分段（方案文件通用 schema）

```yaml
meta:              # 配置身份与兼容基线
composition:       # 积木式组合：进程模块（modules）+ 能力目录（catalogs）+ 流程（flows）+ 接线（见第 7、8 章）
system:            # 部署形态指针（生产/评估/仿真）与全局开关
defaults:          # 全局默认（终端/设备覆盖的基线）
links:             # 接入链路注册表（每条链路一段，全量参数）
  repeater: {}
  multimode: {}
  # <future_link>: {}
agent:             # agent_server 进程参数（.env.agent 的结构化形态）
mcp:               # mcp_service 进程参数
models:            # 模型路由与凭据引用
security:          # 跨进程令牌、TLS、凭据引用
storage:           # 持久化后端（SQLite 回退 / 本地 PostgreSQL）与连接池
terminals:         # 终端目录 + 终端级覆盖（模型/策略/能力）
devices:           # 设备目录引用 + 设备级覆盖（能力白名单）
industry_profile:  # 启用的行业包（仅指针，不含包内容）
third_party:       # 第三方厂商适配器注册与 resource_domain 绑定（预留）
```

### 3.2 完整示例（带注释，与当前实际值一一对应）

```yaml
# =============================================================================
# config/profiles/lab-repeater.yaml —— 方案文件（方案库中一方案一文件）
# 切换方案：setx HYTERA_PROFILE <方案id> 或启动器 --profile <方案id>，不改本文件。
# 维护方案：修改本文件后递增 meta.config_version，并通过校验器后发布。
# 规则：全量声明、不做差量叠加；凭据用 ${ENV:VAR} 引用，不落明文。
# =============================================================================

meta:
  profile_id: lab-repeater        # 方案 id = 文件名（指针切换的单位）
  config_version: 1               # 本方案文件的配置版本号，仅当方案文件被编辑时递增
  compat_baseline: v0.2           # 对应 contracts/gateway_compatibility.json 基线
  description: 实验室：中转台 192.168.64 网段 + 多模 APK 双链路

system:
  # 部署形态：production / evaluation / simulation
  mode: production
  # 事件日志与可观测性总开关（映射 EVENT_JOURNAL_* / OTEL_PROPAGATION_ENABLE）
  event_journal:
    enable: false
    fail_mode: open              # open / closed
  otel_propagation_enable: false
  event_type_unknown_policy: allow

defaults:
  # 终端/设备覆盖的全局基线；覆盖段未写的键一律取这里的值
  models:
    planner: qwen-plus
    summary: qwen-plus
    translation: qwen-plus
  policy:
    auto_execute_max_risk: 2      # 覆盖只能比此值更严
    allow_tools: null             # null = 不裁剪，使用完整工具目录

# ---------------------------------------------------------------------------
# 链路 A：窄带对讲（repeater / adk_gateway_x86, 18081）
# ---------------------------------------------------------------------------
links:
  repeater:
    enabled: true
    server:                       # HTTP 服务（→ config/gateway.ini [server]）
      host: 127.0.0.1
      port: 18081
      watchdog_interval_seconds: 2.0
    agent_base_url: http://127.0.0.1:18082   # → HYTERA_AGENT_BASE_URL
    adk:                          # → gateway.ini [adk]/[audio]/[behaviour]/[rrs]
      runtime_dir: ../runtime/ADK
      bootstrap: ini
      managed_ini: true
      detector_type: 2
      listener_ip: 0.0.0.0
      network:
        local_ip: 192.168.64.46   # 换网卡/换中转台时只改这一处
      ports:                      # → gateway.ini [ports]（键名一致）
        rrs_slot1: 30001
        rrs_slot2: 30002
        lp_slot1: 30003
        lp_slot2: 30004
        tp_slot1: 30005
        tp_slot2: 30006
        tmp_slot1: 30007
        tmp_slot2: 30008
        rcp_slot1: 30009
        rcp_slot2: 30010
        rtp_slot1: 30012
        rtp_slot2: 30014
        self_define_slot1: 3017
        self_define_slot2: 3018
      audio:
        mode: adk_audio
        input_device: 0
        output_device: 0
      behaviour:
        auto_channel: true
        auto_attach_audio: true
      rrs:
        auto_ack: true
        registration_valid_time: 60
    channel:                      # → gateway.ini [channel]
      mode: auto
      fallback_for_voice: false
      default_channel_id: null
    protobuf_tools:               # → gateway.ini [protobuf_tools]
      max_request_bytes: 1048576
      private_target_id: 5
      send_timeout_seconds: 15.0
    identity:                     # → .env.gateway CONTROL_IDS / LOCAL_CONTROL_ID
      control_ids: 1048607
      local_control_id: 1048607
    voice_callback:               # → .env.gateway VOICE_CALLBACK_* 族
      delivery_mode: outbox
      outbox_path: voice_capture/callback_outbox.sqlite3
      merge_gap_seconds: 0.4
      outbox_synchronous: NORMAL
      outbox_max_events: 10000
      outbox_max_bytes: 536870912
      outbox_retention_seconds: 604800
      outbox_lease_seconds: 30
      sender_workers: 2
      sender_poll_seconds: 0.05
      retry_max_attempts: 12
      circuit_failure_threshold: 5
      circuit_reset_seconds: 30
    log:
      level: INFO                 # → HYTERA_LOG_LEVEL
      console: true               # → HYTERA_LOG_CONSOLE

  # ---------------------------------------------------------------------------
  # 链路 B：多模 PTT（terminal_access/multimode, 18084）
  # ---------------------------------------------------------------------------
  multimode:
    enabled: true                  # false = 启动器跳过第四服务（等价 MULTIMODE_ENABLE=false）
    host: 127.0.0.1                # → MULTIMODE_HOST
    port: 18084                    # → MULTIMODE_PORT
    access_token_ref: ${ENV:HYTERA_PROFILE_MULTIMODE_TOKEN}   # 凭据引用，不落明文
    agent_base_url: http://127.0.0.1:18082
    limits:
      max_audio_bytes: 6291456
      max_sessions: 256
      session_ttl_seconds: 300
      max_tasks: 1024
      task_ttl_seconds: 600
      task_timeout_seconds: 90
      result_poll_interval_seconds: 0.5
      task_workers: 4
    audio_dir: voice_capture/multimode
    terminal_prefix: multimode
    # 执行者对讲机号：配置后设备控制类工具经链路 A 网关下发到该对讲机；
    # 空 = 字符串虚拟终端（仅查询类任务）。依赖 links.repeater.enabled=true。
    actor_terminal_id: null
    tls:
      certfile_ref: null           # ${ENV:...} 引用或 null（由 Caddy 终结 TLS 时为 null）
      keyfile_ref: null
    future:                        # 预留：control_channel/data_channel 骨架参数位
      control_channel: {}
      data_channel: {}

  # ---------------------------------------------------------------------------
  # 未来链路 C/D...：新增终端形态时在此加一段 + terminal_access/<link>/ 实现
  # 既有 ingress/delivery 契约即可，不触碰核心编排。
  # ---------------------------------------------------------------------------
  # web_desktop:
  #   enabled: false
  #   adapter: terminal_access.web_desktop
  #   ...

# ---------------------------------------------------------------------------
# agent_server 进程参数（.env.agent 的结构化形态；未列键回落现有默认值）
# ---------------------------------------------------------------------------
agent:
  gateway_base_url: http://127.0.0.1:18081   # → HYTERA_ADK_BASE_URL
  callback:
    host: 127.0.0.1                          # → CALLBACK_HOST
    port: 18082                              # → CALLBACK_PORT
  storage:
    save_dir: received_callbacks             # → SAVE_DIR
    state_db_path: data/state.sqlite3        # → STATE_DB_PATH
  directories:
    contact_map_file: voice_contact_map.json
    zone_channel_map_file: voice_zone_channel_map.json
  http_keepalive: false                      # → VOICE_HTTP_KEEPALIVE
  log_level: INFO                            # → VOICE_LOG_LEVEL

# ---------------------------------------------------------------------------
# mcp_service 进程参数
# ---------------------------------------------------------------------------
mcp:
  host: 127.0.0.1                # → MCP_HTTP_HOST
  port: 18083                    # → MCP_HTTP_PORT
  protocol_version: 2025-11-25
  # 执行器模式（进程装配项，仅系统级可切换）：
  #   gateway_terminal（默认，经网关下发对讲机）
  #   http_forward / print_stub（联调）
  #   resource_domain（预留：third_party_adapters 注册表，启用前需完成绑定校验）
  executor_mode: gateway_terminal    # → DEVICE_EXECUTOR_MODE
  http_forward:
    base_url: null               # executor_mode=http_forward 时必填（→ DEVICE_HTTP_BASE_URL）
    timeout_seconds: 10.0

# ---------------------------------------------------------------------------
# 模型路由（defaults.models 的全局来源；凭据一律引用）
# ---------------------------------------------------------------------------
models:
  asr:
    provider: transcriptions     # → QWEN_ASR_PROVIDER：dashscope / local / transcriptions
    model: qwen-audio-3.0-asr-flash-streaming
    language: zh
    enable_itn: true
    dashscope:
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      api_url: https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation
      api_key_ref: ${ENV:DASHSCOPE_API_KEY}
      streaming_url: null
    local:
      base_url: http://10.115.36.37:8000/v1
      model: Qwen3-ASR-1.7B
      api_key_ref: null
    transcriptions:
      base_url: http://192.168.9.31:30181/v1
      language: Chinese
      api_key_ref: ${ENV:QWEN_TRANSCRIPTIONS_ASR_API_KEY}
  planner:
    prompt_mode: signature        # full / compact / signature（→ PLANNER_PROMPT_MODE）
    context_cache_enable: true
    thinking_enable: null
  summary_model: qwen-plus        # → QWEN_CALL_SUMMARY_MODEL
  translation_model: qwen-plus    # → QWEN_CALL_TRANSLATION_MODEL

# ---------------------------------------------------------------------------
# 安全：跨进程一致性约束在此声明，由校验器强制
# ---------------------------------------------------------------------------
security:
  local_api_token_ref: ${ENV:HYTERA_LOCAL_API_TOKEN}   # 三个进程共用同一引用（→ HYTERA_LOCAL_API_TOKEN）
  constraints:
    - token_uniform_across_processes: true   # 校验器：env.agent 与 env.gateway 物化值必须一致
    - registry_credential_only: true        # 注册表直读仅允许凭据通道（HYTERA_ENV_REGISTRY_SYNC=false 语义收敛于此）

# ---------------------------------------------------------------------------
# 持久化：回退 = 现状 SQLite（零改动）；postgres = 集群/多实例形态（第 10 章）
# 本地 PostgreSQL（非 Docker）：本机安装服务，连接凭据经 ${ENV:...} 注入
# ---------------------------------------------------------------------------
storage:
  backend: sqlite                    # sqlite（回退，默认）/ postgresql（集群形态必选）
  sqlite:
    state_db_path: data/state.sqlite3
    outbox_path: voice_capture/callback_outbox.sqlite3
  postgresql:
    host: 127.0.0.1                   # 本机安装的 PostgreSQL 服务（非容器）
    port: 5432
    database: hytera_agent
    user: hytera_agent
    password_ref: ${ENV:HYTERA_PG_PASSWORD}   # 凭据不落方案文件
    sslmode: prefer
    pool_max: 20                     # 连接池上限（并发任务基准）
    schema: public                   # 集群每节点/租户可用不同 schema 隔离
  migrations:
    dir: storage/migrations          # 版本化迁移脚本（复用 calls/migration_runner 迁移轨道）
    auto_apply: true                 # 启动前迁移检查（快速部署组件）

# ---------------------------------------------------------------------------
# 终端目录 + 终端级覆盖（覆盖只收紧；未覆盖键取 defaults）
# ---------------------------------------------------------------------------
terminals:
  directory:
    contact_map: voice_contact_map.json
    device_directory: voice_device_directory.json
  entries:
    dispatcher-console:          # 示例：调度台走更强模型
      kind: repeater
      match: { terminal_id: 1048607 }
      overrides:
        models: { planner: qwen-max }
    multimode-apk:              # 示例：多模入口策略收紧
      kind: multimode
      match: { terminal_prefix: multimode }
      overrides:
        policy: { auto_execute_max_risk: 1 }
    test-entry:                 # 示例：测试入口只读
      kind: multimode
      match: { terminal_prefix: multimode-test }
      overrides:
        policy: { allow_tools_readonly: true }

# ---------------------------------------------------------------------------
# 设备目录 + 设备级覆盖（能力白名单裁剪）
# ---------------------------------------------------------------------------
devices:
  directory: voice_device_directory.json
  entries:
    robot-dog-01:               # 示例：机器狗仅开放巡检能力子集
      match: { device_id: "robot-dog-01" }
      overrides:
        capabilities: [robot.status_query, robot.task_submit, robot.task_cancel]

# ---------------------------------------------------------------------------
# 行业场景包（仅指针；manifest 版本/依赖经校验后由未来 loader 装载）
# ---------------------------------------------------------------------------
industry_profile:
  enabled: false                 # 当前为模板未装载，保持 false 即现状
  profile_id: default
  path: industry_profiles/default
  min_agent_version: 0.1.0

# ---------------------------------------------------------------------------
# 第三方厂商适配注册（对应 HTML 第 05 章；resource_domain 激活前仅登记）
# ---------------------------------------------------------------------------
third_party:
  enabled: false                 # true 时要求 mcp.executor_mode == resource_domain
  adapters:
    robot-vendor-x:
      module: third_party_adapters.adapters.robot
      package: vendor_packages/robot-x/          # 厂商适配包（协议/字段/回执/错误码映射）
      resource_domains: [robot]
      status: registered                          # registered / verified / disabled
  events:
    dedup_window_seconds: 60
```

### 3.3 物化映射表（YAML → 各进程既有读取面）

物化器输出的每个键必须命中下表之一；**出现无法映射的 YAML 键时 fail-fast**（防拼写静默失效）。方案文件与映射表同构：映射表全局唯一，被方案库中所有方案共用。

| YAML 段 | 物化目标 | 目标读取方（零改动） |
|---|---|---|
| `links.repeater.server/adk/channel/...` | `runtime/generated/gateway.ini` 同名段 | `terminal_access/repeater/settings/model.py` |
| `links.repeater.agent_base_url / identity / voice_callback / log` | `runtime/generated/env.gateway` 的 `HYTERA_AGENT_BASE_URL` / `CONTROL_IDS` / `LOCAL_CONTROL_ID` / `VOICE_CALLBACK_*` / `HYTERA_LOG_*` | x86 网关进程 env 读取 |
| `links.multimode.*` | `MULTIMODE_HOST/PORT/ACCESS_TOKEN/...` 环境变量（启动器注入） | `terminal_access/multimode/config.py::from_env` |
| `links.multimode.enabled` | 启动器分支（等价 `MULTIMODE_ENABLE`） | `start_dual_process.bat` |
| `agent.*` | `runtime/generated/env.agent` 对应键（`HYTERA_ADK_BASE_URL`、`CALLBACK_*`、`SAVE_DIR`、`STATE_DB_PATH`、`VOICE_*`…） | `agent_server/common/env.py::load_dotenv` |
| `mcp.*` | `MCP_HTTP_HOST/PORT`、`DEVICE_EXECUTOR_MODE`、`DEVICE_HTTP_*` 环境变量 | `mcp_service/config.py` |
| `models.asr.*` | `QWEN_ASR_PROVIDER`、`QWEN_*_ASR_*`、`DASHSCOPE_*` 键 | `agent_server/semantics/audio.py` 及模型客户端 |
| `models.planner.*` | `PLANNER_PROMPT_MODE`、`PLANNER_CONTEXT_CACHE_ENABLE`、`QWEN_LOCAL_PLANNER_ENABLE_THINKING` | planner 装配 |
| `security.local_api_token_ref` | 同时物化到 env.agent 与 env.gateway 的 `HYTERA_LOCAL_API_TOKEN` | `shared_contracts/security.py` 校验 |
| `system.event_journal / otel` | `EVENT_JOURNAL_*`、`OTEL_PROPAGATION_ENABLE`、`EVENT_TYPE_UNKNOWN_POLICY` | `shared_runtime/event_journal.py` 等 |
| `terminals.entries.* / devices.entries.*` | **不物化**；运行期由 `system_config` 访问器按请求解析 | 新增访问器（见 4.5） |
| `industry_profile / third_party` | 仅登记元数据；装载器激活前不影响任何现有行为 | 未来 loader / `third_party_adapters/routing.py` |

**前缀规则（保证映射可维护）**：物化器维护"YAML 路径 → 目标键"的声明式映射表；新增键时先在映射表登记再使用，未登记键在物化时报错并列出建议目标。

### 3.4 值引用与环境变量注入

- `${ENV:VAR}`：物化时从启动器进程环境（含用户级注册表同步而来的凭据）读取并展开；缺失则 fail-fast 并指明变量名。
- 注册表直读在 system.yaml 模式下**仅允许出现在 `${ENV:...}` 展开链中**（即仅凭据通道），普通参数一律以文件声明为准，保证配置来源确定性（对应既有"注册表陷阱处理"决策）。

---

## 4. 装配层设计

### 4.1 新增模块与职责（全部为新增文件，不改既有文件）

```text
shared_runtime/
  config_schema.py        # 方案文件分段 schema、类型/枚举/范围规则、跨段联动规则
  composition.py          # 积木组合校验：加载 module_catalog → 构图 → 端口/依赖/顺序校验
  config_loader.py        # 解析指针 → 加载方案文件 → 解引用 → 校验 fail-fast → 物化渲染
  system_config.py        # 运行期只读访问器：effective_planner(terminal_id)、
                          # effective_policy(actor_id)、device_capabilities(device_id)、
                          # config_digest()
scripts/
  materialize_system_config.py   # CLI 入口：--profile <id> / --list / --graph；校验+物化
  validate_system_config.py      # 启动前校验器（只读，按方案校验，可单独运行）
  migrate_sqlite_to_postgres.py # （阶段⑥）一次性迁移：SQLite → PostgreSQL（对账报告，只读源）
  provision_health.py           # （阶段⑥）快速部署体检：环境项→勾选→摘要退出码
  deploy.ps1                    # （阶段⑥）快速部署器：安装组件→用户选择→物化→启用服务（§10.3）
config/
  module_catalog.yaml             # 模块目录：每块积木的端口/契约/顺序/关键性（静态，随代码发布）
  profiles/                       # 方案库：一方案一个全量单文件（指针切换的单位）
  system_mapping.yaml             # YAML 路径 → 目标键 的声明式映射表（全局共用）
  active_profile                  # 可选指针文件（一行方案 id）
runtime/generated/                # 物化产物目录（.gitignore；含 digest 头注释）
```

### 4.2 物化流程

```text
start_dual_process.bat [--profile <方案id>]
  └─ 解析指针（命令行 > HYTERA_PROFILE > config\active_profile > config\system.yaml > 无）：
       命中方案文件 →
       1. python scripts\materialize_system_config.py --profile <方案id>
          a. 解析 YAML + 展开 ${ENV:...}（缺失即失败退出）
          b. schema 校验（附录 B 规则清单）fail-fast
          c. 渲染 env.agent / env.gateway / gateway.ini 至 runtime/generated/
            （文件头写入 digest + profile_id + config_version + 物化时间）
          d. 启动器自身环境注入 MULTIMODE_* / MCP_* / PLANNER_* 等
       2. 通过指针变量把产物交给各进程：
            VOICE_AGENT_ENV_FILE = runtime\generated\env.agent   （agent_server 既有支持）
            网关 --config runtime\generated\gateway.ini          （run_gateway 既有支持）
            env.gateway 优先级处理见下
  └─ 指针完全缺失：按现状启动（.env.* / config\gateway.ini 原样使用），行为逐字节一致
```

**env.gateway 传递的两条路线（按阶段选择）**：

- **阶段一（零风险）**：物化器在生成 `runtime/generated/env.gateway` 的同时，将其内容**原位重写**到根目录 `.env.gateway`（先备份 `.bak`）。x86 网关读取路径完全不动。
- **阶段二（可选）**：确认 x86 启动链支持环境文件指针变量后，切换为指针方式，根目录 `.env.*` 退役为物化产物或删除。

> 说明：`agent_server/common/env.py::load_dotenv()` 已支持 `VOICE_AGENT_ENV_FILE` 指针且"系统环境变量优先于 .env"，因此 agent 侧天然支持阶段二；阶段一为 x86 侧提供无侵入过渡。

### 4.3 启动器改造（`start_dual_process.bat`）

改动仅限脚本头部：追加"解析指针（可选第一参数 `--profile` 或 `HYTERA_PROFILE`）→ 物化前置步骤 + 失败即停"。服务启动顺序、健康检查全部保持；`MULTIMODE_ENABLE` 语义由所选方案的 `links.multimode.enabled` 决定（无方案时保持原环境变量语义不变）。物化或校验失败时启动器直接退出并打印首个错误与可用方案清单，**绝不带病启动**。

### 4.4 启动前校验器（`scripts/validate_system_config.py`）

| 类别 | 规则示例 | 失败处理 |
|---|---|---|
| 凭据完整性 | 所有 `${ENV:...}` 引用可解析；`models.asr.provider=transcriptions` 时其 `api_key_ref` 非空 | fail-fast |
| 跨进程一致性 | `security.local_api_token_ref` 物化到 env.agent 与 env.gateway 的值一致；`links.*.agent_base_url` 与 `agent.gateway_base_url` 指向同一 Agent 地址 | fail-fast |
| 联动组合法性 | `links.multimode.actor_terminal_id` 非空时 `links.repeater.enabled=true` 且该号码存在于 `terminals.directory`；`third_party.enabled=true` 时 `mcp.executor_mode=resource_domain`；`defaults.policy.auto_execute_max_risk` ≥ 各 `terminals.entries.*.overrides.policy.auto_execute_max_risk`（覆盖只收紧） | fail-fast |
| 链路依赖闭合 | `links.repeater.adk.network.local_ip` 属于本机网卡；端口 18081-18084 无冲突且不等于冻结值以外的占用 | fail-fast |
| 工具契约冻结 | `mcp_service/tools/*.yaml` 与 `contracts/gateway_compatibility.json` 哈希不因配置切换而变化 | fail-fast |

### 4.5 终端/设备级覆盖解析（`shared_runtime/system_config.py`）

- 进程启动时加载一次物化后的有效配置（含 `defaults` + `terminals` + `devices`），缓存并提供 `config_digest()` 供日志/审计输出。
- 取值链：`显式请求参数 > terminals/devices 覆盖（严格侧合并） > defaults > 代码现有默认值`。
- 挂接点（新增调用，不改既有公开契约）：
  - **Planner/ASR 链**：客户端工厂的 provider/model 来源从全局常量改为 `effective_planner(terminal_id)`（会话层 `terminal_sessions.py` 已能识别终端，路径已通）；
  - **策略链**：`agent_server/security/policy/engine.py::evaluate()` 已接收 `actor_id`/`target_ids`，风险上限与工具 allowlist 改由 `effective_policy(actor_id)` 提供；
  - **能力视图裁剪**：按 `devices.entries.*.overrides.capabilities` 白名单裁剪模型可见工具列表（复用 CapabilityRegistry 既有机制）。
- 一期建议聚焦 Planner 与策略两项挂接（ASR 涉及音频前处理链路较重，二期再放开按终端配置）。

---

## 5. 兼容与扩展策略

### 5.1 向后兼容三原则

1. **无指针 = 今天**：`HYTERA_PROFILE`、指针文件与 `config/system.yaml` 均不存在时走原路径，行为逐字节一致；旧 `config/system.yaml`（v1.0 形态）仍被识别为兼容方案，无需迁移。
2. **未声明键回落现有默认值**：物化只写出 YAML 中声明的键；各进程 `env_*` 函数与 ini 解析的默认值继续兜底。
3. **golden-file 验证**：迁移期执行"物化产物 vs 当前 `.env.agent`/`.env.gateway`/`gateway.ini`"全等对比（凭据字段脱敏后），差异清单必须为空才放行。

### 5.2 新增接入链路（新终端形态）

新增一个 `terminal_access/<link>/` 包，实现既有两链路共同遵守的契约面即可接入：

- **上行**：解码后构造 `shared_contracts.models.VoiceEvent` 并提交 `AGENT_VOICE_EVENT_PATH`（链路 B 的 `AgentVoiceIngress` 即该契约的参考实现）；
- **下行**：实现结果路由/投递（参考 `multimode/control_channel/result_router.py`），或声明复用链路 A 的网关下行（`actor_terminal_id` 模式）；
- **配置**：方案文件的 `links.<link>` 加一段（参数全量声明），映射表登记新增键。

核心编排（semantics/orchestration/security）零改动——这正是"终端接入差异收敛在接入层"的高内聚边界。

### 5.3 新增厂商/设备（第三方）

- system.yaml `third_party.adapters.<vendor>` 登记：适配器模块、厂商适配包路径、资源域、状态（registered/verified/disabled）。
- 执行路径复用**已预留的切换点**：`mcp_service/device_executor.py::build_executor()` 的 `resource_domain` 分支 + `third_party_adapters/routing.py::ResourceDomainRouter`（当前为显式禁用的预留位，激活前提是完成绑定校验、SSRF allowlist、权限与回执对接）。
- 厂商差异（协议、字段映射、错误码、回执语义）全部进**厂商适配包**，不进主文件、不进核心代码——一家厂商一个包、独立版本（对齐 HTML 6.1）。

### 5.4 行业场景包

- 主文件 `industry_profile` 段只声明"启用哪个包"；包内容（ontology 别名、prompts 覆盖、policies、bindings、回归样本）留在 `industry_profiles/<id>/`。
- 装载前校验 `manifest.yaml` 版本与 `min_agent_version` 依赖；**安全底线不可被包覆盖**：包内 policies 只能在主文件 `defaults.policy` 之内收紧。
- 未启用时（`enabled: false`）即当前现状：模板不参与运行。

### 5.5 配置层级与安全底线（对齐 HTML 6.3）

```text
平台标准契约（冻结：routes/wire/tools 目录）      ← 不可由配置改变
  └ defaults（主文件全局默认）                    ← 唯一可放宽层
      └ terminals / devices 覆盖                 ← 只收紧
          └ 行业包 / 厂商包                        ← 只收紧、且需通过校验
              └ 运行环境凭据（${ENV:...}）          ← 仅凭据，不承载业务语义
```

---

## 6. 迁移路径与验收

### 6.1 三阶段迁移

| 阶段 | 内容 | 放行标准 |
|---|---|---|
| ① 影子物化 | 引入 system.yaml + 映射表 + 物化器 + 校验器；产物仅做 golden-file 对比，不接管启动 | 对比差异为空；校验器对现网配置全部通过 |
| ② 切换接管 | 启动器前置指针解析+物化步骤；`.env.agent`/`.env.gateway`/`gateway.ini` 降级为物化产物（阶段一原位重写 + `.bak`）；建立方案库并迁移首个方案文件；指针参数切换上线 | 4 个 `/health` 全绿；repeater 链路语音冒烟；multimode 链路 PTT 冒烟；`evals/text_accuracy.py` 通过；日志可见 profile_id 与 config_digest |
| ③ 扩展激活 | system_config 覆盖挂接（Planner/策略/能力裁剪）；行业包 loader；resource_domain 执行器激活 | 覆盖只收紧负例通过；行业包回归样本通过；新厂商接入不触碰核心编排 |
| ④ 积木组合 | 引入 module_catalog + composition 段；缺省 composition = 当前生产组合（全部启用+标准接线），存量方案文件零迁移；随后开放自由组搭 | 缺省组合行为与阶段③一致；非法连接被拒（含可接清单）；禁用模块不启进程/不物化/不探测；`--graph` 输出与组合一致 |
| ⑤ 能力目录与流程 | capability_catalogs 规范化（core = 现 mcp_service/tools 原位引用，不移动不改动）；catalog_registry + 三段式校验 + 复用防重机制上线；flow 引擎接线与外部流程能力契约；排他资源门控；组合可观测面（/observability/config） | 目录切换可见性正确（启用/禁用即有/无）；overlay 只收紧负例通过；机器狗场景全链路推演通过；三层十三类冲突检测全部拦截（见 8.7）；语义重复登记被拒且提示 alias 路径 |
| ⑥ 生产化三要素 | 并发任务治理（行级锁/全局速率/准入）；storage.backend=postgresql + 迁移工具；对讲机集群 profile（node_id 唯一、网关无状态化）；deploy.ps1 快速部署 + provision_health 体检 | 50 并发压测通过（无互相取消/无双重下发）；SQLite→PG 迁移对账差异为零；集群 N 节点健康且 A/B 分发不互踩；新增服务器上以最少人工步骤完成部署并通过冒烟 |

### 6.2 验收清单

- [ ] 无指针且无方案文件时，启动行为与迁移前逐字节一致（回归三服务 + 冒烟）。
- [ ] `setx HYTERA_PROFILE <方案B>` 后仅重启即完成整套切换：全程未编辑任何配置文件，方案库所有文件内容与切换前逐字节一致，物化产物差异 = 两方案声明差异。
- [ ] 指针指向不存在的方案时启动器 fail-fast 并列出可用方案清单；`materialize_system_config.py --list` 展示方案库正确。
- [ ] 编辑方案文件中 `links.repeater.adk.network.local_ip`（模拟换网卡）并递增 `config_version` 后仅重启一次生效，无需手改其他文件。
- [ ] `links.multimode.enabled: false` 等价于 `MULTIMODE_ENABLE=false`；`actor_terminal_id` 联动校验生效。
- [ ] `models.asr.provider` 在 dashscope/local/transcriptions 之间单文件切换，`evals/text_accuracy.py` 回归通过。
- [ ] `mcp.executor_mode` 在 gateway_terminal/print_stub/http_forward 间切换行为正确；`resource_domain` 在未激活时保持显式 NotImplementedError。
- [ ] token/端口/联动非法组合在**启动前**被校验器拦截，错误信息含 YAML 路径。
- [ ] 物化产物头部携带 digest；运行日志输出 `config_digest`，事后可按 digest 追溯当时配置。
- [ ] 凭据在 system.yaml 与物化产物中均为 `${ENV:...}` 引用或展开后仅存在于进程环境，不在普通文件留明文。
- [ ] 方案文件**无** `composition` 段时，默认组合（全模块启用+标准接线）行为与 v1.1 阶段②完全一致，存量方案文件零迁移。
- [ ] 非法接线被拒：`multimode_access.voice_events → mcp_service.tools` 等反例在校验期 fail-fast，错误信息含契约类型差异说明与两端口的可接清单。
- [ ] 禁用 `repeater_gateway` 后：进程不启动、`gateway.ini`/`env.gateway` 不物化、`multimode.actor_terminal_id` 被联动校验拒绝（组合 2 场景）；健康探测只检查启用的模块。
- [ ] `materialize_system_config.py --graph` 输出的模块图与 composition 声明一致，含启动顺序标注。
- [ ] 不同方案启用不同目录组合：robotics 目录启用后其能力可调用；未启用方案中调用 `robotics:*` 被拒，且引用它的流程在装载期失败。
- [ ] 校验切换：同一能力在场景 overlay 收紧后，超限参数在结构/语义校验被拒（含可读规则说明）；overlay 放宽尝试被注册期拒绝。
- [ ] 外部工作流注入：提交→回调/轮询→七态跟踪→取消成功；无最终状态证据时停在"结果待核实"，不播报成功。
- [ ] 机器狗场景：越界 waypoint 与未登记 avoid_zone 被静态校验拒绝；排他资源并发第二任务被拒；危险区动作触发审批；超时走补偿+转人工。
- [ ] 冲突治理：重名目录、资源域双归属、wire 越权、overlay 互斥、流程引用缺失、语义重复、终端 match 重叠、设备双归属在注册/装载期全部被拒，报告含双方全名@版本与修复建议；语义重复的报错包含复用/alias 建议。

---

## 7. 积木式模块组合层（composition）

> 本章回答：能不能像搭积木一样自由选择用哪些模块、不用哪些模块，并且"谁能接谁、谁不能接谁"由系统强制？——能。核心是把"模块"与"接线"从代码事实提升为**类型化的积木目录**，方案文件只做"选积木 + 插接线"，不合法的插法在校验期就被拒绝。

### 7.1 积木语义与两类资产

**积木目录（`config/module_catalog.yaml`，静态、随代码版本发布）**：定义平台有哪些积木、每块积木有哪些**类型化端口**（入口/出口）、端口之间哪些能接、数量约束与启动顺序。目录是平台契约的一部分——**方案文件不能发明新积木，只能从目录里选**（防止配置层伪造不存在的组件，对齐 HTML 3.2 "模型不感知厂商私有结构"的确定性原则）。

**组合声明（方案文件内 `composition` 段，每方案独立）**：声明该方案启用哪些积木、积木之间怎么接线。两份资产的关系 = 电路板（目录：可用元件与插口规则）vs 电路图（方案：实际选用与连线）。

```text
┌───────────────────────┐      选择/接线      ┌───────────────────────────┐
│ 积木目录（随代码发布）      │ ◄────────────── │ 方案文件 composition 段      │
│ 模块=积木 + 类型化端口      │                  │ modules: 选哪些积木           │
│ 契约=能接/不能接的规则      │    校验期强制      │ connections: 怎么接           │
└───────────────────────┘ ◄── 不合法即拒绝 ── └───────────────────────────┘
```

### 7.2 契约类型与承接关系矩阵（当前代码事实的映射）

积木间的每根线都是一种**契约**（contract），对应今天真实存在的进程间接口。目录对每种契约声明生产者积木（出口）与消费者积木（入口）及数量约束：

| 契约 | 语义（现有代码落点） | 生产者（出口） | 消费者（入口） | 数量约束 |
|---|---|---|---|---|
| `voice_event` | 接入链路上行语音事件（`AGENT_VOICE_EVENT_PATH`） | `repeater_gateway.voice_events`、`multimode_access.voice_events`、（未来接入链路） | `agent_server.voice_ingress` | 消费端恰 1；生产端 0..n（多链路 fan-in） |
| `tool_invocation` | 智能体→工具服务调用（JSON-RPC/HTTP） | `agent_server.tool_calls` | `mcp_service.tools` | 双方启用时 1:1 |
| `device_command` | 设备控制指令下发（`/adk/protobuf/result-sms` 等网关接口） | `mcp_service.executor`（仅 `gateway_terminal` 模式） | `repeater_gateway.device_commands` | 消费端仅 repeater；生产端 0..1 |
| `result_push_sms` | 结果回传（私信/结果短信推送） | `agent_server.result_sms_out` | `repeater_gateway.result_sms` | 1:1，随 `voice_event` 边自动派生 |
| `result_poll_http` | 结果轮询（`AGENT_RESULTS_PATH`） | `agent_server.results_latest` | `multimode_access.result_poll` | 1:1，随 `voice_event` 边自动派生 |
| `model_asr` | ASR 能力供给 | `asr_dashscope` / `asr_local` / `asr_transcriptions` | `agent_server.asr` | 恰选 1 |
| `model_planner` | 规划模型供给 | `planner_dashscope` / `planner_local` | `agent_server.planner` | 恰选 1 |
| `third_party_resource` | 厂商资源域能力供给 | `third_party.adapters.<vendor>` | `mcp_service.resource_domains`（仅 `resource_domain` 模式） | 0..n |

**回程边自动派生**：目录中每块接入积木在提供 `voice_event` 出口的同时声明其回程能力（repeater 走 `result_push_sms`，multimode 走 `result_poll_http`）。因此方案文件只需声明正向边（谁把语音给 agent），回传边由组合校验器自动补齐——"结果怎么回去"不会配错。

**不能接的组合（目录类型系统天然拒绝，配了就报错）**：

| 非法接线示例 | 拒绝原因 |
|---|---|
| `multimode_access.voice_events → mcp_service.tools` | 契约不匹配：`voice_event` 无消费端口；MCP 只接受 `tool_invocation`。语音必须先过 agent |
| `mcp_service.executor → multimode_access.result_poll` | `device_command` 仅 repeater 可消费；多模终端无底层通信 |
| `repeater_gateway.voice_events → multimode_access.*` | 接入积木之间互不连接；fan-in 只指向 `agent_server.voice_ingress` |
| `asr_transcriptions.asr → mcp_service.*` | 模型供给只服务 `agent_server`；MCP 不直接调模型 |
| `mcp_service.tools → agent_server.tool_calls`（方向反接） | 契约方向校验：`tool_invocation` 生产者只能是 agent |

**积木关键性**：`agent_server`、`mcp_service` 为 `core`（启用即成对，二者构成主链路）；其余（repeater、multimode、各模型提供方、third_party、event_journal、observability）为 `optional`，可自由禁用。

### 7.3 积木目录示例（`config/module_catalog.yaml`）

```yaml
# 静态积木目录：随代码版本发布，方案文件只能从中选择，不能在此之外发明模块。
catalog_version: 1

contracts:
  voice_event:        { consumers: [agent_server.voice_ingress], multiplicity: fan_in }
  tool_invocation:     { pairs: [[agent_server.tool_calls, mcp_service.tools]] }
  device_command:     { consumers: [repeater_gateway.device_commands] }
  result_push_sms:    { consumers: [repeater_gateway.result_sms], derived_from: voice_event }
  result_poll_http:   { consumers: [multimode_access.result_poll], derived_from: voice_event }
  model_asr:          { consumers: [agent_server.asr], choose: 1 }
  model_planner:      { consumers: [agent_server.planner], choose: 1 }
  third_party_resource: { consumers: [mcp_service.resource_domains] }

modules:
  repeater_gateway:                 # 对应 adk_gateway_x86 进程 + terminal_access/repeater
    kind: process
    criticality: optional
    start_phase: 30                 # MCP(10) → Agent(20) → 接入层(30)
    ports:
      voice_events:     { contract: voice_event, role: producer }
      device_commands:  { contract: device_command, role: consumer, mandatory: false }
      result_sms:        { contract: result_push_sms, role: consumer, mandatory: false }
    params_section: links.repeater   # 该积木的参数在方案文件中的位置
  multimode_access:                # 对应 terminal_access/multimode 进程
    kind: process
    criticality: optional
    start_phase: 30
    ports:
      voice_events:  { contract: voice_event, role: producer }
      result_poll:   { contract: result_poll_http, role: consumer, mandatory: true }
    params_section: links.multimode
  agent_server:                     # 对应 agent_server 进程
    kind: process
    criticality: core
    start_phase: 20
    ports:
      voice_ingress:    { contract: voice_event, role: consumer, mandatory: true }
      tool_calls:       { contract: tool_invocation, role: producer, mandatory: true }
      result_sms_out:    { contract: result_push_sms, role: producer }
      results_latest:   { contract: result_poll_http, role: producer }
      asr:              { contract: model_asr, role: consumer, mandatory: true }
      planner:          { contract: model_planner, role: consumer, mandatory: true }
    params_section: agent
  mcp_service:                      # 对应 mcp_service 进程
    kind: process
    criticality: core
    start_phase: 10
    ports:
      tools:            { contract: tool_invocation, role: consumer, mandatory: true }
      executor:        { contract: device_command, role: producer, conditional: "params.executor_mode == gateway_terminal" }
      resource_domains: { contract: third_party_resource, role: consumer, conditional: "params.executor_mode == resource_domain" }
    params_section: mcp
  asr_dashscope:      { kind: provider, ports: { asr: { contract: model_asr, role: producer } }, params_section: models.asr.dashscope }
  asr_local:          { kind: provider, ports: { asr: { contract: model_asr, role: producer } }, params_section: models.asr.local }
  asr_transcriptions: { kind: provider, ports: { asr: { contract: model_asr, role: producer } }, params_section: models.asr.transcriptions }
  planner_dashscope:  { kind: provider, ports: { planner: { contract: model_planner, role: producer } }, params_section: models.planner }
  planner_local:      { kind: provider, ports: { planner: { contract: model_planner, role: producer } }, params_section: models.planner }
  third_party_router:               # 对应 third_party_adapters 注册表
    kind: plugin
    criticality: optional
    ports: { resource: { contract: third_party_resource, role: producer } }
    params_section: third_party
```

要点：

- `conditional` 表达"端口仅在特定参数下才存在"（如 mcp 只有 `executor_mode=gateway_terminal` 才有 device_command 出口）——接线错误与参数错误在同一处校验；
- `derived_from` 声明回程边自动派生；
- `start_phase` 是拓扑序的静态底线，校验器再叠加依赖边做环检测。

### 7.4 组合声明示例（方案文件 `composition` 段）

**组合 1：全量实验室（今天的生产形态 = 缺省组合）**

```yaml
composition:
  modules:
    repeater_gateway:   { enabled: true }
    multimode_access:   { enabled: true }
    agent_server:       { enabled: true }
    mcp_service:        { enabled: true }
    asr_transcriptions: { enabled: true }
    asr_dashscope:      { enabled: false }
    asr_local:          { enabled: false }
    planner_dashscope:  { enabled: true }
    planner_local:      { enabled: false }
    third_party_router: { enabled: false }
  connections:
    - { from: repeater_gateway.voice_events, to: agent_server.voice_ingress }   # 自动派生 result_push_sms 回程
    - { from: multimode_access.voice_events,  to: agent_server.voice_ingress }  # 自动派生 result_poll_http 回程
    - { from: agent_server.tool_calls,        to: mcp_service.tools }
    - { from: mcp_service.executor,            to: repeater_gateway.device_commands }  # mcp.executor_mode=gateway_terminal 时才合法
    - { from: asr_transcriptions.asr,         to: agent_server.asr }
    - { from: planner_dashscope.planner,      to: agent_server.planner }
```

**组合 2：纯多模查询（无中转台、无设备下发）**

```yaml
composition:
  modules:
    repeater_gateway:   { enabled: false }     # 没有中转台/网卡
    multimode_access:   { enabled: true }
    agent_server:       { enabled: true }
    mcp_service:        { enabled: true, params: { executor_mode: print_stub } }   # 或 http_forward
    asr_transcriptions: { enabled: true }
    planner_dashscope:  { enabled: true }
  connections:
    - { from: multimode_access.voice_events, to: agent_server.voice_ingress }
    - { from: agent_server.tool_calls,       to: mcp_service.tools }
    - { from: asr_transcriptions.asr,        to: agent_server.asr }
    - { from: planner_dashscope.planner,     to: agent_server.planner }
    # 注意：mcp_service.executor→repeater 边不存在（repeater 未启用）；
    # 此组合下 links.multimode.actor_terminal_id 必须为 null（联动校验规则 6）。
```

**组合 3：评估态（无任何终端，eval runner 直连 agent+mcp）**

```yaml
composition:
  modules:
    repeater_gateway:  { enabled: false }
    multimode_access:  { enabled: false }
    agent_server:      { enabled: true }
    mcp_service:       { enabled: true }
    asr_dashscope:     { enabled: true }
    planner_dashscope: { enabled: true }
  connections:
    - { from: agent_server.tool_calls,  to: mcp_service.tools }
    - { from: asr_dashscope.asr,        to: agent_server.asr }
    - { from: planner_dashscope.planner, to: agent_server.planner }
```

**组合 4：第三方资源域（+厂商积木）**

```yaml
composition:
  modules:
    repeater_gateway:   { enabled: true }
    agent_server:       { enabled: true }
    mcp_service:        { enabled: true, params: { executor_mode: resource_domain } }
    third_party_router: { enabled: true }        # 厂商适配器作为插件积木接入
    asr_transcriptions: { enabled: true }
    planner_dashscope:  { enabled: true }
  connections:
    - { from: repeater_gateway.voice_events, to: agent_server.voice_ingress }
    - { from: agent_server.tool_calls,        to: mcp_service.tools }
    - { from: third_party_router.resource,     to: mcp_service.resource_domains }
    - { from: asr_transcriptions.asr,         to: agent_server.asr }
    - { from: planner_dashscope.planner,       to: agent_server.planner }
```

### 7.5 组合校验与图运算（`shared_runtime/composition.py`）

校验器按顺序执行，任何一步失败即 fail-fast：

1. **目录闭合**：modules 的键都在目录中（否则报"未知积木"+可用积木清单）；
2. **端口/方向/契约匹配**：逐条 connection 检查两端存在、契约相同、方向 producer→consumer、条件端口生效（`conditional` 求值）；不合法时报错格式：`非法接线：<from> → <to>：契约 <A> ≠ <B>；<from> 可接：<清单>；<to> 可被接：<清单>`——**"能不能接"直接体现在错误信息里**；
3. **数量约束**：`choose: 1` 的契约（model_asr/model_planner）恰好一条边；fan-in 契约消费端唯一；
4. **依赖闭包**：connection 两端都已启用；启用模块的 `mandatory` 端口都有入边；被禁用模块的参数段（`params_section` 指向的段）不得存在（防僵尸配置）；
5. **联动校验桥接**：组合事实与既有规则互检——组合 2 中 `actor_terminal_id != null` 会被规则 6 拒绝（repeater 未启用）；`third_party_router` 启用而 `executor_mode != resource_domain` 被规则 9 拒绝；
6. **拓扑排序**：按 `start_phase` + 依赖边求启动顺序并检测环，输出给启动器；同时补齐 `derived_from` 回程边。

**图可视化**：`materialize_system_config.py --graph [--mermaid]` 输出当前组合的模块图（含禁用积木灰显、边上契约标注、启动序号），供评审与排障——"先后和承接关系"一眼可见。

**组合结果可观测性**（让"功能与组合结果"在代码与运行面上直接可见）：

- **目录结构即文档**：`capability_catalogs/<ns>/capabilities/*.yaml` 一文件一能力、文件名即功能清单；`flows/<ns>/` 按命名空间即流程清单——不读代码也能枚举当前系统的全部能力与流程；
- **启动摘要**：物化完成后日志输出一段组合摘要（profile_id、composition_digest、启用模块/目录/流程计数与清单），产物头部同步携带；
- **运行面暴露**：agent_server 既有 `/observability/metrics`（仅 localhost）扩展一项 `/observability/config` 快照：当前生效 profile、启用目录与能力视图计数、启用流程、设备/终端覆盖条目——运维与排障一眼看到"现在这套是怎么搭的"；
- **调用级标识**：每次能力调用日志与 trace span 带能力全名与来源目录 ns——冲突或排障时可直接定位到积木。

**缺省组合**：方案文件没有 `composition` 段时，加载器使用目录声明的 `default_composition`（= 组合 1，今天的生产形态）。因此 v1.1 存量方案文件**零迁移**即继续工作；需要自由组搭时再加该段。

### 7.6 组合 → 启动与物化的联动

- **启动编排**：启动器只拉起 `enabled=true` 且 `kind=process` 的积木，按拓扑序（今天等价于 MCP→Agent→Gateway→Multimode）；健康探测只覆盖启用模块；
- **物化裁剪**：物化器只为启用模块产出对应 env/ini 段（组合 2 不生成 `gateway.ini`/`env.gateway`）；
- **接线派生参数**：模块间的 URL 类参数从图中节点自动派生，不再人工同步——如 `links.multimode.agent_base_url` ← `agent_server` 的 host/port；`agent.gateway_base_url` ←（存在 device_command 边时）`repeater_gateway` 的 host/port；消除"改了一处忘了另一处"的端口漂移；
- **审计**：物化产物头部除 digest 外记录 `composition_digest`（启用模块集+边集的哈希），日志同步输出，事后可追溯"当时是怎么搭的"。

### 7.7 新积木的接入流程

新增一块积木（新接入链路、新模型提供方、新插件）= 三步，全部发生在代码侧，配置侧只做选择：

1. **实现契约面**：接入链路实现 `voice_event` 上行 + 回程能力（参考 `AgentVoiceIngress`/`ResultRouter`）；模型提供方实现 ASR/Planner 客户端契约；插件实现 `third_party_adapters/base.py`；
2. **登记进目录**：`module_catalog.yaml` 加模块条目（端口、契约、start_phase、params_section），随代码版本发布，走代码评审；
3. **参数映射**：`system_mapping.yaml` 登记新模块参数段的物化目标；方案文件即可 `enabled: true` + 接线。

目录本身版本化（`catalog_version`）：目录变更需通过契约回归（旧组合在新目录下必须继续合法——目录只增不改，废弃积木标记 `deprecated` 而非删除），保证"积木库演进但不拆已搭好的电路"。

---

## 8. 能力目录 / 场景包 / 流程积木 / 冲突治理（内容积木）

> 本章回答四个问题：①统一能力层能否按行业/场景切换不同工具目录与校验规则？②外部工作流（各种协议、各种 BPM/外部系统流程）如何作为积木注入？③机器狗这类复杂设备（路径规划、长任务、排他控制）如何落地？④积木之间（目录与目录、包与包、流程与流程、能力与资源）如何保证不冲突？

### 8.1 设计原则

1. **能力视图 = 合成 + 收紧**：目录叠加产生全局能力视图，任何行业/场景差异只做"收紧"（少看见、少允许、更严校验），不做"放宽"——与 terminals/devices 覆盖同一条铁律，扩展到底。
2. **校验三段式**：结构校验（目录自带的参数 schema）→ 语义校验（overlay 的范围/枚举/上下文限制）→ 执行前校验（policy 权限、排他资源、联锁、审批）。不同行业切换的实质是"换目录 + 换 overlay"，三段位置固定、内容可切换。
3. **平台不做物理算法**：路径规划是厂商供给能力（`robot.path.plan`），平台只负责参数边界校验、排他门控、任务跟踪与证据留存——对齐既有决策"物理不确定性由现场系统承担，智能体不揽责"。
4. **保留域不可注入**：审批、权限、审计、统一任务状态语义只能由平台核心提供；目录/流程/厂商包不得定义或覆盖这些域。
5. **一切内容积木全局全名注册**：见 8.2，标识即冲突检测的根基。
6. **复用优先、防重复开发**：新能力先查目录库与合成视图；语义重复必须显式 `alias_of`（基能力 + 参数映射），不得新建；设备类提供能力组模板，厂商包只做映射差异（见 8.3）。
7. **依赖单向**：核心编排只消费全名引用；目录与目录之间禁止横向 import，只允许 `requires` / `conflicts_with` 声明——保证内容积木可独立装卸。

### 8.2 全局标识体系（不冲突的根基）

所有内容积木用统一全名：

```text
<ns>:<type>/<name>@<semver>

ns    命名空间：core | robotics | chem-safety | <industry> | vendor:<vendor> | site:<site> | tenant:<tenant>
type  积木类型：catalog | capability | flow | overlay | device_class | event_type
name  名称（目录内 kebab-case）
semver 语义版本

示例：robotics:capability/robot.dog.patrol@2.1.0
     chem-safety:overlay/gas-alarm.readonly@1.0.0
     vendor:vendor-x:catalog/vendor-x-dog@0.3.0
```

规则：

- `core` 为平台保留命名空间（现有 mcp_service/tools 规范化后归入）；其余 ns 由注册中心登记，先到先得、全局唯一；
- 所有引用（composition、flow 步骤、overlay 目标、绑定）**只写全名**，禁止裸名——裸名即校验失败；
- wire 数字 ToolId 仅允许 `core` 目录占用（冻结编号不受目录切换影响）；`resource_domain`（如 robot、gas-alarm）全局唯一归属一个目录——两个目录声明同一资源域在注册期即冲突。

### 8.3 能力目录积木（capability catalog）

目录 = 一个可发布的能力集合单元：能力定义 + 工具绑定 + 校验 overlay + 设备类 + 能力事件 + 回归样本。一行业（或一厂商、一场景）一目录，可独立版本化、审核、灰度。

```text
capability_catalogs/<id>/
  manifest.yaml          # 目录身份、requires 依赖、conflicts_with 互斥声明
  capabilities/*.yaml    # 能力定义（目录的主要内容）
  overlays/*.yaml        # 语义校验/策略/可见性 overlay（只收紧）
  device_classes/*.yaml  # 设备类（如 robot.dog）→ 能力组映射
  events/*.yaml          # 能力事件（进度/告警/完成）定义
  evals/regression.jsonl # 目录回归样本（发布门禁）
```

方案文件 composition 段选择启用哪些目录：

```yaml
composition:
  modules: { ... }                    # 进程积木（第 7 章）
  catalogs:                           # 能力目录积木：合成后的能力视图 = ∩启用目录
    - core                            # 永远隐含启用（core 不可禁用）
    - robotics                        # 机器人/机器狗行业目录
    # - chem-safety                  # 未启用 → 其能力不可见不可调
  flows:                              # 流程积木（见 8.5）
    - robotics:flow/patrol-rounds@1.2.0
  connections: [ ... ]                # 进程接线（第 7 章）
```

能力定义示例（机器狗巡检，见 8.6）：

```yaml
# capability_catalogs/robotics/capabilities/robot.dog.patrol.yaml
full_name: robotics:capability/robot.dog.patrol@2.1.0
category: control                       # query | control | flow | notify
side_effect: irreversible_scheduling     # 无副作用 | 可逆 | 不可逆调度
risk: 3                                  # 风险等级（决定审批门控）
executor: resource_domain:robot          # 走第三方适配层（device_command 或 resource_domain）
resource_domain: robot
params_schema:                           # 结构校验（JSON Schema）
  type: object
  required: [device_id, path_plan_id]
  properties:
    device_id: { type: string }
    path_plan_id: { type: string }
    rounds: { type: integer, minimum: 1, maximum: 10 }
preconditions:                           # 执行前校验（确定性规则）
  - device_online: { param: device_id }
  - battery_pct_gte: { param: device_id, minimum: 40 }
  - no_active_task_on: { resource: "robot:${device_id}" }
exclusive_resources: [ "robot:${device_id}" ]   # 排他资源（运行期门控）
idempotency: { key: [device_id, path_plan_id], window_seconds: 300 }
receipt:                                 # 回执声明（对齐 HTML 5.4 能力清单）
  accepted: [submitted, queued]
  progress: { event: robotics:event/robot.task.progress }
  final: [succeeded, failed, cancelled]
  unknown_recovery: [poll_status, manual_verify]   # 结果未知 → 对账/人工
cancellation: { supported: true, semantics: best_effort }
timeouts: { accept_seconds: 10, execute_seconds: 7200, progress_stale_seconds: 300 }
compensation: { on_timeout: [robot.task.cancel, escalate_to_human] }
capability_events: [robotics:event/robot.task.progress@1.0.0]
```

**合成与裁剪**：`catalog_registry` 装载启用目录 → 合成全局能力视图；模型可见列表 = 合成视图 − devices/terminals 裁剪 − overlay 隐藏项。执行时按能力全名路由（`executor: resource_domain:robot` → `third_party_router`）。

**目录间关系**：`requires`（依赖某目录 ≥ 某版本）；`conflicts_with`（互斥，如两个目录声明同一资源域——注册期已拒，此为显式业务互斥，如"防爆区目录与普通巡检目录互斥"）。

**能力复用与防重复开发三机制**：

1. **注册期语义重复检测**：能力入库时计算归一签名（`resource_domain` + `category` + `params_schema` 归一化哈希）；签名重复且未声明 `alias_of` → 注册期拒绝，错误提示"与 `<已有全名>` 语义重复，请复用或显式 alias"——从源头阻断两家厂商/两个行业各写一遍同类能力；
2. **device_class 模板继承**：设备类声明能力组模板（robot.dog → status/plan/patrol/cancel...）；厂商目录不重复定义这些能力，只登记"设备型号 → 基能力 + 字段/单位/错误码映射差异"——一家新厂商接入机器人行业时，能力面是继承的，开发量收敛到映射与联调；
3. **显式 alias**：确实需要变体（如某厂商 patrol 参数集不同）时，声明 `alias_of: <基能力全名>` + 参数映射表，注册期校验映射与基能力 schema 兼容——变体不产生新语义，回归样本复用基能力的。

复用检查纳入开发流程 S1（登记与声明）：提交新能力前必须在 registry 查重并在登记表中填写"复用检查结论"（复用/alias/新建理由）。

### 8.4 场景 Overlay（校验/策略/可见性的收紧面）

不同行业或场景"能力一样但限制不同"时，不复制目录，而是加 overlay：

```yaml
# capability_catalogs/robotics/overlays/night-patrol.yaml
full_name: robotics:overlay/night-patrol@1.0.0
target: robotics:capability/robot.dog.patrol@>=2.0
validation:                              # 语义校验收紧
  params_schema_add:                     # 只能收紧：范围更窄/枚举更少
    properties:
      rounds: { maximum: 3 }             # 夜间最多 3 圈（基线 10）
policy:                                  # 策略收紧
  requires_approval: true                 # 夜巡需审批（基线免审批）
  allowed_time_window: ["22:00", "06:00"]
visibility:
  hide: []                                # 隐藏能力（可选）
```

叠加规则（与 terminals/devices 覆盖同一套机制）：区间取交、max_risk 取小、allowlist 取交、审批要求只增不减、time_window 取交。**overlay 相对目录基线放宽 → 注册期拒绝**；两个启用 overlay 对同一目标给出不同值且非严格收紧关系 → 拒绝，要求收敛或显式 precedence。

**校验切换完整链示例**：白班方案启用 `core+robotics`（巡检最大 10 圈、免审批）；夜班方案 composition.flows 不变，catalogs 追加 `night-patrol` overlay（夜间场景包）→ 同一能力参数上限变 3、需审批。**切换目录/overlay = 方案级切换（指针），业务零代码改动。**

### 8.5 流程积木（外部工作流注入）

外部工作流分两种形态注入，均为"积木"，均可校验、可互斥、可版本化：

**形态 A：平台内流程积木（flow）**——声明式步骤 DAG，由平台确定性引擎执行（复用 orchestration 命令生命周期，非 LangGraph）；LLM 只做意图→flow 映射与参数填充，不编排步骤。

```yaml
# flows/robotics/patrol-rounds.yaml
full_name: robotics:flow/patrol-rounds@1.2.0
match:                                   # 意图映射（装载期查唯一性）
  intents: ["巡检", "开始巡检", "例行巡检"]
  entities: { site: optional, rounds: optional, default: 1 }
steps:                                   # 引用能力全名，禁止裸名
  - id: check-status
    capability: robotics:capability/robot.status.query
    on_failure: abort_with_feedback
  - id: plan-path
    capability: robotics:capability/robot.path.plan
    params: { area: "${site:-default-site}", constraints: day-safe }
  - id: approve-gate
    gate: approval                        # 保留域门控（非能力，平台内置）
    policy: { when: risk_gte(3), timeout_seconds: 300 }
  - id: run-patrol
    capability: robotics:capability/robot.dog.patrol
    params: { device_id: "${device}", path_plan_id: "${steps.plan-path.path_plan_id}" }
  - id: notify
    capability: core:capability/notification.send
lifecycle:                               # 与 M4 命令生命周期对齐
  timeout_seconds: 7200
  compensation: [robot.task.cancel]
  escalation: { on_progress_stale: notify_supervisor }
  checkpoint: per_step                   # 断点续跑粒度
```

**形态 B：外部流程能力（外部 BPM/工单/厂商编排系统黑盒）**——外部系统内部的复杂流程对平台是"一个能力"：目录里登记为 `category: flow` 的能力，绑定协议（REST/MQTT/AsyncAPI），平台按统一契约只看见 submit/query/cancel 三个面 + 七态跟踪，内部步骤不进平台。适用于：企业工单系统、厂商自带编排平台、已有 BPM。**约束**：必须声明回执/幂等/取消/对账能力，缺最终状态证据的能力只能停在"结果待核实"。

**协议适配**：两种形态的协议差异（REST/SDK/MQTT/WebSocket/AsyncAPI 事件流）全部收敛在 `vendor_packages/<vendor>/` 的 adapter 声明中，能力定义引用 adapter，平台不感知协议——对齐 HTML 4.1 "不存在要求所有厂商统一物理协议的假设"。

**意图映射冲突**：多个 flow 的 match 重叠 → 装载期拒绝；确实需要多候选时 flow 显式声明 `candidates_policy: ask_user | precedence`，禁止静默随机选。

### 8.6 机器狗场景设计（路径规划与复杂设备）

能力分解（robotics 目录 robot.dog 设备类）：

| 能力全名 | category | 说明 |
|---|---|---|
| `robotics:capability/robot.status.query` | query | 电量/位置/在线/当前任务 |
| `robotics:capability/robot.path.plan` | query | **路径规划（厂商供给）**：输入区域/途经点/约束，输出 path_plan_id + 安全自检证据 |
| `robotics:capability/robot.dog.patrol` | control | 按规划执行巡检（排他资源 + 长任务跟踪） |
| `robotics:capability/robot.task.cancel` | control | 取消当前任务（best_effort，需厂商回执确认） |
| `robotics:capability/robot.task.pause` | control | 暂停（型号支持才注册） |

**路径规划的职责边界**（关键决策）：平台不做规划算法——`robot.path.plan` 是厂商/规划服务供给的能力，平台只做：

1. **静态边界校验**：waypoints 越出站点地图边界、落入未登记 `avoid_zone`、坡度/速度超设备类限值 → 结构/语义校验拒绝（含可读规则名）；
2. **排他资源门控**：`robot:${device_id}` 排他，同一机器狗同时仅一个活动任务，并发第二任务在发送前被拦（策略可配 reject|queue）；
3. **长任务跟踪**：progress 事件（ETA/位置）超过 `progress_stale_seconds` 视为失联 → 超时升级链（通知监护人→暂停尝试→人工接管）；
4. **证据链**：plan 的安全自检证据、每步回执、最终状态全部入任务记录（对齐 M4/M5）。

**设备类与实例**：`device_classes/robot.dog.yaml` 声明设备类通用能力组与硬约束（速度上限、坡度、防护等级）；具体设备实例在 `devices.entries` 绑定厂商 adapter + 型号 overlay（某型号不支持 `robot.task.pause` → 注册期查 capability 存在性、装载期裁剪可见性）。

**设备接入端到端路径**（与第三方接入六步流程 S0-S5 对齐，每步有固定产物）：

| 步骤 | 动作 | 产物/校验 |
|---|---|---|
| ① 选型登记 | 按设备类登记实例（`devices.entries`：device_id、device_class、vendor adapter、站点归属） | 注册期：设备实例唯一归属（双站点绑定拒绝）；能力存在性核对（模板继承面） |
| ② 映射差异 | 厂商包登记字段/单位/状态/错误码映射 + 型号 overlay | 映射与基能力 schema 兼容校验；alias 显式 |
| ③ 离线验证（S2） | mock 回放、故障夹具回归 | 越权写入/重复副作用/无证据播报三项为零 |
| ④ 沙箱联调（S4） | 真实沙箱核回执、跑通首条闭环 | 行为证据录为夹具；回执/取消/幂等实测记录 |
| ⑤ 启用 | 方案 `devices.entries` 覆盖（能力白名单裁剪）+ 指针切换重启 | 装载期：排他/裁剪/终端授权负例 |
| ⑥ 运行与退役 | 七态跟踪、审计；退役 = 方案移除 + registry 标记 `deprecated` | 历史记录引用的目录/映射版本可追溯（不因退役被重写） |

**完整场景推演**（语音入口）："让狗子去一号罐区巡两圈" → 意图匹配 `patrol-rounds` flow → `robot.status.query`（电量 65% ≥ 40%）→ `robot.path.plan`（area=TK-01 罐区别名经 ontology 解析，无禁区穿越，生成 path_plan_id）→ approve-gate（risk=3 需审批，对讲确认）→ `robot.dog.patrol`（排他门控通过，任务先落库）→ progress 事件播报（"已到 2 号点，预计 8 分钟"）→ succeeded 回执 → 终端播报。任一环节失败均有补偿路径与人工升级。

### 8.7 冲突治理体系（三层检测 + 保留域）

| 检测层 | 时机 | 冲突类别（十三类） |
|---|---|---|
| **注册期**（registry，目录/流程/包入库时） | 发布入库 | ① 命名冲突（同 ns/type/name 重名）② 资源域双归属 ③ wire 越权（非 core 目录声明数字 ToolId）④ 依赖不满足（requires semver 无解）⑤ 保留域越权（目录/flow/包定义审批、权限、审计、状态语义）⑪ 语义重复（归一签名重复且未声明 alias_of，见 8.3）⑫ 设备实例双归属（同一 device_id 绑定多站点/多作用域） |
| **装载期**（composition 校验时） | 方案物化前 | ⑥ 目录互斥（conflicts_with 双启用）⑦ overlay 冲突（同目标非收紧关系不同值）⑧ 流程引用缺失（flow 引用了未启用目录的能力）⑨ 能力/执行器模式不匹配（resource_domain 能力但 mcp.executor_mode ≠ resource_domain）⑬ 终端 match 重叠（terminals.entries 匹配规则重叠且未声明 precedence） |
| **运行期**（门控） | 每次调用前 | ⑩ 排他资源并发、precondition 失败、终端级能力未授权（按 devices/terminals 裁剪） |

**报错格式统一**：`冲突[<类别号>]: <积木A全名> vs <积木B全名> — <原因> — 修复建议: <...>`，双方全名与版本一目了然（"标识明显"）。

**保留域清单**（只能由平台 core 提供，任何积木不可定义/覆盖）：审批策略、权限判定、统一任务状态七态、审计记录、排他门控引擎本身、凭据管理。

### 8.8 落地挂接点（现有代码的扩展位，均不破坏现有行为）

| 设计组件 | 挂接点（现有代码） | 兼容策略 |
|---|---|---|
| catalog_registry + 合成视图 | `mcp_service/tool_catalog.py::ToolCatalog`（构造时仅读 core 目录 → 扩展为读启用目录集合） | core 目录 = 现 mcp_service/tools 原位规范化，零移动零改动 |
| 三段式校验 | `mcp_service/execution.py`（现 validate_arguments 即结构校验，扩语义/执行前两段） | 未启用 overlay 时后两段为空操作 |
| 排他门控 + 七态 | `agent_server/orchestration/commands` 生命周期 + `calls/lifecycle_tracker` | 排他声明空 = 现行为 |
| flow 引擎 | `agent_server/orchestration/planning`（flow 装载为计划模板；step 执行复用 operations/commands） | 无 flow 启用时 Planner 原路径不变 |
| 意图映射 | `semantics/reasoning` 意图分类后查 flow registry | 无匹配 flow → 原工具直调路径 |
| 冲突检测 | `shared_runtime/conflicts.py`（新增） | 仅在目录/流程数 > 1 时实质生效 |
| 外部流程协议适配 | `third_party_adapters/base.py` + `bindings/` | 新 adapter 类型，不动现有 robot/drone stub |

---

## 9. 最终评审结论（v2.0）

针对"组合路径清晰性 / 功能直观性 / 冲突与重复开发 / 耦合内聚 / 演进充分性"五项评审，结论与本次补齐如下：

### 9.1 五项评审结论

| 评审问题 | 结论 | 依据与本次补齐 |
|---|---|---|
| 场景与设备组合路径是否清晰 | **通过**。场景路径 = 方案指针 → composition（modules/catalogs/flows）→ 目录/overlay → 终端/设备覆盖，四级均可枚举验证；设备路径本次补齐端到端六步表（8.6，对齐 S0-S5），每步有固定产物与校验 |
| 功能与组合结果是否直观可见 | **通过（本次补强）**。目录结构即文档 + `--graph` 模块图 + 启动摘要 + `/observability/config` 快照 + 调用级能力全名（7.6 可观测性）；不读代码即可枚举全部能力/流程与当前生效组合 |
| 积木冲突与重复开发是否解决 | **通过（本次补强）**。冲突：三层十三类检测（8.7，新增语义重复/终端 match 重叠/设备双归属）；重复开发：复用防重三机制（8.3：语义重复检测 + device_class 模板继承 + 显式 alias）+ S1 复用检查门禁 |
| 是否低耦合高内聚 | **通过**。见 9.2 耦合评估 |
| 是否足以支撑后期开发 | **通过（含已知边界）**。见 9.3 演进充分性与 9.4 后续决策点 |

### 9.2 耦合度评估

| 交界 | 耦合方式 | 评估 |
|---|---|---|
| 接入层 ↔ 核心 | 仅经 `shared_contracts` 契约（VoiceEvent/结果路由） | 单一契约面，接入积木可独立装卸 |
| 核心 ↔ 能力层 | 仅经合成视图 + 能力全名调用（tool_invocation） | 模型不感知厂商协议 |
| 能力层 ↔ 内容积木 | 仅经 catalog_registry（装载期合成，运行期只读） | 目录可独立版本化发布 |
| 内容积木 ↔ 内容积木 | 禁止横向 import；只允许 requires/conflicts_with/alias_of 声明 | 可独立装卸、冲突注册期可发现 |
| 配置 ↔ 进程 | 仅经物化产物（env/ini）与启动器环境注入 | 进程读取代码零改动 |
| 内聚度 | 每个进程积木一个进程、每个目录一个行业、每个能力一个文件、每条契约一个常量面 | 改动半径 = 单积木 |

### 9.3 演进充分性对照（新增需求 → 是否触碰核心）

| 未来需求 | 扩展路径 | 触碰核心编排？ |
|---|---|---|
| 新行业/场景 | 新增目录 + overlay + 方案 catalogs 选择 | 否 |
| 新厂商/新设备型号 | 厂商包映射差异 + device_class 继承 + alias | 否 |
| 新终端形态 | terminal_access 新接入积木 + links 段 | 否 |
| 新模型/Provider | provider 积木登记 | 否 |
| 新流程/外部工作流 | flow 积木或外部流程能力登记 | 否 |
| 新契约类型（视频/遥测流等） | module_catalog 增契约 + catalog_version 升级（只增不改） | 仅契约层，经评审 |
| 多站点/多租户运营 | 命名空间 site:/tenant: 已预留；方案文件按站点一份 | 部署形态演进，不破坏单文件机制 |

### 9.4 已知边界与后续决策点（不阻塞当前方案，留 ADR）

1. 运行时配置中心（集中下发/热更新）：与"停旧起新"决策冲突；v3.0 起**对讲机集群目标拓扑为"多节点 + 集中配置库 + 各节点本地物化 + 滚动重启"**——节点重启时重新物化，重启窗口内不同节点可能短暂处于不同方案（标记窗口），不引入运行中热加载；真正动态集中下发仍需单独 ADR；
2. 动态能力发现（厂商运行时上报能力清单）：当前为静态登记 + 联调验证，动态发现需先解决"上报≠可验证"的治理问题；
3. 视频/实时遥测等新契约类型：需 catalog_version 升级与契约回归，不适用本版接线；
4. 跨厂商补偿事务（多设备原子性）：平台只保证单任务七态与证据链，跨厂商原子回滚不做承诺（对齐 HTML 7 可靠性底线）。

---

## 10. 并发任务、对讲机集群与快速部署（v3.0 生产化）

> 本章回答三个生产化要求：①方案能否兼容支持并发任务？②能否部署到对讲机集群系统里？③项目能否快速部署到服务器上（数据库使用本地 PostgreSQL 非 Docker）？

### 10.1 并发任务支持

**现状基础**：回调链路已具备并发安全内核——x86 网关 ThreadingHTTPServer、outbox 多 worker（VOICE_CALLBACK_SENDER_WORKERS）+ 熔断 + 退避重试、agent 入站 spool 幂等落库、`http_pool.py` 连接池、执行已以独立任务号跟踪；且现有链路均为"一终端一任务"，天然多终端并发。**方案兼容性**：以上机制不受方案库/积木组合/能力目录影响——并发是进程内行为，方案切换的是参数与积木，不改并发模型。

**需补齐的并发治理**（多终端并发 + 单终端内并发 + 集群多节点并发的三层）：

| 层 | 机制 | 落点 |
|---|---|---|
| 任务存储并发安全 | `storage.backend=postgresql`：任务/命令/回执表 `UNIQUE(task_no)` + `SELECT ... FOR UPDATE` 行锁；任务号全局唯一（含 node_id 前缀，见 10.2）；SQLite 回退形态沿用单写者模式（现状行为） | `agent_server/storage/`、`calls/repository.py` |
| 排他资源门控跨请求 | 原进程内排他（第 8 章）升级为数据库行锁实现；集群形态下跨节点互斥生效 | `orchestration/commands` |
| 多进程竞争协调 | x86 网关多 worker HTTP + 独立 UDP socket 现状保持单写者；若引入多 worker 则 outbox 租约（VOICE_CALLBACK_OUTBOX_LEASE_SECONDS）已具备抢占语义，无需新机制 | `repeater/callbacks/outbox.py` |
| flow 步骤并发 | flow 声明式 DAG 允许无依赖步骤并行执行（并行度可配），依赖步骤串行 | `orchestration/planning` |
| 资源限流 | 目录能力声明的 vendor 限流（8.3）运行期以令牌桶执行 | `mcp_service/execution.py` 第三段校验 |
| 公平调度 | 多终端并发时按终端公平轮转；单终端 voice_event 与 device_command 串行（下行队列保持顺序） | 调度队列参数进方案文件 |

### 10.2 对讲机集群部署

**目标拓扑（复用既有双 venv 架构，不改进程形态）**：

```text
                    ┌─ 方案分发（发布库/手工同步 config/profiles）→ 各节点本地物化 ─┐
对讲集群(中转台×N)    ▼                                                      ▼
  ├─ 节点 A：ADK Gateway(x86) + Agent + MCP（HYTERA_NODE_ID=node-a）
  ├─ 节点 B：同构复制（HYTERA_NODE_ID=node-b）                    
  └─ 节点 N：…
共同依赖：本地 PostgreSQL（中心实例）+ 每节点配置物化产物
约束：x86 网关单节点绑定中转台（UDP 资源物理独占）→ 集群=多网关节点，每节点带自己的中转台
```

**关键设计**：

- **每节点一方案文件**：`config/profiles/<站点>-<节点id>.yaml`，方案 = 积木组合（含 links.repeater 的网卡/端口矩阵）+ `meta.node_id`；节点加入 = 发布一个新方案文件，不改代码；
- **node_id 贯穿任务体系**：任务号、排他资源键、回调关联号全部携带 node 前缀（`node-a:task:123`）——跨节点可追溯，任务号全局不冲突（规则 26）；
- **多网关下发互不互踩**：现有"一终端归属一网关"的对话绑定机制，天然保证 A 终端的下行只经其归属网关节点；跨网关的组呼下发声明 fan-out 席位（规则 27），避免组内重复播报；
- **集中配置 + 本地物化**：配置库（git/共享目录）存放方案文件，各节点拉取后**本地物化**（每节点一份 runtime/generated），`deploy.ps1` 滚动重启逐节点生效——无运行时配置共享状态，保留"停旧起新"铁律；
- **无状态化检查清单**：agent/mcp 可水平复制的前提 = 全部状态进 storage（含会话/任务/回执/提醒）；迁移器（§10.4）完成后由 provision_health 断言无本地状态残留；
- **渐进路径**：单节点 → 垂直扩容（多 worker / 连接池）→ 集群（多节点复制）；仅当单节点容量/可用性实测不足时演进，不预设微服务拆分（对齐 HTML 9.2 阶段 B/C）。

### 10.3 快速部署（本地 PostgreSQL，非 Docker）

**快速部署器 `scripts/deploy.ps1`（PowerShell，面向 Windows 服务器）**：

```text
① 环境体检（provision_health.py，只读）：Python 版本/双 venv、PostgreSQL 本机服务（非容器）连通、
   凭据变量、网卡/端口、磁盘目录 → 每项输出勾选/叉号与修复建议，任一失败即退出非零
② 组件安装（幂等）：.venv-adk32 / .venv-agent64（复用现有 setup.bat，离线包优先）、
   runtime/ADK、本地 PostgreSQL 服务（系统安装包，非 Docker）
③ 用户选择：部署形态（单节点/集群节点）、HYTERA_NODE_ID、方案文件（--list 列方案库）
④ 初始化：建库/建用户（psql）→ storage.migrations 自动迁移 → SQLite→PG 迁移（可选）→ 校验器全绿
⑤ 启动：物化 → 按拓扑序拉起 → 4 项 /health + 语音冒烟
⑥ 交付：自动生成部署报告（环境快照、方案 digest、验收结果清单）
```

**本地 PostgreSQL（非 Docker）约定**：`storage.backend=postgresql`；实例为服务器本机安装的服务（Windows 服务或本机 Linux 服务），连接参数及 `password_ref` 在方案文件声明、凭据经环境/注册表注入；迁移与回退：`migrate_sqlite_to_postgres.py` 一次性迁移（对账报告），`storage.backend=sqlite` 可随时回退到现状（阶段②前的行为不受影响）。

### 10.4 与既有机制的兼容关系（v3.0 不推翻任何 v2.0 结论）

| 变更点 | 兼容策略 |
|---|---|
| SQLite → PostgreSQL | `storage.backend` 新增段，默认 `sqlite` 保持现状零改动；`shared_runtime/sqlite_compat.py` 扩展为统一存储门面：连接串注入 DATA_SOURCE 环境变量，调用方读写代码不动 |
| 并发任务 | 全部为进程内/存储层增强，不新增积木与契约边 |
| 集群 | 每节点一份方案文件与物化产物——单文件铁律不变；node_id 只是 meta 新字段 |
| 快速部署 | deploy.ps1 为编排既有组件（setup.bat/物化器/校验器/迁移器）的粘合层，不引入容器或新中间件 |
| 验收口径 | 50 并发压测、N 节点冒烟、迁移对账、新服务器一次部署通过 |

---

## 附录 A. 目录结构变更表（全部为新增）

```text
config/
  profiles/<方案id>.yaml            # 新增：方案库，一方案一个全量单文件（编辑面）
  module_catalog.yaml               # 新增：积木目录（静态，随代码发布；见第 7 章）
capability_catalogs/<id>/           # 新增（阶段⑤）：能力目录积木（core = 现 mcp_service/tools 原位规范化）
flows/<ns>/<flow>.yaml              # 新增（阶段⑤）：流程积木（平台内确定性流程）
vendor_packages/<vendor>/           # 新增（阶段⑤）：厂商适配包（协议/字段/回执/错误码映射）
  system.yaml                       # 新增：兼容入口（无指针时使用；亦可作为方案之一）
  system_mapping.yaml              # 新增：物化映射声明表（全局共用）
  active_profile                    # 新增（可选）：指针文件，一行方案 id
  gateway.ini                       # 保留：无指针时的回退输入 / 阶段一被物化重写
shared_runtime/
  config_schema.py                 # 新增
  composition.py                   # 新增（阶段④启用）：积木组合校验与图运算
  conflicts.py                     # 新增（阶段⑤启用）：注册/装载期冲突检测（附录 B 规则 17-22）
  config_loader.py                 # 新增
  system_config.py                 # 新增（阶段三启用）
scripts/
  materialize_system_config.py     # 新增
  validate_system_config.py        # 新增
  migrate_sqlite_to_postgres.py    # 新增（阶段⑥）：SQLite→PG 一次性迁移（对账报告）
  provision_health.py              # 新增（阶段⑥）：部署体检（只读，勾选清单+退出码）
  deploy.ps1                       # 新增（阶段⑥）：快速部署器（§10.3，PowerShell）
runtime/generated/                 # 新增：物化产物（env.agent / env.gateway / gateway.ini / digest）
.env.agent / .env.gateway           # 保留：回退输入 / 阶段一被物化重写（.bak 备份）
start_dual_process.bat              # 修改：头部追加物化前置步骤（唯一被修改的既有文件）

集群补充（阶段⑥）：config/profiles/<站点>-<节点id>.yaml 每节点一份（HYTERA_NODE_ID 贯穿任务/排他/回调标识）；
storage/ 增补 PostgreSQL 迁移脚本与连接池（backend=postgresql，默认 sqlite 保持现状）。
```

## 附录 B. 校验规则清单（fail-fast）

1. `meta.profile_id` 必填且与方案文件名（`config/profiles/<id>.yaml`）一致；`config_version` 只在方案文件被编辑时单调递增，指针切换不改 `config_version`。
2. 所有 `${ENV:VAR}` 可解析；被引用变量非空。
3. `security.local_api_token_ref` 展开值非空，且物化到两个 env 文件的值一致。
4. `links.*.server.port` / `agent.callback.port` / `mcp.port` ∈ {18081,18082,18083,18084} 冻结值且互不冲突。
5. `links.repeater.identity.control_ids == local_control_id`（或按部署语义显式声明差异）。
6. `links.multimode.actor_terminal_id ∈ [1, 16777215]` 且存在于设备目录；启用时 `links.repeater.enabled=true`。
7. `mcp.executor_mode ∈ {gateway_terminal, http_forward, print_stub, resource_domain}`；`http_forward` 时 `mcp.http_forward.base_url` 非空；`resource_domain` 未激活时直接拒绝（保持现状显式禁用）。
8. 覆盖只收紧：`terminals.entries.*.overrides.policy.auto_execute_max_risk ≤ defaults.policy.auto_execute_max_risk`；`devices.entries.*.overrides.capabilities ⊆ 全量工具目录`。
9. `third_party.enabled=true` ⇒ `mcp.executor_mode=resource_domain` 且每个 adapter `resource_domains` 非空、`status=verified`。
10. `industry_profile.enabled=true` ⇒ manifest 依赖满足（`min_agent_version`），evals 回归样本文件存在。
11. 映射闭合：方案文件中每个叶子键都能在 `system_mapping.yaml` 中找到物化目标；映射表中的目标键在对应读取方代码中真实存在。
12. 指针闭合：指针（`--profile` / `HYTERA_PROFILE` / `active_profile`）指向的方案文件必须存在；方案 id 仅允许 `[a-z0-9_-]`；物化前静态扫描方案文件，禁止出现跨文件 YAML 锚点/引用。
13. （组合）目录闭合：`composition.modules` 的每个模块 id 必须存在于 `module_catalog.yaml`；方案文件不允许定义目录之外的新模块（新积木只能随代码发布进目录）。
14. （组合）接线合法性：每条 connection 两端（模块.端口）必须存在于目录；两端契约类型相同、方向为 producer→consumer；满足契约数量约束（`model_asr`/`model_planner` 恰选 1、`device_command` 消费者仅 repeater_gateway、`tool_invocation` 双方启用时 1:1）；不合法即拒绝并输出两端口的可接清单。
15. （组合）依赖闭包：所有 connection 两端模块均已启用；启用模块的 mandatory 端口均有入边（如 agent_server 必须接一个 ASR provider 与一个 planner）；禁用模块不得出现在 connections 中；禁用模块的参数段（对应 `links.<x>` 等）必须不存在。
16. （组合）顺序合法：composition 声明的启用模块及边在目录 `start_phase` 约束下存在合法拓扑序（MCP→Agent→Gateway/Access）；启动器按该序拉起，仅对启用模块做健康探测。
17. （目录）命名闭合：目录 id、能力全名全局唯一；wire 数字 ToolId 仅允许 core 目录占用；resource_domain 唯一归属（一个资源域只能被一个目录声明）。
18. （目录）依赖闭合：manifest `requires` 的 semver 范围在锁定版本下满足；厂商适配器版本 ≥ 能力声明的最低适配器版本。
19. （流程）闭合：flow 步骤引用的能力全部在启用目录中；每意图映射唯一或声明候选策略；流程有 timeout/compensation/escalation 声明且无环。
20. （排他）运行期门控：声明了 exclusive_resources 的能力，其并发策略（拒绝/排队）显式；同一资源并发第二任务在发送前被拦截。
21. （保留域）审批、权限、审计类能力不可由 catalog/flow/pack 定义或 overlay；外部工作流能力不得声明保留域语义。
22. （overlay 只收紧）validation/policy overlay 相对目录基线只允许收紧（范围更窄/上限更低/风险不升）；两个启用包 overlay 同一目标且值不同时拒绝，要求显式 precedence 或收敛。
23. （复用防重）能力归一签名（resource_domain + category + params_schema 归一哈希）重复登记必须显式 `alias_of` 且参数映射通过兼容校验，否则注册期拒绝并提示复用路径。
24. （终端匹配）terminals.entries 匹配规则重叠时必须显式声明 precedence，否则装载期拒绝（禁止静默随机匹配）。
25. （设备归属）设备实例（device_id）在同一部署内仅可绑定一个站点/作用域与一个厂商 adapter，双归属装载期拒绝。
26. （集群）`meta.node_id` 全集群唯一（同一集群内禁止重复，跨集群可复用）；任务号/排他键/回调关联号必须携带 node 前缀且格式合法；`storage.backend=postgresql` 为集群形态的强制要求（多节点共享状态禁用 sqlite）。
27. （集群）组呼/广播类下发的 fan-out 席位显式声明：同一 logical 工作组的下行席位唯一归属一个网关节点，跨节点重复播报在装载期拒绝；单播目标网关归属由终端绑定表推导，不依赖广播。
28. （存储）`storage.backend=postgresql` 时 `password_ref` 可解析且非空、连接体检通过；`backend=sqlite` 与 `meta.node_id` 集群段同时出现时拒绝（单节点形态不得声明集群字段）；迁移后对账报告差异必须为零方可切换 backend。

---

（完）本文档为设计方案，不含代码改动；实施时以阶段①影子物化为起点，逐阶段验收放行。
