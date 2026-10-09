# =============================================================================
# 【维护注释】文件：shared_contracts/control_ids.py
# 所属层次：共享契约层（x86 网关与 x64 智能体共用）
# 主要职责：定义 CONTROL_IDS / LOCAL_CONTROL_ID 统一身份契约与端点归一化。
# 项目内直接依赖：无
# 主要公开符号：CONTROL_IDS, LOCAL_CONTROL_ID, normalize_endpoint_id, is_control_id,
#               classify_endpoints, IdentityConfig, load_identity_config
# 兼容性：进程启动时一次性加载为内存集合，运行期只做 O(1) 集合查询；
#         分类过程禁止 SQLite/HTTP/MCP/LLM/远程配置访问。
# 修改原则：控制分类以通信端点 ID 为权威依据；origin 只作审计元数据。
# 排障顺序：先查环境变量 CONTROL_IDS/LOCAL_CONTROL_ID，再查调用方归一化输入。
# =============================================================================
"""CONTROL_IDS / LOCAL_CONTROL_ID 契约。

统一约定（见需求 §2/§3/§43）：

- ``CONTROL_IDS``：控制端 ID 集合。消息的 ``source_id`` / ``target_id`` /
  ``group_id`` 任意一个命中集合即为控制消息。
- ``LOCAL_CONTROL_ID``：中转台自身主动发送语音/短信的固定 ID，
  必须属于 ``CONTROL_IDS``。
- 控制分类以通信端点 ID 为权威依据；``origin`` 仅作审计元数据。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable

ENV_CONTROL_IDS = "CONTROL_IDS"
ENV_LOCAL_CONTROL_ID = "LOCAL_CONTROL_ID"

DEFAULT_CONTROL_IDS = "1"


def normalize_endpoint_id(value: Any) -> str | None:
    """把任意端点 ID 归一化为可比较字符串；空值返回 None。

    分类热路径只调用本函数 + 集合查询，不做任何 I/O。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    if not text or text.lower() in {"none", "null"}:
        return None
    return text


def _parse_control_ids(raw: Any) -> frozenset[str]:
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        items: Iterable[Any] = raw.split(",")
    else:
        items = raw
    normalized = {normalize_endpoint_id(item) for item in items}
    normalized.discard(None)
    return frozenset(normalized)  # type: ignore[arg-type]


@dataclass(frozen=True)
class IdentityConfig:
    """不可变身份配置；进程启动时加载一次，运行期 O(1) 查询。"""

    control_ids: frozenset[str]
    local_control_id: str

    def is_control_id(self, value: Any) -> bool:
        normalized = normalize_endpoint_id(value)
        return normalized is not None and normalized in self.control_ids

    def as_dict(self) -> dict[str, Any]:
        return {
            "control_ids": sorted(self.control_ids),
            "local_control_id": self.local_control_id,
        }


def build_identity_config(
    control_ids: Any = None, local_control_id: Any = None
) -> IdentityConfig:
    """根据给定取值构建身份配置（测试与显式注入用）。

    - ``CONTROL_IDS`` 至少包含一个 ID；
    - ``LOCAL_CONTROL_ID`` 缺省取第一个控制 ID，且必须属于 ``CONTROL_IDS``。
    """
    if control_ids is None:
        control_ids = os.environ.get(ENV_CONTROL_IDS, DEFAULT_CONTROL_IDS)
    ids = _parse_control_ids(control_ids)
    if not ids:
        raise ValueError("CONTROL_IDS must contain at least one ID")
    local_raw = local_control_id
    if local_raw is None:
        local_raw = os.environ.get(ENV_LOCAL_CONTROL_ID, "").strip()
    local = normalize_endpoint_id(local_raw)
    if local is None:
        local = sorted(ids)[0]
    if local not in ids:
        raise ValueError(
            f"LOCAL_CONTROL_ID {local!r} must belong to CONTROL_IDS {sorted(ids)}"
        )
    return IdentityConfig(control_ids=ids, local_control_id=local)


def classify_endpoints(
    identity: IdentityConfig,
    source_id: Any = None,
    target_id: Any = None,
    group_id: Any = None,
) -> tuple[bool, str | None]:
    """按端点判断是否控制消息，并返回命中的原因字段。

    返回 ``(is_control, hit_field)``；``hit_field`` 为命中的字段名
    （source_id/target_id/group_id），非控制消息时为 None。
    """
    for field, value in (("source_id", source_id), ("target_id", target_id), ("group_id", group_id)):
        if identity.is_control_id(value):
            return True, field
    return False, None


# ---------------------------------------------------------------------------
# 进程级默认配置：导入时一次性加载，运行期只读。
# ---------------------------------------------------------------------------
DEFAULT_IDENTITY = build_identity_config()

CONTROL_IDS: frozenset[str] = DEFAULT_IDENTITY.control_ids
LOCAL_CONTROL_ID: str = DEFAULT_IDENTITY.local_control_id


def is_control_id(value: Any, identity: IdentityConfig | None = None) -> bool:
    cfg = identity or DEFAULT_IDENTITY
    return cfg.is_control_id(value)


__all__ = [
    "ENV_CONTROL_IDS",
    "ENV_LOCAL_CONTROL_ID",
    "CONTROL_IDS",
    "LOCAL_CONTROL_ID",
    "IdentityConfig",
    "build_identity_config",
    "classify_endpoints",
    "is_control_id",
    "normalize_endpoint_id",
]
