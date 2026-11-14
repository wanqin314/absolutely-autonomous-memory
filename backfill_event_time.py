"""批量补 event_time：每 10 条一组问一次模型，只补为空的。

用法：
    python backfill_event_time.py --dry-run 20    # 先看 20 条判得对不对
    python backfill_event_time.py --apply         # 全量补
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sqlite3
import sys
import time
import urllib.request

DB = os.environ.get("AAM_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "plugin_data", "astrbot_plugin_absolutely_autonomous_memory", "memory.db")
KEY_FILE = os.environ.get("DEEPSEEK_KEY_FILE", "deepseek-key.txt")
API = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-flash"
BATCH = 10

SYSTEM = "你在帮忙补记忆的时间标注。只输出 JSON。"

TPL = """下面是她的几条记忆。每条前面有「#编号」和「记下的时间」。
请判断：这条记忆描述的**事情本身**发生在什么时候？

{items}

规则：
- 原文里能看出具体时刻的（「今天下午三点」「明天」「上周三」「11月9号」），按「记下的时间」换算成绝对时间，ISO 格式带时区，如 2026-11-12T15:00:00+08:00；
- 只能看出日期、看不出时分的，只写日期（如 2026-11-07），不要编一个 00:00 的时刻；
- 看不出、或者这件事没有明确时间（比如「他喜欢猫」），event_time 填 null；
- **不许猜**，拿不准就 null。

只输出 JSON：{{"times":[{{"id":123,"event_time":"2026-11-12T15:00:00+08:00"}}]}}"""

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?([+-]\d{2}:\d{2})?)?$")


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
        if a == "--dry-run" and i + 1 < len(sys.argv) and sys.argv[i + 1].isdigit():
            limit = int(sys.argv[i + 1])
        if a == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])
    apply = "--apply" in sys.argv

    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = list(c.execute(
        """SELECT t.id, t.kind, t.created_at, v.anchor_time, v.text, v.id vid
           FROM trace t JOIN trace_version v ON v.id=t.head_version_id
           WHERE v.event_time IS NULL OR v.event_time=''
           ORDER BY t.id"""))
    if limit:
        rows = rows[:limit]
    print(f"待补 {len(rows)} 条，分 {-(-len(rows)//BATCH)} 批")
    if not rows:
        c.close()
        return

    updated = 0
    filled = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        items = "\n".join(
            f"#{r['id']}｜记下 {str(r['created_at'])[:16]}｜[{r['kind']}] {r['text'][:120]}"
            for r in chunk)
        try:
            raw = ask(TPL.format(items=items))
            data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
            times = {int(x["id"]): x.get("event_time") for x in (data.get("times") or [])
                     if isinstance(x, dict) and str(x.get("id", "")).isdigit()}
        except Exception as e:  # noqa: BLE001
            print(f"  第 {i//BATCH+1} 批失败：{type(e).__name__}: {e}")
            continue

        for r in chunk:
            et = times.get(int(r["id"]))
            if et and ISO.match(str(et)):
                filled += 1
                if apply:
                    c.execute("UPDATE trace_version SET event_time=? WHERE id=?", (et, r["vid"]))
                if dry:
                    print(f"  #{r['id']} 记 {str(r['created_at'])[:16]} → 事件 {et}  | {r['text'][:34]}")
            updated += 1
        if apply:
            c.commit()
        time.sleep(0.2)

    if apply:
        c.commit()
    print(f"\n处理 {updated} 条，判出事件时间 {filled} 条（{filled*100//max(1,updated)}%）"
          + ("" if apply else "（预览，未写库）"))
    c.close()


if __name__ == "__main__":
    main()
