"""把超长观察拆成 2–3 段（按句边界，原文一字不改）。

用法：
    python split_long_observations.py --dry-run 5     # 先看切法
    python split_long_observations.py --apply         # 真拆（原条归档）
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DB = os.environ.get("AAM_DB") or os.path.join(
    os.path.abspath(os.path.join(HERE, "..", "..", "plugin_data",
                                 "astrbot_plugin_absolutely_autonomous_memory")), "memory.db")

try:  # 作为包加载（AstrBot）
    from .store import Store
    from . import retriever
except ImportError:  # 作为脚本运行（插件目录在 sys.path 上）
    from store import Store
    import retriever

MAX_PIECES = 3
MIN_LEN = 150


def group_sentences(parts: list[str], k: int) -> list[str]:
    """把句子按顺序均分成 k 组（保持原文顺序与全部文字）。"""
    n = len(parts)
    k = max(2, min(k, n))
    size = (n + k - 1) // k
    out = []
    for i in range(0, n, size):
        out.append("".join(parts[i:i + size]))
    return [p for p in out if p]


def main():
    dry = "--dry-run" in sys.argv
    limit = None
    for i, a in enumerate(sys.argv):
        if a in ("--dry-run", "--limit") and i + 1 < len(sys.argv) and sys.argv[i + 1].isdigit():
            limit = int(sys.argv[i + 1])
    apply_ = "--apply" in sys.argv

    s = Store(DB)
    try:
        rows = list(s.conn.execute(
            """SELECT t.id, v.text FROM trace t JOIN trace_version v ON v.id=t.head_version_id
               WHERE t.status='active' AND length(v.text) > ?
                 AND t.split_from IS NULL
               ORDER BY length(v.text) DESC""", (MIN_LEN,)))
        if limit:
            rows = rows[:limit]
        print(f"待拆 {len(rows)} 条（库：{DB}）")

        done = 0
        for r in rows:
            tid = int(r["id"])
            text = r["text"] or ""
            parts = s.split_sentences(text)
            if len(parts) < 3:
                continue
            k = min(MAX_PIECES, max(2, len(text) // 220 + 1))
            pieces = group_sentences(parts, k)
            if len(pieces) < 2 or any(len(p) < 30 for p in pieces):
                continue
            if dry:
                print(f"\n#{tid}（{len(text)} 字）→ {len(pieces)} 段")
                for i, p in enumerate(pieces):
                    print(f"   [{i+1}] ({len(p)} 字) {p[:60]}…")
                done += 1
                continue
            new_ids = s.split_trace(tid, pieces)
            if not new_ids:
                continue
            nvec = 0
            for nid in new_ids:
                row = s.trace_row(nid)
                head = s.conn.execute("SELECT text FROM trace_version WHERE id=?",
                                      (row["head_version_id"],)).fetchone()
                vec = retriever.embed_sync(head["text"]) if head else None
                if vec:
                    s.conn.execute(
                        """INSERT OR REPLACE INTO trace_vec(trace_id, vec, dim, model, updated_at)
                           VALUES (?,?,?,?,datetime('now'))""",
                        (nid, retriever.pack(vec), len(vec), "bge-m3"))
                    nvec += 1
            s.conn.commit()
            done += 1
            print(f"  #{tid} → {new_ids}（{len(pieces)} 段，补向量 {nvec}）")

        print(f"\n完成 {done} 条" + ("" if apply_ else "（预览，未写库）"))
    finally:
        s.close()


if __name__ == "__main__":
    main()
