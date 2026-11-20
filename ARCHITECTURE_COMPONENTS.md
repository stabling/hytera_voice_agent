# 代码架构组成与功能对照（基于统一配置系统 v3.0 整体方案）

- 状态：最终定版（描述性文档，不含代码改动）
- 版本：v3.0（对应 CONFIG_SYSTEM_DESIGN.md v3.0：v2.0 机制 + 并发任务 / 对讲机集群 / 快速部署与本地 PostgreSQL）
- 关联：`docs/CONFIG_SYSTEM_DESIGN.md`、`docs/ARCHITECTURE.md`
- 阅读方式：第 1 章全局图景 → 第 2 章积木体系总表 → 第 3~9 章分层组成明细 → 第 10 章契约边落点 → 第 11 章场景对照 → 第 12 章验证组成

---

## 1. 全局架构总览

系统 = **六个逻辑层 + 配置治理层 + 内容积木层 + 冲突治理**。v1.3 的关键扩展：统一能力层的能力面（工具目录/校验/能力视图）由**能力目录积木**合成，流程由**流程积木**供给，一切内容积木以全局限名注册并经**三层冲突检测**。

```text
┌──────────────────────── L1 业务入口层（terminal_access）────────────────────────┐
│  [进程积木 repeater_gateway] adk_gateway_x86 + terminal_access/repeater (18081,x86)  │
│  [进程积木 multimode_access]  terminal_access/multimode (18084,x64)               │
│  [冻结] terminal_access/wire —— CBOR/L2 线协议与冻结工具编号                          │
└──────────┬────────────────────────────────┬──────────────────────────────────┘
 voice_event│                 result_push_sms│ / result_poll_http（回程）
            ▼                                │
┌──────────────────────── L2 智能体核心层（agent_server, 18082, x64）───────────────┐
│  六大能力：语义/会话/编排/安全/工具调用/反馈  +  意图→流程映射（v1.3）                  │
└────┬───────────────────────┬───────────────────────┬────────────────────────┘
 tool_│invocation        model_asr/model_planner│      │flow 匹配/flow run（七态）
      ▼                        ▼                    ▼
┌── L3 统一能力层 ────────────┐ ┌─ 模型提供方积木 ─┐ ┌─ 流程执行（复用编排引擎）─┐
│ [进程积木 mcp_service]      │ │ asr_* / planner_* │ │ flow 步骤 = 子任务        │
│ catalog_registry 合成视图   │ └───────────────────┘ │ 七态 + 排他 + 补偿        │
│ 三段式校验 + 执行器路由     │                        └──────────────────────────┘
└────┬───────────┬──────────┘
     │(resource_domain)│(device_command)
     ▼               ▼
┌── L3.5 内容积木层（capability_catalogs / flows / vendor_packages）──────────────────┐
│ [内容积木] core / robotics / chem-safety / vendor:* 目录；overlay；flow 定义          │
│ [治理] registry 全局全名注册 + conflicts.py 三层冲突检测（注册/装载/运行）             │
└────┬──────────────────────────────────────────────────────────────────────┘
     ▼ third_party_resource
┌── L4 第三方适配层 ──────────────────────────────────────────────────────────────┐
│ [插件积木 third_party_router] third_party_adapters/ + vendor_packages 协议适配         │
└────┬───────────────────────────────────────────────────────────────────────────┘
     ▼
┌── L5 外部系统与设备层 ───────────────────────────────────────────────────────────┐
│ 中转台/窄带终端｜对讲平台｜机器狗/机器人/无人机/IoT/企业工单/外部 BPM（未来接入）       │
└──────────────────────────────────────────────────────────────────────────────────┘

横向贯穿：L0 shared_contracts/ + shared_runtime/（含 v1.3 config/composition/conflicts）
配置治理层：config/profiles + module_catalog + system_mapping + 物化器（v1.1/1.2 不变）
```

---

## 2. 积木体系总表（进程 / 内容 / 资源三类）

### 2.1 进程积木（modules，第 7 章机制）

| 积木 id | 代码组成 | 主要功能 | 关键性 | 启动序 | 参数段 |
|---|---|---|---|---|---|
| `repeater_gateway` | `adk_gateway_x86/` + `terminal_access/repeater/` + `runtime/ADK/` | 窄带对讲接入：ADK 收发、outbox 语音回调、短信/工具下发、抓包诊断 | optional | 30 | `links.repeater` |
| `multimode_access` | `terminal_access/multimode/` | 多模 PTT 接入：WSS/HTTP、音频解码、任务轮询桥接 | optional | 30 | `links.multimode` |
| `agent_server` | `agent_server/` + `langgraph_agent_x64/run_agent.py` | 智能体核心六能力 + 意图→流程映射 | **core** | 20 | `agent` 等 |
| `mcp_service` | `mcp_service/` + `langgraph_agent_x64/run_mcp_server.py` | 能力视图合成、三段式校验、执行器路由 | **core** | 10 | `mcp` |
| `asr_dashscope`/`asr_local`/`asr_transcriptions` | `semantics/audio.py` 三分支 | ASR 供给（恰选 1） | — | — | `models.asr.*` |
| `planner_dashscope`/`planner_local` | `semantics/reasoning/planner.py` + `models/dashscope_client.py` | 规划模型供给（恰选 1） | — | — | `models.planner` |
| `third_party_router` | `third_party_adapters/` | 第三方资源域能力路由 | optional | 插件 | `third_party` |

### 2.2 内容积木（catalogs / flows / overlays，第 8 章机制）

| 积木类型 | 落点 | 功能 | 选择方式 |
|---|---|---|---|
| 能力目录 `core` | `mcp_service/tools/`（原位规范化，冻结 wire 编号） | 平台基础工具集（查询/短信/下发等） | 隐含启用、不可禁用 |
| 能力目录 `<行业>` | `capability_catalogs/<id>/`（新增） | 行业能力集：能力定义/绑定/overlay/设备类/事件/回归样本 | `composition.catalogs` |
| 流程积木 | `flows/<ns>/<flow>.yaml`（新增） | 平台内确定性流程 DAG；或封装外部 BPM 为单能力 | `composition.flows` |
| 场景 overlay | 目录内 `overlays/*.yaml` | 校验/策略/可见性收紧（夜班、防爆区等场景） | 随目录启用 |
| 厂商包 | `vendor_packages/<vendor>/`（新增） | 协议/字段/回执/错误码映射（REST/SDK/MQTT/AsyncAPI）——**只做映射差异，不重复定义能力**（device_class 模板继承） | 由能力 `executor` 引用 |

### 2.3 资源积木（横切，随方案启用）

| 积木 | 落点 | 功能 |
|---|---|---|
| `industry_profile` | `industry_profiles/<id>/` | 行业语义资产（词典/提示词/策略/绑定/回归），manifest 校验后装载 |
| `event_journal` / `observability` | `shared_runtime/event_journal.py`、`observability.py` | 事件留痕、OTel/OpenLIT 追踪、性能采样 |
| 目录/冲突治理 | `shared_runtime/catalog_registry`（扩展）、`conflicts.py`（新增） | 全局全名注册、三层冲突检测、保留域保护 |

**进程装配壳**：`adk_gateway_x86/run_gateway.py`（x86 venv）、`langgraph_agent_x64/run_{agent,mcp_server}.py`、`multimode/run_server.py`、`start_dual_process.bat`（总启动器，v1.2 后含指针解析+物化前置）。

---

## 3. L1 业务入口层组成明细

### 3.1 `repeater_gateway`（terminal_access/repeater/）

| 子组成 | 文件 | 功能 |
|---|---|---|
| ADK 绑定 | `adk/{backend,runtime,lifecycle,channels,voice,messaging,events,reflection,diagnostics,native_output}.py` | 32 位 ADK DLL 封装：中转台生命周期、通道发现/注册、音频收发、短信事件、CLR 反射桥接 |
| HTTP API | `api/app.py`、`routers/{system,sms,voice,protobuf_tools}.py`、`security.py` | `/health`、`/sms/{private,group}`、`/voice/private/send-wav`、`/adk/protobuf/result-sms`；token 校验 |
| 上行回调 | `callbacks/{manager,outbox,merger,transport,retry_policy,circuit_breaker}.py`、`agent_client.py` | **voice_event 出口**：语音合并（静默确认）→ outbox 至少一次 → 退避重试 → 熔断 → POST agent `/voice/callback` |
| 下行投递 | `services/{gateway_service,voice_delivery}.py` | **device_command / result_push_sms 入口**：agent 下发编码后经 ADK 发对讲机 |
| 抓包诊断 | `capture/{manager,packet,tshark,sms,io_utils}.py` | tshark 抓包、语音采集落盘 |
| 设置装载 | `settings/{loader,model,parsing}.py` | 读 gateway.ini（物化产物），构建设置模型 |
| 运行支撑 | `runtime_support/bootstrap.py`、`gateway.py`、`server.py`、`transport.py` | runtime 定位、主循环、UDP transport |

### 3.2 `multimode_access`（terminal_access/multimode/）

| 文件 | 功能 |
|---|---|
| `app.py`、`run_server.py` | uvicorn 应用（18084） |
| `wss_stream.py`、`http_tasks.py` | WSS `/stream` 音频接入；HTTP `/api/tasks` 任务管理/轮询 |
| `audio_pipeline.py` | 解码、格式检查、落盘 |
| `agent_bridge.py` | **voice_event 出口 + result_poll_http 入口**：VoiceEvent 提交 + `/results/latest` 轮询映射任务状态 |
| `stores.py` | 会话/任务内存存储 |
| `control_channel/`、`data_channel/` | 骨架契约：注册/会话绑定/统一请求/结果路由；分片纠序/解码/`MultimodeAgentIngress` |
| `config.py`、`Caddyfile.example` | `MULTIMODE_*` 读取；TLS 终结建议 |

### 3.3 冻结区（terminal_access/wire/）

CBOR/L2 编解码、`mcp_bridge`、`tool_registry.TOOL_NAME_TO_ID`——受 `contracts/gateway_compatibility.json` 保护；v1.3 下数字 ToolId 仅允许 core 目录（规则 17）。

---

## 4. L2 智能体核心层组成明细（agent_server）

| 能力 | 组成 | 功能（含 v1.3 扩展标注） |
|---|---|---|
| **语义理解** | `semantics/audio.py`（**model_asr 入口**）、`text_norm.py`、`slots.py`、`prompts/`、`reasoning/{planner,canonicalizer,shortcut}.py`（**model_planner 入口**） | 转写、规整、意图与计划；**v1.3：意图分类后追加 flow registry 查询（意图→流程映射）** |
| **会话管理** | `session/`（context、memory、messages、terminal_sessions） | 会话生命周期、上下文预算组装、终端识别（终端级覆盖分流点） |
| **任务编排** | `orchestration/planning/`、`commands/`、`operations/`、`workflow/`（LangGraph 八节点） | 计划→命令→操作确定性执行链；**v1.3：flow 装载为计划模板、flow 步骤复用 commands 七态生命周期与排他门控** |
| **权限校验** | `security/policy/engine.py::evaluate(actor_id, target_ids)`、`security/approvals/` | 默认拒绝、风险门控（**终端级覆盖挂接点**）；**v1.3：flow 的 approve-gate 复用审批保留域** |
| **工具调用** | `tool_invocation/`（capabilities、mcp_executor、mcp_result） | MCP 调用组装与归一（**tool_invocation 出口**）；**v1.3：能力全名路由感知合成视图** |
| **结果反馈** | `feedback/http_api.py`（上行入口+`/results/latest`）、`callback_spool.py`、`terminal_delivery.py`（wire 编码→网关，**device_command 的 agent 侧**）、`reminder_scheduler.py` 等 | 上下行投递、定时提醒 |
| 呼叫业务 | `calls/`（lifecycle_tracker、correlator、transcript_collector、summary_generator…） | 呼叫跟踪、转写归集、摘要、迁移 |
| 实体目录 | `directories/`（contact/zone_channel/device_directory_adapter） | 口语名→终端号/信道；**v1.3：flow 参数中的站点别名同走此层（ontology）** |
| 模型客户端 | `models/dashscope_client.py` | ASR/Planner/摘要共用客户端（含性能打点） |
| 持久化/仿真/公共 | `storage/`（v3.0：双后端——sqlite 回退/本地 PostgreSQL，连接经 `sqlite_compat.py` 门面）、`simulation/`、`common/`（env.py 的 `VOICE_AGENT_ENV_FILE`=物化接入点）、`server.py`、`runtime.py` | 状态库（会话/任务/回执，并发安全：UNIQUE+行锁）、评估驱动、进程设施与装配 |

---

## 5. L3 统一能力层组成明细（mcp_service）

| 文件 | 功能（含 v1.3 扩展标注） |
|---|---|
| `server.py` | HTTP + JSON-RPC 入口（18083，协议版本协商） |
| `tool_catalog.py` | **v1.3 核心扩展位**：`ToolCatalog` 从"仅读 mcp_service/tools"扩展为 **catalog_registry 合成视图**（core + composition.catalogs 启用目录）；模型可见 = 合成视图 − 设备/终端裁剪 − overlay 隐藏 |
| `tool_service.py` | 工具服务编排、结果封装 |
| `execution.py` / `execution_context.py` | **三段式校验落点**：现 `validate_arguments`=结构校验（第一段）；v1.3 增语义校验（overlay 范围/枚举/时间窗）与执行前校验（排他门控、precondition、审批前置）；未启用 overlay/目录时后两段为空操作=现行为 |
| `device_executor.py` | 执行器路由：`GatewayTerminalExecutor`（**device_command 出口**）/ `HttpForwardExecutor` / `PrintStubExecutor` / `resource_domain` 分支（**third_party_resource 入口**，v1.3 与目录能力 `executor: resource_domain:<域>` 对齐） |
| `protocol_adapter.py` / `wire_compat.py` | MCP↔wire 兼容归一 |
| `config.py` | 端口、协议版本、执行器模式（启动器注入） |

---

## 6. L3.5 内容积木层组成明细（v1.3 新增）

### 6.1 能力目录（capability_catalogs/<id>/）

| 组成 | 功能 |
|---|---|
| `manifest.yaml` | 目录全名/版本/`requires` 依赖/`conflicts_with` 互斥/适用范围 |
| `capabilities/*.yaml` | 能力定义：全名、category（query/control/flow/notify）、side_effect、risk、executor（`resource_domain:<域>` 或 core 下发）、params_schema（结构校验）、**preconditions**（执行前校验）、**exclusive_resources**（排他声明）、idempotency、**receipt**（受理/进度/终态/未知恢复）、cancellation、timeouts、**compensation**、capability_events |
| `overlays/*.yaml` | 场景收紧面：validation（范围/枚举收紧）、policy（审批要求/时间窗/风险上限）、visibility（隐藏项）——只收紧，放宽即注册期拒绝 |
| `device_classes/*.yaml` | 设备类（如 robot.dog）：通用能力组 + 硬约束（速度/坡度/防护等级）；实例在方案 `devices.entries` 绑定 adapter + 型号 overlay |
| `events/*.yaml` | 能力事件（progress/alarm/completion），供 flow 升级链与统一通知使用 |
| `evals/regression.jsonl` | 目录发布门禁回归样本 |

**core 目录**：`mcp_service/tools/` 原位规范化为 `core` 命名空间（能力全名 `core:capability/<tool>@<ver>`，wire 数字 ID 冻结不迁移）。

**复用防重三机制**（详见设计文档 8.3）：语义重复签名检测（注册期拦截重复能力，提示复用或 alias 路径）；device_class 模板继承（厂商包只登记映射差异，不重复定义能力面）；显式 alias（变体声明 `alias_of` + 参数映射，兼容校验后复用基能力回归样本）。S1 登记环节强制填写复用检查结论。

### 6.2 流程积木（flows/<ns>/<flow>.yaml）

| 组成 | 功能 |
|---|---|
| `match` | 意图映射（装载期查唯一性；多候选须显式 `candidates_policy`） |
| `steps` | 步骤 DAG：引用能力**全名**（禁止裸名）；步骤类型=能力调用/approval-gate（保留域门控）；参数可引用 `${entities}`、`${device}`、`${steps.<id>.<output>}` |
| `lifecycle` | timeout/compensation/escalation/checkpoint（per_step 断点续跑），与 M4 七态对齐 |
| 外部流程能力（形态 B） | 外部 BPM/工单/厂商编排=目录中 `category: flow` 的能力：只暴露 submit/query/cancel + 七态跟踪，内部步骤不进平台；必须声明 receipt/idempotency/unknown_recovery，缺最终证据只能停在"结果待核实" |

### 6.3 治理组成

| 组成 | 落点 | 功能 |
|---|---|---|
| registry（全局全名注册） | `shared_runtime/catalog_registry`（扩展） | `<ns>:<type>/<name>@<semver>` 唯一性；ns 占用登记；resource_domain 唯一归属；**语义重复签名检测（复用防重，规则 23）** |
| 冲突检测 | `shared_runtime/conflicts.py`（新增） | **三层十三类**：注册期（命名/资源域双归属/wire 越权/依赖不满足/保留域越权/语义重复/设备双归属）、装载期（目录互斥/overlay 冲突/流程引用缺失/能力-执行器不匹配/终端 match 重叠）、运行期（排他并发/precondition/终端未授权）；报错含双方全名@版本与修复建议 |
| 组合可观测 | `agent_server/feedback/http_api.py`（v2.0 扩展） | `/observability/config` 快照（仅 localhost）：当前生效 profile、启用目录与能力视图计数、启用流程、覆盖条目；调用日志/trace span 携带能力全名与来源 ns |
| 保留域保护 | registry 规则 | 审批/权限/审计/七态/排他引擎/凭据仅 core 可定义，积木不可覆盖 |

---

## 7. L4 第三方适配层组成明细（third_party_adapters + vendor_packages）

| 组成 | 功能 |
|---|---|
| `base.py` | 适配器契约：同步查询、异步任务提交/状态/取消、告警上报（v1.3：含 receipt/idempotency/排他资源声明面） |
| `routing.py::ResourceDomainRouter` | **third_party_resource 出口**：按能力 `resource_domain` 路由；激活前提=绑定校验+SSRF allowlist+回执对接 |
| `adapters/{robot,drone,iot,enterprise,retail}/` | 领域适配器占位与接口形态 |
| `bindings/`、`events/` | 工具↔资源域映射；事件归一/去重契约 |
| `vendor_packages/<vendor>/`（新增） | 厂商适配包：协议声明（REST/SDK/MQTT/AsyncAPI）、字段/单位/状态/错误码映射、联调证据——协议差异全部收敛于此，能力定义与平台不感知协议 |

---

## 8. L0 公共契约与运行时组成

### 8.1 shared_contracts/（冻结优先）

| 文件 | 功能 |
|---|---|
| `routes.py` | 契约边地址层：全部路由常量与冻结端口 |
| `models.py` | `VoiceEvent` 等核心模型与契约校验（voice_event 载荷） |
| `http_client.py` / `security.py` / `trace_context.py` | 跨进程 HTTP、token/签名、追踪注入 |
| `identifiers.py` / `control_ids.py` / `event_*.py` / `schemas/` | 标识规则、控制 ID、事件契约、JSON Schema 附件 |

### 8.2 shared_runtime/

| 文件 | 功能 |
|---|---|
| `logging_config.py`、`json_utils.py`、`timing.py`、`latency.py` | 日志/序列化/计时/耗时树 |
| `observability.py`、`performance.py`、`event_journal.py`、`diagnostics.py` | OTel+OpenLIT、性能采样、事件留痕、诊断 |
| `architecture.py`、`sqlite_compat.py`、`env_file.py` | 架构标记、存储兼容门面（v3.0：双后端连接注入，调用方不动）、env 工具 |
| `config_schema.py` / `config_loader.py` / `system_config.py` / `composition.py` | 配置治理内核（schema/指针+物化/终端覆盖访问器/组合图运算） |
| `conflicts.py`（新增）、`catalog_registry`（扩展） | 内容积木注册与三层冲突检测（第 6.3 节） |

---

## 9. 配置治理层组成（v1.1~v2.0 汇总）

| 组成 | 功能 |
|---|---|
| `config/profiles/<方案id>.yaml` | 方案库：一方案一全量单文件（meta/composition[modules+catalogs+flows+connections]/links/agent/mcp/models/security/terminals/devices/industry_profile/third_party） |
| `config/module_catalog.yaml` | 进程积木目录：类型化端口/契约/数量约束/start_phase（随代码发布，只增不改） |
| `config/system_mapping.yaml`、`active_profile` | 物化映射表（全局共用）；可选指针文件 |
| `capability_catalogs/`、`flows/`、`vendor_packages/` | 内容积木库（第 6、7 章） |
| `scripts/materialize_system_config.py`、`validate_system_config.py` | 指针解析+组合校验+冲突检测+物化；独立校验器（28 条 fail-fast 规则） |
| `runtime/generated/`、`.env.*`/`gateway.ini` | 物化产物（digest+composition_digest 头）；无指针回退输入 |
| **`storage` 段（v3.0）** | 持久化后端：backend=sqlite（默认回退）/ postgresql（本机安装非容器，凭据 ${ENV:...} 注入、连接池、schema 隔离、版本化迁移 auto_apply） |
| `voice_contact_map.json`、`voice_device_directory.json` | 目录类数据（由 terminals/devices 段指针引用） |

---

## 10. 契约边 ↔ 代码落点对照

| 契约边 | 生产者 → 消费者 | 物理通道 |
|---|---|---|
| `voice_event`(repeater) | `repeater/callbacks/*` → `agent_client.py` → `feedback/http_api.py::CallbackHandler` | POST `18082/voice/callback`（outbox 至少一次+spool 幂等） |
| `voice_event`(multimode) | `multimode/agent_bridge.py::AgentVoiceIngress` → 同上 | 同一契约复用 |
| `tool_invocation` | `tool_invocation/mcp_executor` → `mcp_service/server.py` | POST `18083` JSON-RPC `tools/call` |
| `device_command` | `feedback/terminal_delivery.py`（CBOR 编码）→ `GatewayTerminalExecutor` → `repeater/api/routers/protobuf_tools.py` | POST `18081/adk/protobuf/result-sms` → ADK UDP |
| `result_push_sms` | `terminal_delivery.py` → `repeater/api/routers/sms.py` | POST `18081/sms/private` |
| `result_poll_http` | `http_api.py` → `agent_bridge.py` 轮询 | GET `18082/results/latest` |
| `model_asr` / `model_planner` | provider 分支 → ASR/规划消费逻辑 | 进程内（QWEN_* 选择） |
| `third_party_resource` | `third_party_adapters/routing.py::ResourceDomainRouter` → `mcp_service` resource_domain 分支 | 进程内注册表（激活后） |
| **能力路由（v1.3）** | 合成视图（`tool_catalog.py`+catalog_registry）→ `execution.py` 三段校验 → 能力 `executor` 字段分流 | 进程内；`resource_domain:<域>` 走上一行，core 下发走 device_command；调用日志/trace span 携带能力全名与来源 ns |
| **flow 执行（v1.3）** | `semantics/reasoning` 意图匹配 → `orchestration/planning` 装载 flow 模板 → 步骤复用 `commands` 七态+排他门控 → 各能力 | 进程内编排引擎 |

---

## 11. 场景对照速查

| 场景 | 积木组合 | 关键校验/门控 |
|---|---|---|
| 组合 1：全量实验室（缺省） | 全部进程积木+core 目录 | = 今天 `start_dual_process.bat` 行为 |
| 组合 2：纯多模查询 | 禁 repeater；mcp=print_stub | `actor_terminal_id` 联动拒绝；不物化网关产物 |
| 组合 3：评估态 | 禁两接入积木 | 上行仅 evals/simulation |
| 组合 4：第三方资源域 | +third_party_router；mcp=resource_domain | third_party↔executor_mode 联动（规则 9） |
| **机器狗巡检（v1.3）** | core+robotics 目录+patrol-rounds flow | path.plan 静态边界（地图/禁区/坡度）→ approve-gate(risk=3) → `robot:${device_id}` 排他 → progress 失联升级链 → 回执终态；越界/禁区/并发二任务/未授权全被拦；**设备接入端到端六步（登记→映射→离线→沙箱→启用→退役，见设计文档 8.6）** |
| **夜班场景切换（v1.3）** | 同方案 + night-patrol overlay | rounds≤3、需审批、时间窗 22:00-06:00（只收紧） |
| **外部工单系统（v1.3）** | 目录登记 `category: flow` 外部能力 + vendor 包协议适配 | 只见 submit/query/cancel+七态；缺最终证据停在"结果待核实" |
| **新厂商接入机器人行业（v2.0）** | device_class 模板继承 + 厂商包映射差异 + alias | 语义重复登记被拒并提示复用路径；开发量收敛到映射与联调 |
| **集群节点部署（v3.0）** | 每节点一方案文件（<站点>-<节点id>.yaml，meta.node_id）+ 集中配置库→本地物化→滚动重启 | node_id 唯一（规则 26）；PG 强制；fan-out 席位唯一（规则 27）；任务号带 node 前缀全局不冲突 |

---

## 12. 验证与运维组成

| 组成 | 功能 |
|---|---|
| `evals/text_accuracy.py`、`evals/ptt_voice/`、`evals/results/` | Planner 回归门禁、PTT 评估样本与历史 |
| `capability_catalogs/*/evals/regression.jsonl`（新增） | 目录发布门禁回归样本 |
| `tests/`、`scripts/health_probe.ps1`、`runtime/smoke_multimode.py` | 单测、健康探测（仅启用模块）、冒烟 |
| `docs/` | CONFIG_SYSTEM_DESIGN v3.0（机制+评审结论+生产化三章）/ 本文（组成对照）/ ARCHITECTURE（基线） |

---

## 13. 生产化组成（v3.0：并发任务 / 对讲机集群 / 快速部署）

### 13.1 并发任务支撑组成

| 组成 | 落点 | 功能 |
|---|---|---|
| 回调链路既有并发内核 | `feedback/http_api.py`（ThreadingHTTPServer）、`repeater/callbacks/outbox.py`（多 worker+租约+熔断）、`callback_spool.py`（幂等落库）、`common/http_pool.py` | 多终端并发基础；v3.0 方案切换不影响并发模型 |
| 任务存储并发安全 | `agent_server/storage/` + `calls/repository.py`（PG 后端：UNIQUE(task_no)+行锁） | 任务号全局唯一；单写者→多读者多写者演进 |
| 排他门控跨请求/跨节点 | `orchestration/commands`（行锁实现第 8 章排他） | 集群形态下跨节点互斥 |
| flow 并行步骤 | `orchestration/planning`（DAG 无依赖步骤并行，并行度可配） | 流程内加速 |
| 令牌桶限流 | `mcp_service/execution.py` 第三段 | 目录能力声明的 vendor 限流运行期执行 |
| 公平调度 | 调度队列参数（方案文件） | 按终端公平轮转；单终端上/下行串行保序 |

### 13.2 集群部署组成

| 组成 | 落点 | 功能 |
|---|---|---|
| 节点方案文件 | `config/profiles/<站点>-<节点id>.yaml` | 节点加入=发布新方案文件；node_id 进 meta |
| node 前缀标识 | 任务号/排他键/回调关联号（`node-a:task:123`） | 跨节点可追溯、全局不冲突（规则 26） |
| 终端归属绑定 | 既有对话绑定机制 + fan-out 席位声明 | 单播只经归属网关节点；组播席位唯一不重复播报（规则 27） |
| 集中配置→本地物化 | 配置库（git/共享目录）→ 各节点 materialize → deploy.ps1 滚动重启 | 无运行时配置共享状态，保留"停旧起新" |
| 无状态化断言 | provision_health 检查清单 | agent/mcp 全部状态入 PG，可水平复制 |

### 13.3 快速部署组成（本地 PostgreSQL，非 Docker）

| 组成 | 落点 | 功能 |
|---|---|---|
| 部署体检 | `scripts/provision_health.py` | 只读体检：Python/双 venv、本机 PG 服务连通、凭据、网卡/端口/磁盘 → 勾选清单+退出码 |
| 部署器 | `scripts/deploy.ps1` | 幂等编排：体检→安装（复用 setup.bat、离线包优先、本机 PG 系统安装包）→ 选型（形态/node_id/方案）→初始化（建库/迁移/校验）→启动冒烟→部署报告 |
| 存储迁移 | `scripts/migrate_sqlite_to_postgres.py` | SQLite→PG 一次性迁移，对账报告差异为零方可切换（规则 28）；backend=sqlite 可回退 |
| 版本化迁移 | `storage/migrations`（复用 `calls/migration_runner` 轨道） | auto_apply 启动前迁移检查 |
| 存储门面 | `shared_runtime/sqlite_compat.py` 扩展 | 双后端连接注入（DATA_SOURCE），调用方读写代码零改动 |
| 进程装配壳 | `langgraph_agent_x64/setup.bat`、`adk_gateway_x86/setup.bat` | 被 deploy.ps1 复用，支持离线安装 |

（完）实施机制见 `docs/CONFIG_SYSTEM_DESIGN.md`（v3.0 最终定版，含第 9 章评审结论与第 10 章生产化设计）；本文提供"每块积木在哪、干什么、接谁"的完整对照。
