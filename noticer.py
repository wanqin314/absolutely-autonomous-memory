"""注意到：规则筛选，判断这段对话要不要交给模型提炼（M2）。

规则全部是确定性的、可解释的；不做价值判断，只做「有没有可能值得留」的粗筛。
"""

from __future__ import annotations

EXPLICIT = ("记住", "别忘", "别忘了", "你记错了", "以后注意", "要记得", "记一下", "存一下")
PROMISE = ("答应", "说好", "约好", "明天", "后天", "下周", "下个月", "几点", "等你", "回头", "到时候", "说定了")
EMOTION = ("难过", "生气", "委屈", "开心", "好累", "烦死", "害怕", "想哭", "哭了", "笑死", "舍不得", "想你", "在乎")
PAST = ("上次", "以前", "还记得", "说过", "提过", "那会儿", "当初")


def _hit(text: str, words) -> bool:
    return any(w in text for w in words)


def check(user_text: str, pending: int, idle_minutes: float,
          batch_size: int = 8, idle_threshold: float = 30.0) -> str | None:
    """返回触发原因（字符串）或 None。"""
    t = user_text or ""
    if _hit(t, EXPLICIT):
        return "explicit"
    if _hit(t, PROMISE):
        return "promise"
    if _hit(t, EMOTION):
        return "emotion"
    if _hit(t, PAST):
        return "past"
    if pending >= batch_size:
        return "batch"
    if idle_minutes >= idle_threshold:
        return "idle"
    return None
