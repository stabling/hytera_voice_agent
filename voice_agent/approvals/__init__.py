# =============================================================================
# 【维护注释】文件：voice_agent/approvals/__init__.py
# 所属层次：人工审批层
# 主要职责：负责高风险操作的审批请求、不可变审批事件和审核结果持久化。
# 项目内直接依赖：.models, .repository, .service
# 主要公开符号：无；以导入副作用、常量、数据模型或包导出为主
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
from .models import Approval
from .repository import ApprovalRepository
from .service import ApprovalService
__all__ = ["Approval", "ApprovalRepository", "ApprovalService"]
