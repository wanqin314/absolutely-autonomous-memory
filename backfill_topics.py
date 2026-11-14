"""给已有记忆补主题词（只写 topics，不改正文），然后重建索引和向量。

用法：
    python backfill_topics.py --dry-run 20
    python backfill_topics.py --apply
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DB = os.environ.get("AAM_DB") or os.path.join(
    os.path.abspath(os.path.join(HERE, "..", "..", "plugin_data",
                                 "astrbot_plugin_absolutely_autonomous_memory")), "memory.db")
KEY = os.environ.get("DEEPSEEK_KEY_FILE", "deepseek-key.txt")
API = "https://api.deepseek.com/v1/chat/completions"
BATCH = 10

try:  # 作为包加载（AstrBot）
    from .store import Store
    from . import retriever
except ImportError:  # 作为脚本运行（插件目录在 sys.path 上）
    from store import Store
    import retriever

SYSTEM = "你在给记忆起主题词。只输出 JSON。"
TPL = """下面是她的一条条记忆。请给每条起 2–3 个主题词，回答「这条记忆是关于什么的」。

{items}

规则：
- 至少 2 个必须是**别人可能用来问的词**（如「换客户端」「白饭减半」「生日」），2–6 个字；
- 最多 1 个可以用她自己的说法；
- 只能从原文里概括，不许联想、不许加入原文没有的概念；
- 禁用：情绪、聊天、日常、事情、主人、我、他、她、它。

只输出 JSON：{{"items":[{{"id":123,"topics":["换客户端","断连"]}}]}}"""

BAN = {"情绪", "聊天", "日常", "事情", "主人", "我", "他", "她", "它"}


def key() -> str:
    return (os.environ.get("DEEPSEEK_API_KEY") or open(KEY, encoding="utf-8-sig").read()).strip()


def ask(prompt: str) -> str:
    body = {"model": "deepseek-flash",
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            "stream": False, "response_format": {"type": "json_object"}}
    req = urllib.request.Request(API, data=json.dumps(body, ensure_ascii=False).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key()})
    with urllib.request.urlopen(req, timeout=120, context=ssl.create_default_context()) as r:
        return json.loads(r.read().decode())["choices"][0]["message"]["content"]


def clean(raw):
    out = []
    for x in (raw or [])[:4]:
        t = str(x).strip().strip("「」“”\"'")
        if 2 <= len(t) <= 6 and t not in BAN and t not in out:
            out.append(t)
    return out[:3]


def main():
    dry = "--dry-run" in sys.argv
    apply_ = "--apply" in sys.argv
    # 默认只补插件更换后的记忆；旧记忆不动（实测会挤掉旧记忆的精确排名）
    only_live = "--all" not in sys.argv
    limit = None
    for i, a in enumerate(sys.argv):
        if a in ("--dry-run", "--limit") and i + 1 < len(sys.argv) and sys.argv[i + 1].isdigit():
            limit = int(sys.argv[i + 1])

    s = Store(DB)
    rows = list(s.conn.execute(
        """SELECT t.id, v.id vid, v.text FROM trace t JOIN trace_version v ON v.id=t.head_version_id
           WHERE t.status='active' AND (v.topics IS NULL OR v.topics='' OR v.topics='[]')
             AND (? = 0 OR t.source = 'live')
           ORDER BY t.id""", (0 if only_live else 1,)))
    if limit:
        rows = rows[:limit]
    print(f"待补主题 {len(rows)} 条（库：{DB}）")
    if not rows:
        s.close()
        return

    filled = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        items = "\n".join(f"#{r['id']}｜{r['text'][:120]}" for r in chunk)
        try:
            raw = ask(TPL.format(items=items))
            data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
            got = {int(x["id"]): clean(x.get("topics")) for x in (data.get("items") or [])
                   if isinstance(x, dict) and str(x.get("id", "")).isdigit()}
        except Exception as e:  # noqa: BLE001
            print(f"  第 {i//BATCH+1} 批失败：{type(e).__name__}: {e}")
            continue

        for r in chunk:
            tp = got.get(int(r["id"])) or []
            if not tp:
                continue
            filled += 1
            if dry:
                print(f"  #{r['id']} {tp} | {r['text'][:34]}")
                continue
            s.conn.execute("UPDATE trace_version SET topics=? WHERE id=?",
                           (json.dumps(tp, ensure_ascii=False), r["vid"]))
            s.reindex_trace(int(r["id"]))
        if apply_:
            s.conn.commit()
        time.sleep(0.15)

    if apply_:
        s.conn.commit()
        # 重算向量（含主题）
        todo = [int(r["id"]) for r in s.conn.execute(
            """SELECT t.id FROM trace t JOIN trace_version v ON v.id=t.head_version_id
               WHERE t.status='active' AND v.topics IS NOT NULL AND v.topics!=''""")]
        print(f"重算向量 {len(todo)} 条…")
        for tid in todo:
            row = s.trace_row(tid)
            v = s.conn.execute("SELECT text, topics FROM trace_version WHERE id=?",
                               (row["head_version_id"],)).fetchone()
            vec = retriever.embed_sync(Store.index_text(v["text"], v["topics"]))
            if vec:
                s.conn.execute(
                    """INSERT OR REPLACE INTO trace_vec(trace_id, vec, dim, model, updated_at)
                       VALUES (?,?,?,?,datetime('now'))""",
                    (tid, retriever.pack(vec), len(vec), "bge-m3"))
        s.conn.commit()
    n = s.conn.execute("SELECT COUNT(*) FROM trace_version WHERE topics IS NOT NULL AND topics!=''").fetchone()[0]
    print(f"\n补好 {filled} 条 · 库里有主题的版本 {n} 个" + ("" if apply_ else "（预览）"))
    s.close()


if __name__ == "__main__":
    main()
