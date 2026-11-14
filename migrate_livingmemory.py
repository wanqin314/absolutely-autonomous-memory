"""把 LivingMemory 的旧记忆导进新结构（M1，规则迁移，不调模型）。

映射关系：
- 每条 documents 记录 → 一个 episode（原文只有它的 summary，标 source=livingmemory）
- canonical_summary / text → 一条 observation 痕迹（v1）
- key_facts 的每一条 → 一条 fact 痕迹（它们本来就是原子事实）
- topics → tags
- metadata.importance → importance
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:  # 作为包加载（AstrBot）
    from .store import Store
except ImportError:  # 作为脚本运行（插件目录在 sys.path 上）
    from store import Store

SRC = os.environ.get("LM_DB") or "livingmemory.db"
DST = os.environ.get("AAM_DB") or "memory.db"


def to_iso(dt_text: str) -> str:
    try:
        d = datetime.fromisoformat(str(dt_text))
        return d.astimezone().isoformat(timespec="seconds")
    except Exception:
        return datetime.now().astimezone().isoformat(timespec="seconds")


def scope_of(session_id: str) -> str:
    if "GroupMessage" in (session_id or ""):
        gid = session_id.split("GroupMessage:")[-1].strip()
        return f"group:{gid}" if gid else "group:unknown"
    return "private"


def main():
    src = sqlite3.connect(SRC)
    src.row_factory = sqlite3.Row
    store = Store(DST)

    done = {r[0] for r in store.conn.execute("SELECT source_ref FROM episode WHERE source='livingmemory'")}
    rows = list(src.execute("SELECT * FROM documents ORDER BY created_at"))
    eps = facts = 0

    for row in rows:
        try:
            meta = json.loads(row["metadata"] or "{}")
        except Exception:
            meta = {}
        doc_id = row["doc_id"] or str(row["id"])
        if doc_id in done:
            continue
        session_id = meta.get("session_id", "")
        summary = (meta.get("canonical_summary") or row["text"] or "").strip()
        if " | " in summary:          # 旧版 text 有时是「摘要 | 事实1；事实2」
            summary = summary.split(" | ", 1)[0].strip()
        if not summary:
            continue
        importance = float(meta.get("importance") or 0.5)
        topics = meta.get("topics") or []
        created = to_iso(row["created_at"])
        scope = scope_of(session_id)

        episode_id = store.add_episode(
            scope, session_id=session_id, started_at=created, ended_at=created,
            participants=[], transcript=summary, source="livingmemory", source_ref=doc_id,
        )
        eps += 1

        store.add_trace(
            summary, "observation", importance=importance, episode_id=episode_id,
            anchor_quote=None, anchor_time=created, tags=topics, source="livingmemory",
            created_at=created,
        )

        for fact in (meta.get("key_facts") or [])[:6]:
            fact = (fact or "").strip()
            if not fact or len(fact) < 4:
                continue
            store.add_trace(
                fact, "fact", importance=importance, episode_id=episode_id,
                anchor_quote=fact, anchor_time=created, tags=topics, source="livingmemory",
                created_at=created,
            )
            facts += 1

    print(f"导入完成：episode {eps} 条，observation {eps} 条，fact {facts} 条")
    print("统计：", json.dumps(store.stats(), ensure_ascii=False))
    store.close()


if __name__ == "__main__":
    main()
