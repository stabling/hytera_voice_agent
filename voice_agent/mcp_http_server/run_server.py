# =============================================================================
# 【维护注释】文件：voice_agent/mcp_http_server/run_server.py
# 所属层次：MCP 服务边界
# 主要职责：负责 MCP 工具目录、JSON-RPC 适配、参数校验、设备执行和 HTTP Server；工具 YAML 是模型可见 schema 的主要来源。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：main
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""MCP HTTP 服务的直接脚本入口。"""

from __future__ import annotations

import importlib
from pathlib import Path
import sys


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    project_root_text = str(project_root)
    if project_root_text not in sys.path:
        sys.path.insert(0, project_root_text)

    from shared_runtime.observability import initialize_observability
    initialize_observability("hytera-mcp-server")

    server_module = importlib.import_module("voice_agent.mcp_http_server.server")
    server_module.run_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
