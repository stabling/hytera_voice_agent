# -*- coding: utf-8 -*-
# =============================================================================
# 【维护注释】文件：shared_runtime/json_utils.py
# 所属层次：跨进程共享运行时
# 主要职责：提供日志、可观测性、事件日志、环境加载、架构检查和无业务倾向的通用工具。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：canonical_json, compact_json
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""项目内通用 JSON 序列化工具。

该模块只提供无状态、无 I/O 的纯函数，目的是消除多个 SQLite Repository、日志模块中
重复的 ``json.dumps`` 参数组合。统一序列化规则后，哈希、幂等键、审计日志和数据库
JSON 字段使用相同的 UTF-8/排序/紧凑格式，避免同一对象因序列化细节不同产生不一致。

兼容性约束：
- 不改变任何外部 HTTP/MCP/protobuf 协议；
- 不对值做业务归一化，仅负责 JSON 文本编码；
- ``canonical_json`` 保证 key 排序，适合哈希和持久化；
- ``compact_json`` 保留插入顺序，适合结构化日志展示。
"""

from __future__ import annotations

import json
from typing import Any


def canonical_json(value: Any) -> str:
    """返回稳定、紧凑、按 key 排序的 JSON 文本。

    主要用于数据库 JSON 字段、幂等摘要和需要稳定字节表示的内部场景。
    ``ensure_ascii=False`` 保证中文可直接阅读；紧凑分隔符减少存储与日志体积。
    """

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compact_json(value: Any, *, default: Any = str) -> str:
    """返回紧凑但不强制排序的 JSON 文本，默认将未知对象转换为字符串。

    该函数主要用于日志，不参与协议签名或业务哈希，因此保留调用方构造字典的自然顺序，
    便于排查链路时按业务字段顺序阅读。
    """

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=default)


__all__ = ["canonical_json", "compact_json"]
