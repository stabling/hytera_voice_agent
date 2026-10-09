# =============================================================================
# 【维护注释】文件：adk_gateway_x86/run_cli.py
# 所属层次：x86 进程启动层
# 主要职责：仅负责加载 x86 网关环境、日志与运行时约束，再进入 hytera_adk_gateway；不承载业务逻辑。
# 项目内直接依赖：shared_runtime.architecture, shared_runtime.env_file, shared_runtime.logging_config
# 主要公开符号：main
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Architecture-checked launcher for ADK doctor/interactive/server commands."""

from __future__ import annotations

from shared_runtime.architecture import require_windows_x86_python310
from shared_runtime.env_file import load_env_file
from shared_runtime.logging_config import configure_project_logging


def main() -> int:
    load_env_file(".env.gateway")
    require_windows_x86_python310()
    configure_project_logging(log_dir="logs/adk_gateway_x86")
    from hytera_adk_gateway.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
