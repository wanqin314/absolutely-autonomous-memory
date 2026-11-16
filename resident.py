"""她自己放的常驻区：范围解析与渲染（不依赖 AstrBot，方便离线测试）。"""

from __future__ import annotations


def scope_keys(event) -> list[str]:
    """当前会话能看到的范围：global + 这个私聊 / 这个群。"""
    keys = ["global"]
    try:
        gid = str(event.get_group_id() or "")
    except Exception:  # noqa: BLE001
        gid = ""
    if gid:
        keys.append("group:" + gid)
        return keys
    try:
        sid = str(event.get_sender_id() or "")
    except Exception:  # noqa: BLE001
        sid = ""
    if sid:
        keys.append("private:" + sid)
    return keys


def resolve_scope(event, scope: str) -> str:
    """把工具参数 global/private/group 解析成实际 scope。"""
    s = (scope or "global").strip().lower()
    s = {"全局": "global", "私聊": "private", "群": "group", "群聊": "group"}.get(s, s)
    if s == "global":
        return "global"
    if s == "private":
        try:
            sid = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            sid = ""
        if not sid:
            raise ValueError("现在认不出私聊对象")
        return "private:" + sid
    if s == "group":
        try:
            gid = str(event.get_group_id() or "")
        except Exception:  # noqa: BLE001
            gid = ""
        if not gid:
            raise ValueError("现在不在群里")
        return "group:" + gid
    raise ValueError("scope 只能是 global / private / group")


def render(rows: list[dict]) -> str:
    """原样渲染她写的条目，不加总结、不筛选。"""
    if not rows:
        return ""
    lines = ["【你自己放在常驻区的】"]
    for r in rows:
        sc = str(r.get("scope") or "global")
        if sc == "global":
            label = "全局"
        elif sc.startswith("group:"):
            label = "这个群"
        else:
            label = "这个私聊"
        lines.append(f"- [{label}] {r.get('text', '')}")
    return "\n".join(lines)
