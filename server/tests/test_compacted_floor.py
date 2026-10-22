"""plan-527-2218: 压缩水位与例外的正确性测试。

水位语义：``id > floor`` 的消息都没被压缩，可以从 SQL 下界直接查起；
例外 = 水位以下但**没有**被遮蔽的消息（含图片附件的消息不参与压缩），
必须随下界查询一起带回，否则会从上下文静默消失。
"""

from app.orchestration.context_compressor import compute_compacted_floor


class _Msg:
    """最小消息替身：水位计算只依赖 id。"""

    def __init__(self, mid: int):
        self.id = mid


def _msgs(ids):
    return [_Msg(i) for i in ids]


def test_no_compaction_returns_zero_and_no_exceptions():
    floor, exc = compute_compacted_floor(_msgs([1, 2, 3]), set())
    assert floor == 0
    assert exc == []


def test_continuous_span_advances_floor():
    """连续遮蔽：水位取最大遮蔽 id，无例外。"""
    floor, exc = compute_compacted_floor(_msgs([1, 2, 3, 4, 5]), {1, 2, 3})
    assert floor == 3
    assert exc == []


def test_unshadowed_inside_span_becomes_exception():
    """压缩区间内未被遮蔽的消息（如含图片附件，不进压缩候选）必须列为例外。

    这是本算法与「取未被遮蔽的最小 id - 1」的关键差异：后者一旦最老消息是图片，
    水位就永远停在它前面，下界过滤彻底失效（会话 464 正是如此）。
    """
    floor, exc = compute_compacted_floor(_msgs([1, 2, 3, 4, 5]), {2, 3, 4})
    assert floor == 4
    assert exc == [1]


def test_exceptions_beyond_floor_are_dropped():
    """水位以上的消息会走正常查询，不再需要列为例外。"""
    floor, exc = compute_compacted_floor(
        _msgs([1, 2, 3, 4]), {2, 3}, prev_exceptions=[1, 4])
    assert floor == 3
    assert exc == [1]


def test_all_messages_shadowed():
    floor, exc = compute_compacted_floor(_msgs([7, 8, 9]), {7, 8, 9})
    assert floor == 9
    assert exc == []


def test_prev_exceptions_are_carried():
    """早先记录、仍在水位以下的例外要继续保留（它们本来就未被遮蔽）。

    同时验证「水位以下未遮蔽的消息本身就是例外」：11 被遮蔽后水位到 11，
    而 10 仍未被遮蔽，必须一起列为例外，否则会被下界过滤掉。
    """
    floor, exc = compute_compacted_floor(
        _msgs([10, 11]), {11}, prev_exceptions=[3])
    assert floor == 11
    assert exc == [3, 10]


def test_empty_messages_no_crash():
    floor, exc = compute_compacted_floor([], {1, 2})
    assert floor == 2
    assert exc == []
