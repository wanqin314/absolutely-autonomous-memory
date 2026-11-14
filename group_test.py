"""群聊主动发话 + 走神不进群的自测。"""

import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import astrbot_plugin_absolutely_autonomous_memory.main as M  # noqa: E402
from astrbot_plugin_absolutely_autonomous_memory.store import Store  # noqa: E402


class Ctx:
    def __init__(self):
        self.sent = []

    async def send_message(self, session, chain):
        self.sent.append((session, str(chain)))
        return True


class Ev:
    unified_msg_origin = "bot:GroupMessage:123456789"


def make(db, conf, ctx=None):
    p = M.DafeiyuMemory.__new__(M.DafeiyuMemory)
    p.conf = dict(conf)
    p.db_path = db
    p.store = None
    p.context = ctx or Ctx()
    return p


def main():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "t.db")
    st = Store(db)
    st.conn.execute(
        "INSERT INTO raw_message(session_id,scope,role,text,sender,created_at,source) "
        "VALUES (?,?,?,?,?,?,?)",
        ("bot:GroupMessage:123456789", "group:123456789", "user", "hi", "x",
         "2026-11-14T16:00:00+08:00", "live"))
    st.conn.commit()
    st.close()

    conf = {"wander_enabled": True, "wander_quiet_hours": "",
            "wander_idle_minutes": 0, "wander_max_per_day": 3,
            "wander_max_thoughts_per_day": 6,
            "group_send_enabled": True, "group_send_allow": "123456789",
            "group_send_max_per_day": 2, "group_send_quiet_hours": ""}

    # 走神：群聊直接拒绝
    p = make(db, conf)
    r = asyncio.run(p._wander_once(session="bot:GroupMessage:123456789"))
    assert r and r.get("skipped") == "group", r

    # 群发：能发、会记次、到上限
    p = make(db, conf)
    assert asyncio.run(p.send_group_message(Ev(), "在吗", "")) == "说了。"
    assert p.context.sent and p.context.sent[0][0] == Ev.unified_msg_origin, p.context.sent
    assert asyncio.run(p.send_group_message(Ev(), "再说一句", "")) == "说了。"
    third = asyncio.run(p.send_group_message(Ev(), "第三句", ""))
    assert "已经主动说过" in third, third
    assert len(p.context.sent) == 2, p.context.sent

    # 不在名单：拒发
    p2 = make(db, {**conf, "group_send_allow": "999"})
    r = asyncio.run(p2.send_group_message(Ev(), "你好", ""))
    assert "不在允许" in r, r
    assert not p2.context.sent

    st = Store(db)
    st.close()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print("群聊主动发话 / 走神不进群：全部通过")


if __name__ == "__main__":
    main()
