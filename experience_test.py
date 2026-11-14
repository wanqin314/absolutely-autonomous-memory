"""三块字段、原话、时效的自测。不需要 AstrBot。"""

import os
import sys
import tempfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import astrbot_plugin_absolutely_autonomous_memory.archivist as A  # noqa: E402
from astrbot_plugin_absolutely_autonomous_memory.store import Store  # noqa: E402


def main():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "t.db")
    st = Store(db)

    raw = """{"items":[{"kind":"observation","text":"她把碗放下，说“不吃了”。",
      "importance":0.5,"felt":"闷","reaction":"没接话，去把碗收了",
      "inference":"她可能累了","inference_certainty":0.6,
      "anchor_quote":"不吃了","topics":["晚饭"]}],"updates":[]}"""
    items, _ = A.parse_items(raw)
    assert items and items[0]["felt"] == "闷", items
    assert items[0]["reaction"] == "没接话，去把碗收了", items
    assert items[0]["inference_certainty"] == 0.6, items

    lines = [{"role": "user", "text": "不吃了", "sender": "某人",
              "created_at": datetime.now().isoformat(timespec="seconds")}]
    n = A.write_items(st, items, lines, "private", "sess")
    assert n == 1, n
    t = st.conn.execute("SELECT t.id FROM trace t").fetchone()
    tid = int(t["id"])
    got = st.get_trace(tid)
    assert got["felt"] == "闷", got
    assert got["reaction"] == "没接话，去把碗收了", got
    assert got["inference"] == "她可能累了", got
    assert got["inference_certainty"] == 0.6, got
    assert "不吃了" in (got["anchor_quote"] or ""), got
    assert "不吃了" in st.episode_transcript(tid), "transcript 没写上"

    # 她自己的话
    st.add_note(tid, "那天其实不是嫌饭凉。", kind="her_words")
    kinds = [r["kind"] for r in st.conn.execute(
        "SELECT kind FROM trace_note WHERE trace_id=?", (tid,))]
    assert "her_words" in kinds, kinds

    # 时效：4 天没提起 → 候选；问过 → 不再问；8 天后 → 休眠
    t2 = st.add_trace("答应过的事", "promise")
    old = (datetime.now() - timedelta(days=4)).isoformat(timespec="seconds")
    st.conn.execute("UPDATE trace SET last_mentioned_at=? WHERE id=?", (old, t2))
    st.conn.commit()
    ids = [c["id"] for c in st.review_candidates(3, 7)]
    assert t2 in ids, ids
    st.mark_review_asked(t2)
    ids = [c["id"] for c in st.review_candidates(3, 7)]
    assert t2 not in ids, ids
    older = (datetime.now() - timedelta(days=8)).isoformat(timespec="seconds")
    st.conn.execute("UPDATE trace SET review_asked_at=? WHERE id=?", (older, t2))
    st.conn.commit()
    assert t2 in st.dormant_candidates(7), "没进休眠候选"
    st.mark_dormant([t2])
    assert st.conn.execute("SELECT state FROM trace WHERE id=?", (t2,)).fetchone()[0] == "dormant"

    st.close()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print("三块/原话/时效：全部通过")


if __name__ == "__main__":
    main()
