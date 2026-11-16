"""走神自测：不需要 AstrBot、不连 QQ、不发消息。"""

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import astrbot_plugin_absolutely_autonomous_memory.main as M  # noqa: E402
from astrbot_plugin_absolutely_autonomous_memory.store import Store  # noqa: E402


class FakeProvider:
    def __init__(self, text):
        self.text = text

    async def text_chat(self, **kwargs):
        return SimpleNamespace(completion_text=self.text)


def make_plugin(db_path, conf):
    p = M.DafeiyuMemory.__new__(M.DafeiyuMemory)
    p.conf = dict(conf)
    p.db_path = db_path
    p.store = None
    p.context = SimpleNamespace()
    return p


def seed(store):
    def mk(text, days, state="open"):
        tid = store.add_trace(text, "unresolved", importance=0.7)
        store.conn.execute(
            "UPDATE trace SET last_mentioned_at=?, state=? WHERE id=?",
            ((datetime.now() - timedelta(days=days)).isoformat(timespec="seconds"), state, tid))
        return tid

    a = mk("她说想去看海", 40, "old")
    store.add_core("我是小鲸鱼，嘴硬，认人。")
    store.conn.execute(
        "INSERT INTO raw_message(session_id, scope, role, text, sender, created_at, source) "
        "VALUES ('sess','private','user','嗨','婉清',?, 'live')",
        ((datetime.now() - timedelta(minutes=60)).isoformat(timespec="seconds"),))
    store.conn.commit()
    return a


def main():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "t.db")
    st = Store(db)
    assert {r[1] for r in st.conn.execute("PRAGMA table_info(reflection)")}, "reflection 表没建"
    assert {r[1] for r in st.conn.execute("PRAGMA table_info(core_memory)")}, "core_memory 表没建"
    assert {r[1] for r in st.conn.execute("PRAGMA table_info(wander_trigger)")}, "wander_trigger 表没建"
    tid_a = seed(st)
    st.close()

    base = {"wander_enabled": False, "wander_quiet_hours": "1-7"}

    # 1. 关着：什么都不做
    p = make_plugin(db, base)
    assert asyncio.run(p._wander_once()) is None

    # 2. 概率门（0 概率不往下走）
    p = make_plugin(db, {**base, "wander_enabled": True, "wander_probability": 0})
    r = asyncio.run(p._wander_once(session="sess", provider=FakeProvider('{"decision":"silent"}'),
                                   send_func=None))
    assert r and r.get("skipped") == "probability", r

    # 3. 沉默：只落库不发
    p = make_plugin(db, {**base, "wander_enabled": True})
    sent = []

    async def send(t):
        sent.append(t)

    prov = FakeProvider('{"decision":"silent","source_trace":%d,"reason":"不惦记了","message":""}' % tid_a)
    r = asyncio.run(p._wander_once(session="sess", provider=prov, send_func=send, force=True))
    assert r and r["decision"] == "silent", r
    assert sent == []

    # 再加一条 B，A 已在 7 天内想过 → 只剩 B 可挑
    st = Store(db)
    tid_b = st.add_trace("她说想学着做饭", "unresolved", importance=0.7)
    st.conn.execute(
        "UPDATE trace SET last_mentioned_at=?, state='stale' WHERE id=?",
        ((datetime.now() - timedelta(days=20)).isoformat(timespec="seconds"), tid_b))
    st.conn.commit()
    st.close()

    # 4. 说出来的不是眼前这条 → 丢弃（A 已在 7 天内想过，被排除，眼前是 B）
    prov = FakeProvider('{"decision":"speak","source_trace":%d,"reason":"x","message":"再说说海"}' % tid_a)
    r = asyncio.run(p._wander_once(session="sess", provider=prov, send_func=send, force=True))
    assert r and r.get("decision") == "discard", r
    assert sent == []

    # 5. 指回眼前这条（B）→ 说话
    prov = FakeProvider('{"decision":"speak","source_trace":%d,"reason":"想起做饭","message":"饭学会了吗"}' % tid_b)
    r = asyncio.run(p._wander_once(session="sess", provider=prov, send_func=send, force=True))
    assert r and r.get("decision") == "speak", r
    assert sent == ["饭学会了吗"], sent

    # 6. 幻觉：来源不存在 → 丢弃（先加一条没被想过的 C）
    st = Store(db)
    tid_c = st.add_trace("她说想去爬山", "unresolved", importance=0.7)
    st.conn.execute(
        "UPDATE trace SET last_mentioned_at=? WHERE id=?",
        ((datetime.now() - timedelta(days=3)).isoformat(timespec="seconds"), tid_c))
    st.conn.commit()
    st.close()
    before = len(sent)
    prov = FakeProvider('{"decision":"speak","source_trace":999999,"reason":"x","message":"瞎编"}')
    r = asyncio.run(p._wander_once(session="sess", provider=prov, send_func=send, force=True))
    assert r and r["decision"] == "discard", r
    assert len(sent) == before

    # 7. 你刚说话 → 空闲门
    st = Store(db)
    st.conn.execute(
        "INSERT INTO raw_message(session_id, scope, role, text, sender, created_at, source) "
        "VALUES ('sess','private','user','刚说的','婉清',?, 'live')",
        (datetime.now().isoformat(timespec="seconds"),))
    st.conn.commit(); st.close()
    prov = FakeProvider('{"decision":"silent","source_trace":%d,"reason":"x","message":""}' % tid_b)
    r = asyncio.run(p._wander_once(session="sess", provider=prov, send_func=send, force=True))
    assert r and r.get("skipped") == "idle", r

    # 8. 每日上限
    st = Store(db)
    st.conn.execute(
        "UPDATE raw_message SET created_at=? WHERE session_id='sess'",
        ((datetime.now() - timedelta(minutes=60)).isoformat(timespec="seconds"),))
    st.conn.commit()
    for _ in range(3):
        st.add_reflection("sess", [tid_b], "speak", "上限测试", "x", tid_b)
    st.close()
    prov = FakeProvider('{"decision":"silent","source_trace":%d,"reason":"x","message":""}' % tid_b)
    r = asyncio.run(p._wander_once(session="sess", provider=prov, send_func=send, force=True))
    assert r and r.get("skipped") == "cap", r

    # 9. 触发队列：入队 / 去重 / 到期 / 完成
    st = Store(db)
    t1 = st.enqueue_trigger("memory", tid_b, 0)
    assert st.due_triggers(5), "到期触发器没取到"
    same = st.enqueue_trigger("memory", tid_b, 0)
    assert same == t1, (t1, same)
    st.finish_trigger(t1)
    assert not st.due_triggers(5)
    assert tid_b in st.recent_reflected_traces(7)
    st.close()

    # 10. 解析器与核心层
    assert M.parse_wander_reply('```json\n{"decision":"silent","source_trace":1,"reason":""}\n```')
    assert M.parse_wander_reply("这不是 JSON") is None
    assert M.parse_wander_reply('{"decision":"maybe"}') is None
    st = Store(db)
    st.add_core("第二条")
    assert len(st.core_entries(5)) == 2
    st.close()

    print("走神自测：全部通过")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
