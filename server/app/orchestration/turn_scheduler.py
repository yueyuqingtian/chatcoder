"""v0.3: 预算跟踪器(原发言权调度器重构)。

变更说明:
- speaker token(令牌制发言权)与 v0.3 的并行层调度冲突(并行需多 agent 同时工作),
  本版暂停;代码与概念保留,等 v0.4 "自由讨论/chat 模式" 再启用。
- 保留 session 级 token 预算跟踪(BudgetTracker),供成本控制与熔断使用。

并行任务共享同一个 BudgetTracker,任一任务超预算 → 整 session 熔断。
"""
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class BudgetTracker:
    """每个会话一个预算跟踪器(并行任务共享)。"""

    session_id: int
    token_budget: int = 200_000  # 单会话 token 预算
    _total_tokens: int = 0
    _frozen: bool = False

    def consume_tokens(self, count: int) -> bool:
        """记录 token 消耗,超预算则熔断。返回是否仍可用。"""
        self._total_tokens += count
        if self._total_tokens >= self.token_budget:
            if not self._frozen:
                logger.warning(
                    "session %s token 预算熔断: %s/%s",
                    self.session_id, self._total_tokens, self.token_budget,
                )
            self._frozen = True
            return False
        return not self._frozen

    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def total_tokens(self) -> int:
        return self._total_tokens


# 全局注册表:session_id -> BudgetTracker
_trackers: dict[int, BudgetTracker] = {}


def get_scheduler(session_id: int) -> BudgetTracker:
    """保留旧函数名,返 BudgetTracker(v0.3 兼容入口)。"""
    if session_id not in _trackers:
        from app.core.config import settings
        budget = getattr(settings, 'session_token_budget', 2_000_000)
        _trackers[session_id] = BudgetTracker(session_id=session_id, token_budget=budget)
    return _trackers[session_id]


# 向后兼容别名
get_budget_tracker = get_scheduler


# ───────────────────────── 旧 speaker token 概念(暂停,保留注释) ─────────────────────────
# v0.4 计划:当引入"自由讨论 / agent 互聊"模式时,在 BudgetTracker 之上叠加:
#   - TurnToken:同一时刻仅持令牌者可在主群发言
#   - max_consecutive_turns:单 agent 连续发言上限
#   - acquire/release:用户优先抢占、超时回收
# 当前 v0.3 任务执行模式:agent 在 thread 内独立工作,主群只发关键节点卡片,
# 不存在"互聊抢话",故 speaker token 不启用。
