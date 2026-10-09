# =============================================================================
# 【维护注释】文件：voice_agent/policy/engine.py
# 所属层次：语音智能体核心
# 主要职责：负责 ASR、规划、MCP 调用、设备投递、目录解析、HTTP 回调和运行时装配。
# 项目内直接依赖：voice_agent.capabilities, voice_agent.planning.models, .models
# 主要公开符号：PolicyEngine
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

from typing import Iterable

from voice_agent.capabilities import CapabilityRegistry
from voice_agent.planning.models import OperationPlan
from .models import AuthorizationDecision

_RISK_ORDER = {f"R{i}": i for i in range(6)}


class PolicyEngine:
    def __init__(self, registry: CapabilityRegistry, *, mode: str = "audit", auto_execute_max_risk: str = "R1", r2_allowlist: Iterable[str] = (), policy_id: str = "policy") -> None:
        mode = str(mode).strip().lower()
        if mode not in {"audit", "enforce"}:
            raise ValueError("POLICY_MODE must be audit or enforce")
        self.registry = registry
        self.mode = mode
        self.auto_execute_max_risk = auto_execute_max_risk
        self.r2_allowlist = frozenset(str(x) for x in r2_allowlist)
        self.policy_id = policy_id

    def evaluate(self, plan: OperationPlan, *, actor_id: str | None = None, target_ids: Iterable[str] = (), actor_roles: Iterable[str] = ()) -> AuthorizationDecision:
        capabilities = [step.capability for step in plan.steps]
        unknown = [name for name in capabilities if not self.registry.contains(name)]
        unavailable = [name for name in capabilities if self.registry.contains(name) and not self.registry.get(name).available]
        risks = [self.registry.get(name).risk_level for name in capabilities if self.registry.contains(name)] or ["R5"]
        risk = max(risks, key=lambda value: _RISK_ORDER.get(value, 5))
        if unknown or unavailable:
            desired, rule, reason = "DENY", "capability.unavailable", "Plan contains unknown or unavailable capabilities"
        elif _RISK_ORDER[risk] <= _RISK_ORDER.get(self.auto_execute_max_risk, 1):
            desired, rule, reason = "AUTHORIZE", "risk.low.auto", f"Risk {risk} is within automatic execution boundary"
        elif risk == "R2" and all(
            name in self.r2_allowlist
            for name in capabilities
            if self.registry.get(name).risk_level == "R2"
        ):
            desired, rule, reason = "AUTHORIZE", "risk.r2.allowlist", "All R2 capabilities are allowlisted"
        else:
            desired, rule, reason = "APPROVAL_REQUIRED", "risk.human-approval", f"Risk {risk} requires explicit approval"
        effective = desired if self.mode == "enforce" else "AUTHORIZE"
        return AuthorizationDecision(
            decision_id=plan.decision_id,
            decision=effective,
            audit_decision=desired if self.mode == "audit" else None,
            rule_id=rule,
            policy_id=self.policy_id,
            risk_level=risk,
            allowed_operations=capabilities if desired != "DENY" else [],
            denied_operations=capabilities if desired == "DENY" else [],
            reason=reason + (" (audit-only)" if self.mode == "audit" else ""),
            approval_required=(desired == "APPROVAL_REQUIRED"),
        )
