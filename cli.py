"""命令行翻记忆（M1）：dir / show / history / search / stats

用法：
    python cli.py dir
    python cli.py show 12
    python cli.py history 12
    python cli.py search 猫
    python cli.py stats
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:  # 作为包加载（AstrBot）
    from .store import Store
except ImportError:  # 作为脚本运行（插件目录在 sys.path 上）
    from store import Store

DB = os.environ.get("MEMORY_DB") or os.environ.get("AAM_DB") or os.path.join(
    os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                                 "plugin_data", "astrbot_plugin_absolutely_autonomous_memory")), "memory.db")


def cmd_dir(store: Store):
    d = store.directory()
    print(f"\n共 {d['total']} 条痕迹  |  " + "  ".join(f"{k}:{v}" for k, v in d["by_kind"].items()))
    print("\n【标签】")
    print("  " + " ｜ ".join(f"{g['tag']}({g['count']})" for g in d["tags"]))
    if d["unresolved"]:
        print("\n【未了的事】")
        for x in d["unresolved"]:
            print(f"  #{x['id']}  {x['text']}")
    if d["most_recalled"]:
        print("\n【想起过很多次的】")
        for x in d["most_recalled"]:
            print(f"  #{x['id']}  想起 {x['activations']} 次 · {x['days_span']} 天  {x['text']}")


def cmd_show(store: Store, tid: int):
    t = store.get_trace(tid)
    if not t:
        print("没有这条")
        return
    print(f"\n#{t['id']}  [{t['kind']}]  重要度 {t['importance']:.2f}  稳定度 {t['stability']} 天  "
          f"此刻可检索性 R={t['r_now']}  想起 {t['activations']} 次")
    print(f"标签：{t['tags']}")
    print(f"写入：{t['created_at']}   版本：v{t['version_no']}")
    print(f"\n{t['text']}\n")
    if t["anchor_quote"]:
        print(f"锚点原话：{t['anchor_quote']}")


def cmd_history(store: Store, tid: int):
    t = store.get_trace(tid, history=True)
    if not t:
        print("没有这条")
        return
    print(f"\n#{t['id']} 的版本链（当前 v{t['version_no']}）")
    for v in t["chain"]:
        mark = "← 当前" if v["version_no"] == t["version_no"] else ""
        print(f"\n  v{v['version_no']}  {v['created_at']}  reason={v['reason']} "
              f"caused_by={v['caused_by_type']} [{v['change_class']}] {mark}")
        print(f"    {v['text']}")
    if t.get("notes"):
        print("\n  旁注：")
        for n in t["notes"]:
            print(f"    [{n['kind']}] {n['text']}  ({n['created_at']})")


def cmd_search(store: Store, q: str):
    hits = store.search(q)
    if not hits:
        print("没搜到")
        return
    for h in hits:
        print(f"  #{h['id']}  [{h['kind']}]  {h['text'][:80]}")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    store = Store(DB)
    cmd, rest = args[0], args[1:]
    if cmd == "dir":
        cmd_dir(store)
    elif cmd == "show" and rest:
        cmd_show(store, int(rest[0]))
    elif cmd == "history" and rest:
        cmd_history(store, int(rest[0]))
    elif cmd == "search" and rest:
        cmd_search(store, rest[0])
    elif cmd == "touch" and rest:
        res = store.activate(int(rest[0]), cue_type="manual", query="cli")
        t = store.get_trace(int(rest[0]))
        print(f"激活一次：R {res['r_before']} → 稳定度 {res['s_after']} 天；"
              f"现在 R={t['r_now']}，天花板 {round(store.ceiling(store.trace_row(int(rest[0]))))} 天")
    elif cmd == "stats":
        print(json.dumps(store.stats(), ensure_ascii=False, indent=1))
    else:
        print(__doc__)
    store.close()


if __name__ == "__main__":
    main()
