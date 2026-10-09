# =============================================================================
# 【维护注释】文件：hytera_adk_gateway/server.py
# 所属层次：x86 ADK 网关核心
# 主要职责：实现 32 位 Windows/Python 3.10 侧的海能达 ADK HTTP 网关。对外行为必须优先兼容现网和真实终端。
# 项目内直接依赖：.config, .logging_config, .runtime_support.bootstrap, shared_runtime.observability
# 主要公开符号：create_app, main
# 兼容性：不得改变既有外部 HTTP 路径、protobuf 字段/工具 ID、ADK 调用含义或公开 Python 导入路径。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""HTTP 服务兼容入口；应用工厂与路由位于 ``hytera_adk_gateway.api``。"""

from __future__ import annotations

import argparse
from typing import List, Optional

from .config import load_config
from .logging_config import configure_project_logging
from .runtime_support.bootstrap import prepare_runtime_imports
from shared_runtime.observability import install_asgi_observability


def create_app(config_path: str | None = None, *, debug_fields: bool | None = None):
    prepare_runtime_imports()
    from .api.app import create_app as factory

    app = factory(config_path, debug_fields=debug_fields)
    return install_asgi_observability(app, "hytera-adk-gateway")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="启动 Hytera ADK HTTP 网关服务")
    parser.add_argument("--config", default="config/gateway.ini", help="配置文件路径")
    parser.add_argument("--host", default=None, help="监听地址")
    parser.add_argument("--port", type=int, default=None, help="监听端口")
    parser.add_argument("--debug-fields", action="store_true", help="输出 CLR 反射字段")
    args = parser.parse_args(argv)

    configure_project_logging()
    prepare_runtime_imports()
    cfg = load_config(args.config)
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("uvicorn 未安装，请先执行 setup_venv.bat") from exc
    uvicorn.run(
        create_app(args.config, debug_fields=args.debug_fields),
        host=args.host or cfg.server_host,
        port=args.port or cfg.server_port,
        workers=1,
        lifespan="on",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
