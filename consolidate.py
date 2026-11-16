"""合并同类记忆（两阶段）。

阶段一（自动，写入时）：archivist.find_near_duplicate —— 相似度够高就直接归并。
阶段二（定期，需确认）：本脚本 —— 宽松聚类后让模型逐簇判断"是不是同一件事"，
只有判为"重复"的才合并；旧痕迹归档、不删除，合并出来的痕迹留反向指针。

用法（命令行）：
    python consolidate.py --dry-run     只出预览，不写库
    python consolidate.py --apply       真正合并
"""

from __future__ import annotations

import difflib
import json
import os
import ssl
import sys
import urllib.request

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:  # 作为包加载（AstrBot）
    from .store import Store
    from . import retriever
except ImportError:  # 作为脚本运行（插件目录在 sys.path 上）
    from store import Store
    import retriever

DB = os.environ.get("AAM_DB") or os.path.join(
    os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                                 "plugin_data", "astrbot_plugin_absolutely_autonomous_memory")), "memory.db")
KEY_FILE = os.environ.get("DEEPSEEK_KEY_FILE", "")
API_KEY_ENV = "DEEPSEEK_API_KEY"
API_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-flash"

CLUSTER_COS = 0.85
CLUSTER_RATIO = 0.45
MERGABLE_KINDS = ("fact", "observation")
PROTECTED_IMPORTANCE = 0.85
MAX_CLUSTERS = 8

JUDGE_SYSTEM = (
    "你在帮她整理记忆。给你几条她记下的事，判断它们是不是「同一件事的重复说法」。"
    "只输出 JSON。"
)

JUDGE_TEMPLATE = """下面是她记下的几条事（同一个类型）：

{items}

请判断：
1. 它们是同一件事的不同说法（重复），还是不同的事件/不同的事实？
2. 如果是重复，用一句话把它们合并成一条（第一人称，40 字以内，不漏关键信息）。

只输出 JSON：
{{"same": true/false, "merged": "合并后的一句话（same 为 false 时留空）", "why": "不超过 20 字的理由"}}

注意：
- 时间、地点、数量不同的，是不同的事，不是重复；
- **同一件事在不同天反复发生（例如每天叫他起床），是不同发生，不要合并**；只合并同一次发生/同一事实的不同说法；
- 同一个人的名字/身份/生日这类事实，说法不同也算重复；
- 只有同一个场景（scope）里的才放在一起判断，不要跨私聊/群聊。
"""


def _norm(t: str) -> str:
    return "".join(ch for ch in (t or "") if ch.isalnum())


def load_traces(store) -> list[dict]:
    rows = store.conn.execute(
        """SELECT t.id, t.kind, t.importance, t.tags, t.scope, v.text FROM trace t
           JOIN trace_version v ON v.id = t.head_version_id
           WHERE t.status='active' AND t.avoided=0 AND t.merged_into IS NULL
             AND t.kind IN ('fact','observation') AND t.importance < ?
           ORDER BY t.id""", (PROTECTED_IMPORTANCE,)).fetchall()
    out = []
    for r in rows:
        vr = store.conn.execute("SELECT vec FROM trace_vec WHERE trace_id=?", (r["id"],)).fetchone()
        if not vr:
            continue
        out.append({"id": int(r["id"]), "kind": r["kind"], "importance": float(r["importance"]),
                    "tags": json.loads(r["tags"]) if r["tags"] else [], "text": r["text"] or "",
                    "scope": r["scope"] or "", "vec": retriever.unpack(vr["vec"])})
    return out


def build_clusters(traces: list[dict]) -> list[list[int]]:
    if len(traces) < 2:
        return []
    M = np.array([t["vec"] for t in traces], dtype=np.float32)
    M /= np.linalg.norm(M, axis=1, keepdims=True) + 1e-9
    sim = M @ M.T
    parent = list(range(len(traces)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in range(len(traces)):
        for b in range(a + 1, len(traces)):
            if traces[a]["kind"] != traces[b]["kind"]:
                continue
            if traces[a]["scope"] != traces[b]["scope"]:
                continue
            if float(sim[a, b]) < CLUSTER_COS:
                continue
            if difflib.SequenceMatcher(None, _norm(traces[a]["text"]), _norm(traces[b]["text"])).ratio() < CLUSTER_RATIO:
                continue
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

    groups: dict[int, list[int]] = {}
    for i in range(len(traces)):
        groups.setdefault(find(i), []).append(i)
    idx_clusters = [g for g in groups.values() if len(g) >= 2]
    idx_clusters.sort(key=len, reverse=True)
    return [[traces[i]["id"] for i in g] for g in idx_clusters[:MAX_CLUSTERS]]


def _llm(prompt: str) -> str:
    key = (os.environ.get(API_KEY_ENV) or "").strip()
    if not key and KEY_FILE and os.path.exists(KEY_FILE):
        key = open(KEY_FILE, encoding="utf-8-sig").read().strip()
    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": JUDGE_SYSTEM},
                     {"role": "user", "content": prompt}],
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(API_URL, data=json.dumps(body, ensure_ascii=False).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=90, context=ctx) as r:
        d = json.loads(r.read().decode())
    return (d["choices"][0]["message"]["content"] or "").strip()


def judge(cluster_texts: list[str], llm=_llm) -> dict:
    items = "\n".join(f"{i+1}. {t}" for i, t in enumerate(cluster_texts))
    try:
        raw = llm(JUDGE_TEMPLATE.format(items=items))
        m = raw[raw.find("{"): raw.rfind("}") + 1]
        d = json.loads(m)
        return {"same": bool(d.get("same")), "merged": str(d.get("merged") or "").strip(),
                "why": str(d.get("why") or "")[:30]}
    except Exception as e:  # noqa: BLE001
        return {"same": False, "merged": "", "why": f"判定失败：{type(e).__name__}"}


def apply_merge(store, ids: list[int], merged_text: str) -> int:
    rows = [store.trace_row(i) for i in ids]
    rows = [r for r in rows if r]
    if len(rows) < 2:
        return 0
    kind = rows[0]["kind"]
    importance = max(float(r["importance"] or 0.5) for r in rows)
    tags = sorted({t for r in rows for t in (json.loads(r["tags"]) if r["tags"] else [])})[:4]
    if not (merged_text or "").strip():
        return 0
    new_id = store.add_trace(
        merged_text.strip(), kind, importance=importance,
        tags=tags + ["合并"], source="merge", reason="consolidation",
        scope=rows[0]["scope"] or "",
        episode_id=rows[0]["episode_id"],
        anchor_time=min((r["created_at"] or "") for r in rows),
    )
    for r in rows:
        store.archive(int(r["id"]), merged_into=new_id)
    store.add_note(new_id, "合并自 " + "、".join(f"#{int(r['id'])}" for r in rows), kind="evidence")
    return new_id


def run(store, apply: bool = False, llm=_llm) -> dict:
    traces = load_traces(store)
    clusters = build_clusters(traces)
    by_id = {t["id"]: t for t in traces}
    stats = {"clusters": len(clusters), "judged": 0, "same": 0, "merged": 0, "new_ids": [], "preview": []}
    for ids in clusters:
        texts = [by_id[i]["text"] for i in ids]
        verdict = judge(texts, llm)
        stats["judged"] += 1
        stats["preview"].append({"ids": ids, "texts": texts, "verdict": verdict})
        if not verdict["same"] or not verdict["merged"]:
            continue
        stats["same"] += 1
        if apply:
            new_id = apply_merge(store, ids, verdict["merged"])
            if new_id:
                stats["merged"] += len(ids)
                stats["new_ids"].append(new_id)
    return stats


def main():
    apply = "--apply" in sys.argv
    store = Store(DB)
    try:
        stats = run(store, apply=apply)
        print(f"聚类 {stats['clusters']} 个，判定 {stats['judged']} 个，判为重复 {stats['same']} 个"
              + (f"，已合并 {stats['merged']} 条 → 新痕迹 {stats['new_ids']}" if apply else "（预览模式，未写库）"))
        for p in stats["preview"]:
            v = p["verdict"]
            mark = "✔ 重复" if v["same"] else "✘ 不同"
            print(f"\n[{mark}] {p['ids']}  {v['why']}")
            for t in p["texts"]:
                print("   -", t[:60])
            if v["same"] and v["merged"]:
                print("   → 合并为：", v["merged"])
    finally:
        store.close()


if __name__ == "__main__":
    main()
