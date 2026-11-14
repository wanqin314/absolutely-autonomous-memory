"""把记忆导成一张自包含的 HTML：双击就能看，不需要起任何服务。

用法：
    python snapshot.py                 # 写到桌面
    python snapshot.py --out D:\\x.html
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("AAM_DB") or os.path.join(
    os.path.abspath(os.path.join(HERE, "..", "..", "plugin_data",
                                 "astrbot_plugin_absolutely_autonomous_memory")), "memory.db")


def collect():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    one = lambda sql: c.execute(sql).fetchone()[0]

    stats = {
        "total": one("SELECT COUNT(*) FROM trace"),
        "active": one("SELECT COUNT(*) FROM trace WHERE status='active'"),
        "archived": one("SELECT COUNT(*) FROM trace WHERE status='archived'"),
        "versions": one("SELECT COUNT(*) FROM trace_version"),
        "episodes": one("SELECT COUNT(*) FROM episode"),
    }
    kinds = {r["kind"]: r["n"] for r in c.execute(
        "SELECT kind, COUNT(*) n FROM trace WHERE status='active' GROUP BY kind")}

    rows = list(c.execute(
        """SELECT t.id, t.kind, t.importance, t.created_at, t.activation_count,
                  t.versions_count, COALESCE(t.state,'open') state, v.text
           FROM trace t JOIN trace_version v ON v.id=t.head_version_id
           WHERE t.status='active' ORDER BY t.created_at DESC, t.id DESC"""))
    items = [dict(r) for r in rows]

    opens = [dict(r) for r in c.execute(
        """SELECT t.id, t.kind, v.text FROM trace t JOIN trace_version v ON v.id=t.head_version_id
           WHERE t.kind IN ('promise','unresolved') AND t.status='active'
             AND COALESCE(t.state,'open')='open' ORDER BY t.id DESC""")]

    # 可检索性分布
    buckets = {"高": 0, "中": 0, "低": 0, "几乎忘": 0}
    now = datetime.now().astimezone()
    for r in c.execute("SELECT stability, last_active_at, created_at FROM trace WHERE status='active'"):
        try:
            anchor = datetime.fromisoformat(r["last_active_at"] or r["created_at"])
            dt = max(0.0, (now - anchor).total_seconds() / 86400)
            R = math.exp(-dt / max(0.1, float(r["stability"] or 7)))
        except Exception:
            R = 1.0
        buckets["高" if R > 0.8 else "中" if R > 0.5 else "低" if R > 0.2 else "几乎忘"] += 1
    c.close()
    return {"stats": stats, "kinds": kinds, "items": items, "opens": opens,
            "retention": buckets, "generated": time.strftime("%Y-%m-%d %H:%M")}


HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>她的记忆 · 快照</title>
<style>
:root{--bg:#0f1419;--panel:#161c24;--line:#232c38;--fg:#e6edf3;--dim:#8b98a5;--acc:#4aa3ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 "Microsoft YaHei",system-ui,sans-serif}
header{padding:16px 20px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:baseline}
h1{font-size:16px;margin:0}
.meta{color:var(--dim);font-size:12px}
.wrap{display:flex;height:calc(100vh - 60px)}
.side{width:46%;border-right:1px solid var(--line);overflow:auto;padding:10px 14px}
.detail{flex:1;overflow:auto;padding:16px 18px}
.tabs{display:flex;gap:6px;margin-bottom:10px}
.tabs button{background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:5px 12px;cursor:pointer}
.tabs button.on{border-color:var(--acc);color:var(--acc)}
.cards{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.card b{font-size:20px;display:block}
.card span{color:var(--dim);font-size:12px}
.day{color:var(--dim);font-size:12px;margin:12px 0 6px}
.it{padding:7px 10px;border-radius:8px;cursor:pointer;border:1px solid transparent}
.it:hover{background:var(--panel);border-color:var(--line)}
.kind{font-size:11px;padding:1px 6px;border-radius:5px;background:#1d2733;color:var(--acc);margin-right:6px}
.sub{color:var(--dim);font-size:12px}
.txt{margin-top:2px}
h4{font-size:12.5px;color:var(--dim);margin:14px 0 6px}
.bar{height:8px;background:#1c242e;border-radius:4px;overflow:hidden;margin:3px 0 6px}
.bar i{display:block;height:100%;background:var(--acc)}
.row{display:flex;justify-content:space-between;font-size:12.5px}
</style></head><body>
<header><h1>她的记忆 · 快照</h1><span class="meta" id="meta"></span></header>
<div class="wrap">
  <div class="side">
    <div class="tabs">
      <button id="t-tl" class="on" onclick="tab('tl')">时间线</button>
      <button id="t-st" onclick="tab('st')">统计</button>
      <button id="t-op" onclick="tab('op')">未了</button>
    </div>
    <div id="list"></div>
  </div>
  <div class="detail" id="detail"><div class="sub">左边点一条看详情</div></div>
</div>
<script>
const D = __DATA__;
const KINDN = {fact:'事实',observation:'观察',speculation:'推测',self:'自我',unresolved:'未了',promise:'约定'};
let cur = 'tl';
function esc(s){return String(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function tab(t){cur=t;['tl','st','op'].forEach(x=>document.getElementById('t-'+x).className=(x===t?'on':''));render()}
function render(){
  const L=document.getElementById('list');
  if(cur==='tl'){
    let h='',day=null;
    for(const it of D.items){
      const d=(it.created_at||'').slice(0,10);
      if(d!==day){h+=`<div class="day">${d}</div>`;day=d}
      const st=it.state&&it.state!=='open'?`<span class="sub">〔${it.state==='void'?'作废':'了结'}〕</span>`:'';
      h+=`<div class="it" onclick="show(${it.id})"><span class="kind">${KINDN[it.kind]||it.kind}</span>
          <span class="sub">${(it.created_at||'').slice(11,16)} · #${it.id} · ${(it.importance||0).toFixed(2)}</span>${st}
          <div class="txt">${esc((it.text||'').slice(0,90))}</div></div>`;
    }
    L.innerHTML=h;
  } else if(cur==='st'){
    const s=D.stats, ks=Object.entries(D.kinds).sort((a,b)=>b[1]-a[1]);
    const kmax=Math.max(1,...ks.map(x=>x[1]));
    const rm=Math.max(1,...Object.values(D.retention));
    L.innerHTML=`<div class="cards">
      <div class="card"><b>${s.active}</b><span>活跃</span></div>
      <div class="card"><b>${s.archived}</b><span>归档</span></div>
      <div class="card"><b>${s.versions}</b><span>版本</span></div>
      <div class="card"><b>${s.episodes}</b><span>经历</span></div></div>
      <h4>类型</h4>${ks.map(([k,n])=>`<div class="row"><span>${KINDN[k]||k}</span><span>${n}</span></div><div class="bar"><i style="width:${Math.round(n*100/kmax)}%"></i></div>`).join('')}
      <h4>还多想得起来</h4>${Object.entries(D.retention).map(([k,n])=>`<div class="row"><span>${k}</span><span>${n}</span></div><div class="bar"><i style="width:${Math.round(n*100/rm)}%"></i></div>`).join('')}`;
  } else {
    L.innerHTML = D.opens.length
      ? D.opens.map(o=>`<div class="it"><span class="kind">${KINDN[o.kind]||o.kind}</span><span class="sub">#${o.id}</span><div class="txt">${esc(o.text)}</div></div>`).join('')
      : '<div class="sub">没有未了的事</div>';
  }
}
function show(id){
  const it=D.items.find(x=>x.id===id);
  if(!it) return;
  document.getElementById('detail').innerHTML =
    `<h3 style="margin:0 0 4px">#${it.id} <span class="kind">${KINDN[it.kind]||it.kind}</span></h3>
     <div class="sub">${it.created_at} · 重要度 ${(it.importance||0).toFixed(2)} · 版本 ${it.versions_count} · 想起 ${it.activation_count} 次</div>
     <p style="margin-top:14px">${esc(it.text)}</p>`;
}
document.getElementById('meta').textContent = `生成于 ${D.generated} · 共 ${D.stats.total} 条`;
render();
</script></body></html>"""


def main():
    data = collect()
    html = HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False))
    out = None
    for i, a in enumerate(sys.argv):
        if a == "--out" and i + 1 < len(sys.argv):
            out = sys.argv[i + 1]
    if not out:
        desk = os.path.join(os.path.expanduser("~"), "Desktop")
        name = f"记忆快照-{time.strftime('%Y%m%d-%H%M')}.html"
        out = os.path.join(desk if os.path.isdir(desk) else os.getcwd(), name)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"已生成：{out}（{os.path.getsize(out)/1024:.0f} KB，{data['stats']['active']} 条活跃）")


if __name__ == "__main__":
    main()
