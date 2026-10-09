# -*- coding: utf-8 -*-
"""无线区域/信道模式的统一定义与规范化。"""

from __future__ import annotations

from typing import Final


RADIO_MODES: Final[tuple[str, ...]] = (
    "active",
    "conventional",
    "trunking",
)

DEFAULT_RADIO_MODE: Final[str] = "conventional"


_MODE_ALIASES: Final[dict[str, str]] = {
    # 标准值
    "active": "active",
    "conventional": "conventional",
    "trunking": "trunking",

    # 中文自然语言兼容
    "当前": "active",
    "当前模式": "active",
    "激活": "active",
    "激活模式": "active",

    "常规": "conventional",
    "常规模式": "conventional",

    "集群": "trunking",
    "集群模式": "trunking",
}


def normalize_radio_mode(
    value: object | None,
    *,
    default: str = DEFAULT_RADIO_MODE,
) -> str:
    """
    将 mode 统一转换为系统标准值。

    规则：
    1. 没传 / 空字符串 -> conventional
    2. 支持中文别名
    3. 最终只允许：
       active / conventional / trunking
    """

    text = str(value or "").strip()

    if not text:
        return default

    normalized = _MODE_ALIASES.get(
        text.lower(),
        _MODE_ALIASES.get(text, text.lower()),
    )

    if normalized not in RADIO_MODES:
        raise ValueError(
            f"invalid radio mode: {value!r}; "
            f"allowed={list(RADIO_MODES)}"
        )

    return normalized