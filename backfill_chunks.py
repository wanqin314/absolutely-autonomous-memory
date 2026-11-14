"""补块向量：把 trace_chunk 里 vec 为空的块算出来（本地 bge-m3，无 API 成本）。

用法：
    python backfill_chunks.py            # 全部补齐
    python backfill_chunks.py --dry-run
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


def main():
    dry = "--dry-run" in sys.argv
    s = Store(DB)
    try:
        # 1) 先给长文本建块（幂等：已建过的不重复）
        n_new = 0
        for r in s.conn.execute(
                """SELECT t.id, v.text FROM trace t JOIN trace_version v ON v.id=t.head_version_id
                   WHERE t.status='active' AND length(v.text) > 150
                     AND NOT EXISTS (SELECT 1 FROM trace_chunk c WHERE c.trace_id=t.id)"""):
            n_new += s.rebuild_chunks(int(r["id"]), r["text"] or "")
        print(f"新切块 {n_new} 块")

        rows = s.chunks_without_vec(10000) if not dry else s.chunks_without_vec(10)
        print(f"待补向量的块 {len(rows)} 个" + ("（预览）" if dry else ""))
        n = 0
        for r in rows:
            vec = retriever.embed_sync(r["text"])
            if not vec:
                continue
            if not dry:
                s.set_chunk_vec(int(r["id"]), retriever.pack(vec))
            n += 1
        print(f"补好 {n} 块")
        total = s.conn.execute("SELECT COUNT(*) FROM trace_chunk").fetchone()[0]
        ready = s.conn.execute("SELECT COUNT(*) FROM trace_chunk WHERE vec IS NOT NULL").fetchone()[0]
        print(f"块总数 {total} · 有向量 {ready}")
    finally:
        s.close()


if __name__ == "__main__":
    main()
