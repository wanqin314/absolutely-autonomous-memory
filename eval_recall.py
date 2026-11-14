"""检索评测：在数据库副本上跑，跑完删干净，绝不动生产库。

用法：
    python eval_recall.py            # 30 道题
    python eval_recall.py 50         # 50 道题

规则（按用户要求）：
- 只操作副本，生产库只读打开一次用于复制；
- 无论成功失败，finally 里删掉副本和临时文件；
- 跑完核对：生产库的修改时间没变、条数没变。
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PROD = os.environ.get("AAM_DB") or os.path.join(
    os.path.abspath(os.path.join(HERE, "..", "..", "plugin_data",
                                 "astrbot_plugin_absolutely_autonomous_memory")), "memory.db")

CJK = re.compile(r"[\u4e00-\u9fa5]{4,}")


def pick_phrase(text: str, n: int = 6) -> str:
    hits = CJK.findall(text or "")
    if not hits:
        return ""
    longest = max(hits, key=len)
    return longest[:n]


def fingerprint(path: str):
    st = os.stat(path)
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        n_trace = c.execute("SELECT COUNT(*) FROM trace").fetchone()[0]
        n_raw = c.execute("SELECT COUNT(*) FROM raw_message").fetchone()[0]
    except Exception:
        n_trace = n_raw = -1
    finally:
        c.close()
    return (st.st_mtime_ns, st.st_size, n_trace, n_raw)


def main():
    n_q = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 30
    before = fingerprint(PROD)

    tmpdir = tempfile.mkdtemp(prefix="aam_eval_")
    copy = os.path.join(tmpdir, "eval.db")
    try:
        # 1) 复制（生产库只读）
        src = sqlite3.connect(f"file:{PROD}?mode=ro", uri=True)
        dst = sqlite3.connect(copy)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()

        # 2) 从副本里出题：拿一条真实记忆的短语当问题
        c = sqlite3.connect(copy)
        c.row_factory = sqlite3.Row
        rows = list(c.execute(
            """SELECT t.id, v.text FROM trace t JOIN trace_version v ON v.id=t.head_version_id
               WHERE t.status='active' AND t.avoided=0"""))
        c.close()

        import random
        random.seed(20261111)
        random.shuffle(rows)

        from store import Store
        import retriever
        store = Store(copy)
        hits_at = {1: 0, 3: 0, 5: 0}
        rr = 0.0
        used = 0
        fails = []
        for r in rows:
            q = pick_phrase(r["text"])
            if len(q) < 4:
                continue
            res = retriever.search(store, q, top_k=5)
            ids = [h["id"] for h in res]
            used += 1
            for k in hits_at:
                if int(r["id"]) in ids[:k]:
                    hits_at[k] += 1
            if int(r["id"]) in ids:
                rr += 1.0 / (ids.index(int(r["id"])) + 1)
            else:
                fails.append((r["id"], q, ids))
            if used >= n_q:
                break
        store.close()

        if used:
            print(f"题目 {used} 道（用真实记忆里的中文短语当问题，跑在副本上）")
            print(f"  Recall@1 {hits_at[1]/used:.0%}   @3 {hits_at[3]/used:.0%}   @5 {hits_at[5]/used:.0%}")
            print(f"  MRR      {rr/used:.3f}")
            if fails:
                print(f"\n没召回 {len(fails)} 道，前 5 个：")
                for fid, q, ids in fails[:5]:
                    print(f"  #{fid} 问「{q}」→ 返回 {ids}")
    finally:
        # 3) 清理：副本、临时目录，一个不留
        shutil.rmtree(tmpdir, ignore_errors=True)
        left = [p for p in os.listdir(os.path.dirname(PROD))
                if p.startswith("eval.db") or p.startswith("aam_eval_")]
        after = fingerprint(PROD)
        print("\n清理：副本目录已删除" + (f"，残留 {left}" if left else "，无残留"))
        print("生产库未改动:", "是" if before == after else f"否！（{before} → {after}）")


if __name__ == "__main__":
    main()
