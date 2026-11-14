"""最小自检：建临时库 → 写一条 → 补向量 → 检索。

不碰生产库；没有嵌入服务时只跳过向量断言，不报失败。

    python smoke_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:  # 作为包加载（AstrBot）
    from .store import Store
    from . import retriever
except ImportError:  # 作为脚本运行（插件目录在 sys.path 上）
    from store import Store
    import retriever


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="aam_smoke_")
    db = os.path.join(tmp, "smoke.db")
    ok = True
    try:
        s = Store(db)
        tid = s.add_trace("明天九点叫我起床，别忘了", "promise",
                          importance=0.6, due_at="2026-11-13T09:00:00+08:00",
                          topics=["起床", "闹钟"])
        print(f"[1] 写入痕迹 #{tid}")
        if not tid:
            print("    ✗ 没有返回 id")
            return 1

        emb = retriever.embedding_status()
        vec_ok = retriever.embed_sync("ping") is not None
        print(f"[2] 向量服务 {emb['mode']} · {emb['model']} → {'可用' if vec_ok else '不可用（已降级）'}")
        n = retriever.ensure_vectors(s, [tid])
        vec_rows = s.conn.execute("SELECT COUNT(*) FROM trace_vec WHERE trace_id=?",
                                  (tid,)).fetchone()[0]
        print(f"    ensure_vectors 返回 {n} · trace_vec {vec_rows} 行")
        if vec_ok and (n != 1 or vec_rows != 1):
            print("    ✗ 补向量失败（报错见上方日志）")
            ok = False
        elif not vec_ok and vec_rows != 0:
            print("    ✗ 服务不可用却写了向量？")
            ok = False

        hits = retriever.search(s, "闹钟", top_k=3)
        print(f"[3] 检索「闹钟」→ {[h['id'] for h in hits]}")
        if tid not in [h["id"] for h in hits]:
            print("    ✗ 没检索到自己")
            ok = False

        dropped = s.conn.execute(
            "SELECT COUNT(*) FROM trace WHERE kind='not_a_kind'").fetchone()[0]
        print(f"[4] 非法类型未写入：{dropped} 条")
        if dropped:
            ok = False
        s.close()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n结果：", "通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
