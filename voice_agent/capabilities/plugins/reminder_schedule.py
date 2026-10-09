from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from voice_agent.capabilities.plugin_api import LocalCapabilityContext


class ReminderSchedulePlugin:
    capability_id = "reminder_schedule"

    _CHINESE_DIGITS = {
        "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    }
    _WEEKDAYS = {
        "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
    }

    def __init__(self, reminder_scheduler: Any | None = None) -> None:
        self.reminder_scheduler = reminder_scheduler

    @classmethod
    def parse_number(cls, raw: str | None) -> int | None:
        value = str(raw or "").strip()
        if not value:
            return None
        if value.isdigit():
            return int(value)
        if value in cls._CHINESE_DIGITS:
            return cls._CHINESE_DIGITS[value]
        if "十" in value:
            left, _, right = value.partition("十")
            tens = cls._CHINESE_DIGITS.get(left, 1) if left else 1
            ones = cls._CHINESE_DIGITS.get(right, 0) if right else 0
            if tens is None or ones is None:
                return None
            return tens * 10 + ones
        digits: list[str] = []
        for char in value:
            if char not in cls._CHINESE_DIGITS:
                return None
            digits.append(str(cls._CHINESE_DIGITS[char]))
        return int("".join(digits)) if digits else None

    @staticmethod
    def ceil_to_minute(value: datetime) -> datetime:
        if value.second or value.microsecond:
            value = value + timedelta(minutes=1)
        return value.replace(second=0, microsecond=0)

    @classmethod
    def extract_clock(cls, text: str) -> tuple[int, int] | None:
        colon = re.search(r"(?<!\d)(\d{1,2})[:：](\d{1,2})(?!\d)", text)
        if colon:
            hour = int(colon.group(1))
            minute = int(colon.group(2))
        else:
            clock = re.search(
                r"([0-9零〇一二两三四五六七八九十]{1,3})(?:点|时)"
                r"(?:(半|一刻|三刻)|([0-9零〇一二两三四五六七八九十]{1,3})分?)?",
                text,
            )
            if not clock:
                return None
            hour = cls.parse_number(clock.group(1))
            if hour is None:
                return None
            special = clock.group(2)
            if special == "半":
                minute = 30
            elif special == "一刻":
                minute = 15
            elif special == "三刻":
                minute = 45
            else:
                minute = cls.parse_number(clock.group(3)) if clock.group(3) else 0
                if minute is None:
                    return None

        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return None

        if any(word in text for word in ("下午", "傍晚", "晚上", "今晚", "明晚", "夜里", "夜间")):
            if hour < 12:
                hour += 12
        elif "中午" in text:
            if 1 <= hour < 11:
                hour += 12
        elif any(word in text for word in ("凌晨", "上午", "早上", "早晨", "清晨", "今早", "今晨", "明早", "明晨")):
            if hour == 12:
                hour = 0
        return hour, minute

    @classmethod
    def time_desc_to_target_datetime(
        cls,
        time_desc: str,
        *,
        now: datetime | None = None,
    ) -> str | None:
        raw = str(time_desc or "").strip()
        if not raw:
            return None
        current = now or datetime.now()
        text = re.sub(r"\s+", "", raw)

        text = re.sub(r"过(半个?小时|一刻钟)", r"\1后", text)
        text = re.sub(
            r"过([0-9零〇一二两三四五六七八九十]{1,5})(个半小时|个?(?:小时|钟头)|分钟|分|天|秒)",
            r"\1\2后",
            text,
        )

        absolute = re.search(
            r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})(?:日|号)?"
            r"(?:[T ]*)?(\d{1,2})(?::|：|点|时)(\d{1,2})?",
            raw,
        )
        if absolute:
            try:
                resolved = datetime(
                    int(absolute.group(1)),
                    int(absolute.group(2)),
                    int(absolute.group(3)),
                    int(absolute.group(4)),
                    int(absolute.group(5) or 0),
                )
                return resolved.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                return None

        relative_half_hour = re.search(
            r"([0-9零〇一二两三四五六七八九十]{1,5})个半小时(?:后|以后|之后)", text
        )
        if relative_half_hour:
            hours = cls.parse_number(relative_half_hour.group(1))
            if hours is not None:
                return cls.ceil_to_minute(
                    current + timedelta(hours=hours, minutes=30)
                ).strftime("%Y-%m-%d %H:%M")

        half_hour = re.search(r"半个?小时(?:后|以后|之后)", text)
        if half_hour:
            return cls.ceil_to_minute(current + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")
        quarter = re.search(r"一刻钟(?:后|以后|之后)", text)
        if quarter:
            return cls.ceil_to_minute(current + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M")

        relative_hour = re.search(
            r"([0-9零〇一二两三四五六七八九十]{1,5})个?(?:小时|钟头)"
            r"(?:([0-9零〇一二两三四五六七八九十]{1,5})(?:分钟|分))?"
            r"(?:后|以后|之后)",
            text,
        )
        if relative_hour:
            hours = cls.parse_number(relative_hour.group(1))
            minutes = cls.parse_number(relative_hour.group(2)) if relative_hour.group(2) else 0
            if hours is not None and minutes is not None:
                return cls.ceil_to_minute(
                    current + timedelta(hours=hours, minutes=minutes)
                ).strftime("%Y-%m-%d %H:%M")

        relative_minute = re.search(
            r"([0-9零〇一二两三四五六七八九十]{1,5})(?:分钟|分)(?:后|以后|之后)",
            text,
        )
        if relative_minute:
            minutes = cls.parse_number(relative_minute.group(1))
            if minutes is not None:
                return cls.ceil_to_minute(current + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")

        relative_second = re.search(
            r"([0-9零〇一二两三四五六七八九十]{1,5})秒(?:后|以后|之后)", text
        )
        if relative_second:
            seconds = cls.parse_number(relative_second.group(1))
            if seconds is not None:
                return cls.ceil_to_minute(current + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M")

        clock = cls.extract_clock(text)

        if clock is None:
            relative_day = re.search(
                r"([0-9零〇一二两三四五六七八九十]{1,5})天(?:后|以后|之后)",
                text,
            )
            if relative_day:
                days = cls.parse_number(relative_day.group(1))
                if days is not None:
                    return cls.ceil_to_minute(current + timedelta(days=days)).strftime("%Y-%m-%d %H:%M")

        target_date = None
        month_day = re.search(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})(?:日|号)?", text)
        if month_day:
            year = int(month_day.group(1) or current.year)
            try:
                target_date = current.replace(
                    year=year, month=int(month_day.group(2)), day=int(month_day.group(3))
                ).date()
                if not month_day.group(1) and target_date < current.date():
                    target_date = target_date.replace(year=year + 1)
            except ValueError:
                return None

        if target_date is None:
            day_offsets = (
                ("大后天", 3), ("后天", 2),
                ("明天", 1), ("明日", 1), ("明早", 1), ("明晨", 1), ("明晚", 1),
                ("今天", 0), ("今日", 0), ("今早", 0), ("今晨", 0), ("今晚", 0),
            )
            for keyword, offset in day_offsets:
                if keyword in text:
                    target_date = (current + timedelta(days=offset)).date()
                    break

        if target_date is None:
            days_later = re.search(
                r"([0-9零〇一二两三四五六七八九十]{1,5})天(?:后|以后|之后)", text
            )
            if days_later:
                amount = cls.parse_number(days_later.group(1))
                if amount is not None:
                    target_date = (current + timedelta(days=amount)).date()

        if target_date is None:
            weekday_match = re.search(r"(下下周|下周|本周|这周|周|星期)([一二三四五六日天])", text)
            if weekday_match:
                target_weekday = cls._WEEKDAYS[weekday_match.group(2)]
                prefix = weekday_match.group(1)
                if prefix == "下下周":
                    days = 14 - current.weekday() + target_weekday
                elif prefix == "下周":
                    days = 7 - current.weekday() + target_weekday
                elif prefix in ("本周", "这周"):
                    days = target_weekday - current.weekday()
                else:
                    days = (target_weekday - current.weekday()) % 7
                    if days == 0 and clock is not None:
                        candidate = current.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
                        if candidate <= current:
                            days = 7
                target_date = (current + timedelta(days=days)).date()

        if clock is None:
            if target_date is None:
                if text in ("现在", "马上", "立即"):
                    return cls.ceil_to_minute(current + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M")
                return None
            resolved = cls.ceil_to_minute(datetime.combine(target_date, current.time()))
            if resolved <= current:
                resolved += timedelta(minutes=1)
            return resolved.strftime("%Y-%m-%d %H:%M")

        if target_date is None:
            target_date = current.date()
        resolved = datetime.combine(target_date, datetime.min.time()).replace(hour=clock[0], minute=clock[1])
        if resolved <= current and not any(
            keyword in text
            for keyword in (
                "今天", "今日", "今早", "今晨", "今晚",
                "明天", "明日", "明早", "明晨", "明晚",
                "后天", "大后天", "月", "周", "星期",
            )
        ):
            resolved += timedelta(days=1)
        return resolved.strftime("%Y-%m-%d %H:%M")

    def execute(
        self,
        arguments: dict[str, Any],
        context: LocalCapabilityContext,
    ) -> dict[str, Any]:
        if self.reminder_scheduler is None:
            return {
                "ok": False,
                "error": "reminder_scheduler_unavailable",
                "error_type": "UNAVAILABLE",
            }
        if context.terminal_id is None:
            return {
                "ok": False,
                "error": "terminal_id_required",
                "error_type": "INVALID_CONTEXT",
            }
        time_desc = str(arguments.get("time_desc") or "").strip()
        target_datetime = str(arguments.get("target_datetime") or "").strip()
        if not target_datetime and time_desc:
            target_datetime = self.time_desc_to_target_datetime(time_desc) or ""
        if not target_datetime:
            if time_desc:
                return {
                    "ok": False,
                    "error": "time_desc_not_resolved",
                    "error_type": "NEEDS_CLARIFICATION",
                }
            return {
                "ok": False,
                "error": "target_datetime_required",
                "error_type": "NEEDS_CLARIFICATION",
            }
        value = self.reminder_scheduler.schedule_reminder(
            task=str(arguments.get("task") or "").strip(),
            time_desc=time_desc or target_datetime,
            target_datetime=target_datetime,
            terminal_id=int(context.terminal_id),
            channel_id=context.channel_id,
            request_id=context.request_id,
            origin_command_id=context.operation.origin_command_id,
        )
        return {"ok": bool(value.get("ok")), "result": value, "error": value.get("error")}
