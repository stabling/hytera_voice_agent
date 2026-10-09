# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/audio.py
# 所属层次：x86 ADK 网关核心
# 主要职责：实现 32 位 Windows/Python 3.10 侧的海能达 ADK HTTP 网关。对外行为必须优先兼容现网和真实终端。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：AudioPipelineError, AdkAudioPipeline
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from __future__ import annotations

from typing import Any


class AudioPipelineError(RuntimeError):
    pass


class AdkAudioPipeline:
    """Audio.dll-backed local microphone/speaker bridge exposed by the CLR wrapper."""

    def __init__(self, adk: Any, cfg: Any, emit: Any) -> None:
        self._adk = adk
        self._cfg = cfg
        self._emit = emit
        self._sound: Any | None = None
        self._attached: set[int] = set()

    def start(self) -> None:
        try:
            self._sound = self._adk.GetSoundController()
        except Exception as exc:
            raise AudioPipelineError(
                "ADK did not expose SoundController; verify the V4 Audio.dll beside ADKCore.dll."
            ) from exc
        if self._sound is None:
            raise AudioPipelineError(
                "ADK returned no SoundController; verify x86 Python and Audio.dll."
            )

    def device_counts(self) -> dict[str, int]:
        if self._sound is None:
            return {"input": 0, "output": 0}
        return {
            "input": int(self._sound.GetSoundDeviceCount(True)),
            "output": int(self._sound.GetSoundDeviceCount(False)),
        }

    def attach(self, channel_id: int) -> None:
        if channel_id in self._attached:
            return
        if self._sound is None:
            raise AudioPipelineError("Audio pipeline was not started.")
        try:
            self._sound.PlayChannelSound(
                int(channel_id),
                int(self._cfg.input_device),
                int(self._cfg.output_device),
                int(self._cfg.play_flag),
            )
        except Exception as exc:
            raise AudioPipelineError(
                "PlayChannelSound failed. Check audio device indexes, x86 Audio.dll, and local microphone permissions."
            ) from exc
        self._attached.add(int(channel_id))
        self._emit(
            "audio_attached",
            int(channel_id),
            metadata={
                "input_device": self._cfg.input_device,
                "output_device": self._cfg.output_device,
                "play_flag": self._cfg.play_flag,
            },
        )
