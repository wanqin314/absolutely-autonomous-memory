"""给已有记忆补实体（只登记名字，不建图）。

用法：
    set AAM_DB=<副本.db>  python backfill_entities.py --dry-run 20
    python backfill_entities.py --apply

规则：每 10 条一次调用；只写 entity / mention 两张表，不动记忆本身。
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
KEY_FILE = os.environ.get("DEEPSEEK_KEY_FILE", "deepseek-key.txt")
API = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-flash"
BATCH = 10

SYSTEM = "你在给记忆标注实体的名字。只输出 JSON。"

TPL = """下面是她的几条记忆。请给每条列出**明确出现**的人、物、群、作品的名字。

{items}

规则：
- 只写名字本身（如「婉清」「记忆插件」「超级纯小子」），不带修饰、不带称谓；
- 不要写代词（他、她、它、这个、那件事）；
- 不要写形容词、情绪词、时间词；
- 一条最多 3 个；看不出来就给空数组，别猜。

只输出 JSON：{{"items":[{{"id":123,"names":["婉清"]}}]}}"""


def key() -> str:
    env = os.environ.get("DEEPSEEK_API_KEY")
    if env:
        return env.strip()
    return open(KEY_FILE, encoding="utf-8-sig").read().strip()


def ask(prompt: str) -> str:
    body = {"model": MODEL,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            "stream": False, "response_format": {"type": "json_object"}}
    req = urllib.request.Request(API, data=json.dumps(body, ensure_ascii=False).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + key()})
    with urllib.request.urlopen(req, timeout=120, context=ssl.create_default_context()) as r:
        return json.loads(r.read().decode())["choices"][0]["message"]["content"]


def main():
    dry = "--dry-run" in sys.argv
    limit = None
    for i, a in enumerate(sys.argv):
        if a in ("--dry-run", "--limit") and i + 1 < len(sys.argv) and sys.argv[i + 1].isdigit():
            limit = int(sys.argv[i + 1])
    apply = "--apply" in sys.argv

    try:  # 作为包加载（AstrBot）
        from .store import Store
    except ImportError:  # 作为脚本运行（插件目录在 sys.path 上）
        from store import Store
    s = Store(DB)
    rows = list(s.conn.execute(
        """SELECT t.id, v.text FROM trace t JOIN trace_version v ON v.id=t.head_version_id
           WHERE t.status='active'
             AND NOT EXISTS (SELECT 1 FROM mention m WHERE m.trace_id=t.id)
           ORDER BY t.id"""))
    if limit:
        rows = rows[:limit]
    print(f"待标 {len(rows)} 条，分 {-(-len(rows)//BATCH)} 批（库：{DB}）")
    if not rows:
        s.close()
        return

    names_total = 0
    labeled = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        items = "\n".join(f"#{r['id']}｜{r['text'][:110]}" for r in chunk)
        try:
            raw = ask(TPL.format(items=items))
            data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
            got = {int(x["id"]): [str(n).strip() for n in (x.get("names") or [])[:3]]
                   for x in (data.get("items") or [])
                   if isinstance(x, dict) and str(x.get("id", "")).isdigit()}
        except Exception as e:  # noqa: BLE001
            print(f"  第 {i//BATCH+1} 批失败：{type(e).__name__}: {e}")
            continue

        for r in chunk:
            names = [n for n in got.get(int(r["id"]), []) if 2 <= len(n) <= 24]
            if not names:
                continue
            labeled += 1
            names_total += len(names)
            if apply:
                for n in names:
                    eid = s.upsert_entity(n)
                    if eid:
                        s.add_mention(int(r["id"]), eid)
            if dry:
                print(f"  #{r['id']} → {names}  | {r['text'][:36]}")
        if apply:
            s.conn.commit()
        time.sleep(0.15)

    if apply:
        s.conn.commit()
    n_ent = s.conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0]
    n_men = s.conn.execute("SELECT COUNT(*) FROM mention").fetchone()[0]
    print(f"\n有名字的 {labeled} 条，共 {names_total} 个名字"
          + ("" if apply else "（预览，未写库）"))
    if apply:
        print(f"实体 {n_ent} 个 · 提及 {n_men} 条")
    s.close()


if __name__ == "__main__":
    main()
