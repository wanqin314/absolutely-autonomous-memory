"""Absolutely autonomous memory —— 本地记忆面板。

用法：
    python webui.py [--port 21600] [--open]

只监听 127.0.0.1。只给用户看/管，不参与她的运行。
"""

from __future__ import annotations

import argparse
import json
import os
import webbrowser

import sys

# pythonw 没有控制台，stdout/stderr 为 None，uvicorn 一写日志就崩，先兜住
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

try:  # 作为包加载（AstrBot）
    from .store import Store
    from . import retriever
except ImportError:  # 作为脚本运行（插件目录在 sys.path 上）
    from store import Store
    import retriever

DB = os.environ.get("AAM_DB") or os.path.join(
    os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                                 "plugin_data", "astrbot_plugin_absolutely_autonomous_memory")), "memory.db")

app = FastAPI(title="Absolutely autonomous memory")


def _configure_embedding_from_config() -> None:
    """面板单独运行时，也从插件配置里读向量设置。"""
    try:
        import json as _json
        db_dir = os.path.dirname(os.path.abspath(DB))
        data_dir = os.path.dirname(os.path.dirname(db_dir))
        pid = os.path.basename(db_dir)
        cfg_path = os.path.join(data_dir, "config", pid + "_config.json")
        if not os.path.exists(cfg_path):
            return
        cfg = _json.loads(open(cfg_path, encoding="utf-8-sig").read())
        retriever.configure(
            url=str(cfg.get("embedding_url") or ""),
            model=str(cfg.get("embedding_model") or ""),
            mode=str(cfg.get("embedding_mode") or ""),
            key=str(cfg.get("embedding_api_key") or ""),
        )
    except Exception:
        pass


_configure_embedding_from_config()


def db() -> Store:
    return Store(DB)


def _scope_label(scope: str) -> str:
    sc = (scope or "").strip()
    if sc == "global":
        return "全局"
    if sc.startswith("group:"):
        return "群聊 " + sc.split(":", 1)[1]
    if sc.startswith("private:"):
        return "私聊 · " + sc.split(":", 1)[1]
    if sc == "private":
        return "私聊"
    return sc or "—"


@app.get("/api/series")
def api_series():
    s = db()
    try:
        rows = s.series_list()
        for r in rows:
            r["scope_label"] = _scope_label(r.get("scope"))
        return rows
    finally:
        s.close()


@app.get("/api/resident")
def api_resident(all: int = 0):
    """她自己放的常驻区；默认只看还在用的。"""
    s = db()
    try:
        sql = "SELECT * FROM resident_note"
        if not all:
            sql += " WHERE status='active'"
        sql += " ORDER BY id"
        rows = [dict(r) for r in s.conn.execute(sql).fetchall()]
        for r in rows:
            r["scope_label"] = _scope_label(r.get("scope"))
        active = [r for r in rows if r.get("status") == "active"]
        return {
            "items": rows,
            "n": len(active),
            "chars": sum(len(r.get("text") or "") for r in active),
            "max_items": 12,
            "max_chars": 1200,
        }
    except Exception:
        return {"items": [], "n": 0, "chars": 0, "max_items": 12, "max_chars": 1200}
    finally:
        s.close()


@app.get("/api/looks")
def api_looks(limit: int = 100):
    """群聊 <look> 观察记录，只读本地 JSONL。"""
    import json as _json
    from collections import deque

    base = os.path.dirname(os.path.dirname(DB))
    path = os.path.join(base, "astrbot_plugin_group_extras", "look.jsonl")
    old = os.path.join(base, "astrbot_plugin_group_look", "look.jsonl")
    if not os.path.exists(path) and os.path.exists(old):
        path = old
    if not os.path.exists(path):
        return []
    n = max(1, min(500, int(limit or 100)))
    try:
        with open(path, encoding="utf-8-sig", errors="ignore") as f:
            lines = deque(f, maxlen=n)
        out = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(_json.loads(line))
            except Exception:
                continue
        return out
    except Exception:
        return []


@app.get("/api/recall_queries")
def api_recall_queries(limit: int = 100):
    """最近的检索调用记录；包括 0 命中。"""
    s = db()
    try:
        n = max(1, min(500, int(limit or 100)))
        rows = s.conn.execute(
            """SELECT id, session_id, query, hits, degraded, sort, trigger, scope,
                      kind, person, results, since, until, created_at
               FROM recall_query_log ORDER BY id DESC LIMIT ?""", (n,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["results"] = json.loads(d.get("results") or "[]")
            except Exception:
                d["results"] = []
            out.append(d)
        return out
    except Exception:
        return []
    finally:
        s.close()


@app.get("/api/two_stage")
def api_two_stage(limit: int = 100):
    """两段式第一段的预想记录：用户消息、判断、查词、想法、材料。"""
    s = db()
    try:
        return s.recent_two_stage_logs(max(1, min(300, int(limit or 100))))
    except Exception:
        return []
    finally:
        s.close()


@app.get("/api/self")
def api_self(days: int = 30):
    """最近的自我的观察与自我思考；只读。"""
    from datetime import date, timedelta
    try:
        n = max(1, min(365, int(days or 30)))
        since = (date.today() - timedelta(days=n - 1)).isoformat()
        s = db()
        try:
            traces = s.conn.execute(
                """SELECT t.id, t.kind, v.text, COALESCE(v.anchor_time, t.created_at) AS at,
                          e.session_id
                   FROM trace t
                   JOIN trace_version v ON v.id=t.head_version_id
                   LEFT JOIN episode e ON e.id=t.episode_id
                   WHERE t.kind='self' AND t.status='active' AND t.avoided=0
                     AND date(COALESCE(v.anchor_time, t.created_at))>=date(?)
                   ORDER BY at DESC LIMIT 200""", (since,)).fetchall()
            thoughts = s.conn.execute(
                """SELECT id, session_id, tick_no, text, status, created_at
                   FROM self_thought WHERE date(created_at)>=date(?)
                   ORDER BY id DESC LIMIT 200""", (since,)).fetchall()
            return {"since": since, "traces": [dict(r) for r in traces],
                    "thoughts": [dict(r) for r in thoughts]}
        finally:
            s.close()
    except Exception:
        return {"since": "", "traces": [], "thoughts": []}


@app.get("/api/self_thoughts")
def api_self_thoughts(days: int = 30):
    """思考过程：不参与检索，也不等于她选择留下的记忆。"""
    from datetime import date, timedelta
    try:
        n = max(1, min(365, int(days or 30)))
        since = (date.today() - timedelta(days=n - 1)).isoformat()
        s = db()
        try:
            rows = s.conn.execute(
                """SELECT id, session_id, tick_no, text, status, created_at
                   FROM self_thought WHERE date(created_at)>=date(?)
                   ORDER BY id DESC LIMIT 300""", (since,)).fetchall()
            return {"since": since, "thoughts": [dict(r) for r in rows]}
        finally:
            s.close()
    except Exception:
        return {"since": "", "thoughts": []}


@app.get("/api/series/{sid}")
def api_series_one(sid: int):
    s = db()
    try:
        data = s.series_show(int(sid))
        if not data:
            return JSONResponse({"error": "not found"}, status_code=404)
        data["scope_label"] = _scope_label(data.get("scope"))
        # 面板按对话时间排；event_time 只最后兜底
        data["members"] = sorted(
            data.get("members") or [],
            key=lambda m: str(m.get("anchor_time") or m.get("created_at")
                              or m.get("event_time") or ""))
        return data
    finally:
        s.close()


@app.get("/api/stats")
def api_stats():
    s = db()
    try:
        d = s.stats()
        rows = s.conn.execute("SELECT AVG(importance) a, AVG(stability) b FROM trace").fetchone()
        d["avg_importance"] = round(rows["a"] or 0, 2)
        d["avg_stability"] = round(rows["b"] or 0, 1)
        return d
    finally:
        s.close()


@app.get("/api/directory")
def api_directory():
    s = db()
    try:
        return s.directory()
    finally:
        s.close()


@app.get("/api/unresolved")
def api_unresolved():
    s = db()
    try:
        rows = s.conn.execute(
            """SELECT t.id, t.kind, v.text, t.created_at, t.due_at, t.due_hint,
                      COALESCE(t.state,'open') state
               FROM trace t
               JOIN trace_version v ON v.id=t.head_version_id
               WHERE t.kind IN ('unresolved','promise') AND t.status='active'
                 AND COALESCE(t.state,'open') IN ('open','missed','stale','old')
               ORDER BY t.id DESC""").fetchall()
        return [dict(r) for r in rows]
    finally:
        s.close()


@app.get("/api/search")
def api_search(q: str = "", limit: int = 20, all: int = 0):
    s = db()
    try:
        # 面板和她的检索用同一套规则；all=1 时把归档的也翻出来
        return retriever.search(s, q, limit, include_archived=bool(all)) if q.strip() else []
    finally:
        s.close()


@app.get("/api/raw")
def api_raw(limit: int = 50, q: str = "", session: str = ""):
    s = db()
    try:
        if q.strip():
            return s.raw_search(q, min(200, int(limit)))
        return s.raw_recent(min(200, int(limit)), session)
    finally:
        s.close()


@app.get("/api/recent")
def api_recent(limit: int = 30):
    s = db()
    try:
        rows = s.conn.execute(
            """SELECT t.id, t.kind, t.importance, t.stability, t.activation_count,
                      t.status, t.created_at, v.text
               FROM trace t JOIN trace_version v ON v.id=t.head_version_id
               ORDER BY t.id DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        s.close()


@app.get("/api/trace/{tid}")
def api_trace(tid: int):
    s = db()
    try:
        t = s.get_trace(tid, history=True)
        if not t:
            return JSONResponse({"error": "not found"}, status_code=404)
        row = s.trace_row(tid)
        ep = s.conn.execute("SELECT transcript, started_at FROM episode WHERE id=?",
                            (row["episode_id"],)).fetchone() if row["episode_id"] else None
        t["transcript"] = (ep["transcript"] or "")[:1500] if ep else ""
        if t.get("anchor_quote"):
            try:
                if not s.anchor_quote_ok(tid, t["anchor_quote"]):
                    t["anchor_quote"] = ""
                    t["anchor_quote_hidden"] = True
            except Exception:
                pass
        t["created_at"] = row["created_at"]
        t["state"] = row["state"] if "state" in row.keys() else "open"
        t["kind"] = row["kind"]
        t["due_at"] = row["due_at"] if "due_at" in row.keys() else None
        try:
            t["series"] = [
                {"id": int(r["id"]), "name": r["name"] or "",
                 "scope": r["scope"] or ""}
                for r in s.conn.execute(
                    """SELECT s.id, s.name, s.scope FROM series_member m
                       JOIN memory_series s ON s.id=m.series_id
                       WHERE m.trace_id=? AND m.status='active' AND s.status='active'
                       ORDER BY s.id""", (tid,))
            ]
        except Exception:
            t["series"] = []
        _sc = row["scope"] if "scope" in row.keys() else ""
        if (_sc or "").startswith("group:"):
            t["scope_label"] = "群聊 " + _sc.split(":", 1)[1]
        elif _sc == "private":
            t["scope_label"] = "私聊"
        else:
            t["scope_label"] = ""
        try:
            hv = s.conn.execute("SELECT topics FROM trace_version WHERE id=?",
                                (row["head_version_id"],)).fetchone()
            t["topics"] = json.loads(hv["topics"]) if hv and hv["topics"] else []
        except Exception:
            t["topics"] = []
        return t
    finally:
        s.close()


@app.post("/api/trace/{tid}/note")
def api_note(tid: int, kind: str = "annotation", text: str = ""):
    s = db()
    try:
        s.add_note(tid, text, kind=kind)
        return {"ok": True}
    finally:
        s.close()


@app.post("/api/trace/{tid}/settle")
def api_settle(tid: int, state: str = "settled", note: str = ""):
    s = db()
    try:
        s.settle(tid, state, note)
        return {"ok": True}
    finally:
        s.close()


@app.post("/api/trace/{tid}/avoid")
def api_avoid(tid: int, on: bool = True):
    s = db()
    try:
        s.conn.execute("UPDATE trace SET avoided=?, status=? WHERE id=?",
                       (1 if on else 0, "avoided" if on else "active", tid))
        s.conn.commit()
        return {"ok": True}
    finally:
        s.close()


@app.post("/api/trace/{tid}/importance")
def api_importance(tid: int, value: float = 0.5):
    s = db()
    try:
        s.conn.execute("UPDATE trace SET importance=? WHERE id=?", (max(0.0, min(1.0, value)), tid))
        s.conn.commit()
        return {"ok": True}
    finally:
        s.close()


@app.post("/api/trace/{tid}/edit")
def api_edit(tid: int, text: str = ""):
    s = db()
    try:
        if text.strip():
            s.add_version(tid, text.strip(), reason="correction", caused_by_type="correction",
                          change_class="minor")
            # 正文变了，立刻补上新向量（否则这条会暂时只能靠关键词召回）
            try:
                retriever.ensure_vectors(s, [tid])
            except Exception:
                pass
        return {"ok": True}
    finally:
        s.close()


@app.post("/api/trace/{tid}/fade")
def api_fade(tid: int):
    s = db()
    try:
        s.conn.execute("UPDATE trace SET status='faded' WHERE id=?", (tid,))
        s.conn.commit()
        return {"ok": True}
    finally:
        s.close()


HTML = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Absolutely autonomous memory</title>
<style>
:root{--bg:#0f1216;--panel:#171c22;--line:#242c36;--fg:#d6dee8;--dim:#7d8b9c;--acc:#5fb3f6;--warn:#e0a45c;--ok:#5cc98d}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 "Microsoft YaHei",system-ui,sans-serif}
header{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
h1{font-size:16px;margin:0;font-weight:600;letter-spacing:.3px}
.meta{color:var(--dim);font-size:12.5px}
main{display:grid;grid-template-columns:minmax(320px,1fr) minmax(360px,1.1fr);gap:0;height:calc(100vh - 56px)}
.left{border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
.bar{display:flex;gap:8px;padding:10px 14px;border-bottom:1px solid var(--line)}
input,button,select{background:var(--panel);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:7px 10px;font:inherit;outline:none}
input{flex:1}
button{cursor:pointer}
button:hover{border-color:var(--acc);color:var(--acc)}
.list{overflow:auto;flex:1}
.item{padding:9px 14px;border-bottom:1px solid #1c232b;cursor:pointer}
.item:hover{background:#151b21}
.item.active{background:#1a2430;border-left:2px solid var(--acc)}
.kind{font-size:11px;padding:1px 6px;border-radius:5px;margin-right:6px;background:#1e2833;color:var(--acc)}
.k-fact{color:#8fd0ff}.k-observation{color:#9fe3b1}.k-speculation{color:#e0a45c}
.k-self{color:#d8a6ff}.k-unresolved{color:#ffb4a2}.k-promise{color:#ffd479}
.txt{color:var(--fg);font-size:13.5px}
.small{color:var(--dim);font-size:12px}
.right{overflow:auto;padding:16px 20px;min-height:0}
.detail h2{font-size:15px;margin:0 0 8px}
.row{display:flex;gap:14px;flex-wrap:wrap;color:var(--dim);font-size:12.5px;margin-bottom:10px}
.box{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:12px}
.box h3{font-size:12.5px;margin:0 0 8px;color:var(--dim);font-weight:600;letter-spacing:.5px}
.chain .v{border-left:2px solid var(--line);padding:4px 0 8px 12px;margin-bottom:4px}
.chain .v:last-child{border-color:var(--acc)}
.tag{display:inline-block;background:#1c2530;border-radius:6px;padding:1px 7px;margin:2px 4px 2px 0;font-size:12px;color:var(--dim)}
.ops{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.ops button{font-size:12.5px;padding:5px 10px}
textarea{width:100%;min-height:60px;background:var(--panel);border:1px solid var(--line);border-radius:8px;color:var(--fg);padding:8px;font:inherit;resize:vertical}
.tabs{display:flex;gap:6px;padding:8px 14px;border-bottom:1px solid var(--line)}
.tabs button{font-size:12.5px;padding:4px 10px}
.tabs button.on{color:var(--acc);border-color:var(--acc)}
.empty{color:var(--dim);padding:24px;text-align:center}
.ok{color:var(--ok)}

.ov{padding:12px 14px;overflow:auto}
.ov .cards{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px}
.ov .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.ov .card b{font-size:20px;display:block;line-height:1.2}
.ov .card span{color:var(--dim);font-size:12px}
.ov h4{font-size:12.5px;color:var(--dim);margin:12px 0 6px;font-weight:600;letter-spacing:.4px}
.ov .brow{height:8px;background:#1c242e;border-radius:4px;overflow:hidden;margin:3px 0 6px}
.ov .brow i{display:block;height:100%;background:var(--acc)}
.ov .row{display:flex;justify-content:space-between;font-size:12.5px}
.ov .li{font-size:12.5px;padding:3px 0;color:var(--fg)}
</style></head>
<body>
<header>
  <h1>Absolutely autonomous memory</h1>
  <span class="meta" id="stats">加载中…</span>
</header>
<main>
  <section class="left">
    <div class="bar">
      <input id="q" placeholder="搜她记得的事…">
      <label class="small" style="display:flex;align-items:center;gap:4px;white-space:nowrap;cursor:pointer">
        <input type="checkbox" id="withArchived" style="width:auto" title="把归档的也翻出来">含归档</label>
      <button onclick="doSearch()">搜</button>
      <button onclick="loadRecent()">最近</button>
    </div>
    <div class="tabs">
      <button id="tab-recent" class="on" onclick="loadRecent()">最近</button>
      <button id="tab-unres" onclick="loadUnresolved()">未了的事</button>
      <button id="tab-tags" onclick="loadTags()">按主题</button>
      <button id="tab-series" onclick="loadSeries()">系列</button>
      <button id="tab-resident" onclick="loadResident()">常驻区</button>
      <button id="tab-looks" onclick="loadLooks()">先看</button>
      <button id="tab-recall" onclick="loadRecallQueries()">检索</button>
      <button id="tab-pre" onclick="loadTwoStage()">预想</button>
      <button id="tab-self" onclick="loadSelf()">自我</button>
      <button id="tab-thoughts" onclick="loadThoughts()">思考</button>
      <button id="tab-ov" onclick="loadOverview()">总览</button>
      <button id="tab-st" onclick="loadStats()">统计</button>
      <button id="tab-raw" onclick="loadRaw()">原话</button>
    </div>
    <div class="list" id="list"><div class="empty">选一条看详情</div></div>
  </section>
  <section class="right" id="detail"><div class="empty">左边点一条</div></section>
</main>
<script>
let cur=null;
const KIND={fact:'事实',observation:'观察',speculation:'推测',self:'自我',unresolved:'未了',promise:'约定',agreement:'说好的'};
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function j(u,o){const r=await fetch(u,o);return r.json()}
async function stats(){const d=await j('/api/stats');
  document.getElementById('stats').textContent =
    `${d.traces} 条痕迹 · ${d.versions} 个版本 · ${d.episodes} 段经历 · 平均重要度 ${d.avg_importance} · 平均稳定度 ${d.avg_stability} 天`;}
function item(r){
  const k=KIND[r.kind]||r.kind;
  const extra=r.activation_count?` · 想起 ${r.activation_count} 次`:'';
  return `<div class="item" data-id="${r.id}" onclick="openTrace(${r.id},this)">
    <div><span class="kind k-${r.kind}">${k}</span><span class="small">#${r.id} · 重要度 ${(r.importance??0).toFixed(2)}${extra}</span></div>
    <div class="txt">${esc((r.text||'').slice(0,90))}</div></div>`;
}
let kindFilter='';
function kindBar(){
  const ks=[['','全部'],['fact','事实'],['observation','观察'],['self','自我'],
            ['speculation','推测'],['unresolved','未了'],['promise','约定'],['agreement','说好的']];
  return `<div class="bar" style="padding:6px 10px;flex-wrap:wrap">`+
    ks.map(([v,t])=>`<button class="${kindFilter===v?'on':''}" style="padding:2px 8px;font-size:12px"
      onclick="kindFilter='${v}';loadRecent()">${t}</button>`).join('')+`</div>`;
}
async function loadRecent(){setTab('tab-recent');const d=await j('/api/recent?limit=120');
  const rows=kindFilter?d.filter(r=>r.kind===kindFilter):d;
  document.getElementById('list').innerHTML = kindBar() +
    (rows.length?rows.slice(0,60).map(item).join(''):'<div class="empty">这一类还没有记忆</div>');}
async function loadUnresolved(){setTab('tab-unres');
  const d=await j('/api/unresolved');
  const hintOf=r=>{
    if(r.due_hint) return r.due_hint;
    const m=(r.text||'').match(/(今早|明早|早上|上午|中午|下午|傍晚|晚上|今晚|睡前|饭后|周末|明天|后天|夜里)/);
    return m?m[1]:'';
  };
  const exact=d.filter(r=>r.due_at);
  const vague=d.filter(r=>!r.due_at && hintOf(r));
  const untimed=d.filter(r=>!r.due_at && !hintOf(r));
  const head=(t,n)=>`<h4 style="margin:10px 12px 4px">${t}（${n}）</h4>`;
  const row=r=>{
    const hint=!r.due_at?hintOf(r):'';
    const due=r.due_at?`<span class="small"> · ${String(r.due_at).slice(5,16).replace('T',' ')}</span>`
      :(hint?`<span class="small"> · ${esc(hint)}</span>`:'');
    const miss=r.state==='missed'?` <span class="small" style="color:var(--warn)">过点未做</span>`:'';
    const st=r.state==='old'?'<span class="small" style="color:var(--warn)">旧事</span>':
             r.state==='stale'?'<span class="small">沉默</span>':'';
    return `<div class="item" data-id="${r.id}" onclick="openTrace(${r.id},this)">
      <div><span class="kind k-${r.kind}">${KIND[r.kind]||r.kind}</span>
      <span class="small">#${r.id}${due}</span>${miss} ${st}</div>
      <div class="txt">${esc((r.text||'').slice(0,90))}</div></div>`;};
  const active=r=>!r.state||r.state==='open'||r.state==='missed';
  let html='';
  const exactA=exact.filter(active), vagueA=vague.filter(active), untimedA=untimed.filter(active);
  const stale=d.filter(r=>r.state==='stale'), old=d.filter(r=>r.state==='old');
  if(exactA.length) html += head('还在办 · 有确切时间',exactA.length)+exactA.map(row).join('');
  if(vagueA.length) html += head('还在办 · 只有笼统时间',vagueA.length)+vagueA.map(row).join('');
  if(untimedA.length) html += head('还在办 · 没时间',untimedA.length)+untimedA.map(row).join('');
  if(stale.length) html += head('沉默',stale.length)+stale.map(row).join('');
  if(old.length) html += head('旧事',old.length)+old.map(row).join('');
  document.getElementById('list').innerHTML = html||'<div class="empty">没有未了的事</div>';}
async function loadRaw(){
  setTab('tab-raw');
  const q=document.getElementById('q').value.trim();
  const d=await j('/api/raw?limit=80'+(q?('&q='+encodeURIComponent(q)):''));
  document.getElementById('list').innerHTML =
    `<div class="ov"><div class="row" style="margin-bottom:6px"><span>原话（只追加，不参与检索）</span>
      <span class="small">${d.length} 条</span></div>`+
    (d.length?d.map(r=>`<div class="li">${String(r.created_at).slice(5,16).replace('T',' ')}
      <span class="small">${r.role==='user'?'你':'她'}</span> ${esc((r.text||'').slice(0,120))}</div>`).join('')
     :'<div class="li" style="color:var(--dim)">还没有原话</div>')+'</div>';
}async function loadTags(){setTab('tab-tags');const d=await j('/api/directory');
  const html=(d.tags||[]).map(t=>`<div class="item" onclick="doSearch('${esc(t.tag)}')">
    <div class="txt">${esc(t.tag)}</div><div class="small">${t.count} 条</div></div>`).join('');
  document.getElementById('list').innerHTML = html||'<div class="empty">没有标签</div>';}

function scopeLabelJS(sc){
  sc=sc||'';
  if(sc==='global')return '全局';
  if(sc.indexOf('group:')===0)return '群聊 '+sc.slice(6);
  if(sc.indexOf('private:')===0)return '私聊 · '+sc.slice(8);
  if(sc==='private')return '私聊';
  return sc||'—';
}
const fmtWhen=s=>String(s||'').slice(0,16).replace('T',' ')||'—';
async function loadSeries(){
  setTab('tab-series');
  const d=await j('/api/series');
  document.getElementById('list').innerHTML = d.length ? d.map(s=>`
    <div class="item" onclick="openSeries(${s.id},this)">
      <div><span class="kind">系列</span><span class="small">#${s.id} · ${scopeLabelJS(s.scope)} · ${s.n||0} 条</span></div>
      <div class="txt">${esc(s.name)}</div>
    </div>`).join('') : '<div class="empty">还没有系列</div>';
  document.getElementById('detail').innerHTML='<div class="empty">点一个系列看里面的发生</div>';
}
async function openSeries(sid,el){
  document.querySelectorAll('.item').forEach(x=>x.classList.remove('active'));
  el?.classList.add('active');
  const s=await j('/api/series/'+sid);
  const rows=(s.members||[]).map(m=>`
    <div class="item" data-id="${m.id}" onclick="openTrace(${m.id},this)">
      <div><span class="kind k-${m.kind}">${KIND[m.kind]||m.kind}</span>
        <span class="small">${fmtWhen(m.anchor_time||m.created_at||m.event_time)} · #${m.id}</span></div>
      <div class="txt">${esc((m.text||'').slice(0,100))}</div>
    </div>`).join('');
  document.getElementById('detail').innerHTML=`<div class="detail">
    <h2>系列 #${s.id}「${esc(s.name)}」</h2>
    <div class="row"><span>${scopeLabelJS(s.scope)}</span><span>${(s.members||[]).length} 条发生</span>
      <span class="small">按对话时间排</span></div>
    <div class="box"><h3>里面的记忆</h3>${rows||'<div class="small">空</div>'}</div>
  </div>`;
}
async function loadResident(showAll=false){
  setTab('tab-resident');
  const d=await j('/api/resident'+(showAll?'?all=1':''));
  const rows=d.items||[];
  const head=`<div class="bar" style="padding:8px 12px;align-items:center">
    <span class="small">常驻区 ${d.n||0}/${d.max_items||12} 条 · ${d.chars||0}/${d.max_chars||1200} 字</span>
    <button style="margin-left:auto;padding:2px 8px;font-size:12px"
      onclick="loadResident(${showAll?'false':'true'})">${showAll?'只看在用的':'连已撤的一起看'}</button>
  </div>`;
  const html=rows.map(r=>`
    <div class="item" style="${r.status==='retired'?'opacity:.55':''}">
      <div><span class="kind">${r.status==='retired'?'已撤':'常驻'}</span>
        <span class="small">#${r.id} · ${scopeLabelJS(r.scope)} · ${fmtWhen(r.created_at)}</span></div>
      <div class="txt" style="white-space:pre-wrap">${esc(r.text)}</div>
    </div>`).join('');
  document.getElementById('list').innerHTML = head + (html||'<div class="empty">常驻区还是空的</div>');
  document.getElementById('detail').innerHTML='<div class="empty">她自己放进去的东西，只读</div>';
}
async function loadLooks(){
  setTab('tab-looks');
  const d=await j('/api/looks?limit=100');
  const rows=Array.isArray(d)?d:[];
  const head=`<div class="bar" style="padding:8px 12px;align-items:center">
    <span class="small">先看记录 ${rows.length} 条 · 只读本地 look.jsonl</span>
  </div>`;
  const html=rows.map(r=>{
    const silent=(r.reply||'').includes('<SILENT>');
    const lookText=(r.look||'').replaceAll('<look>','').replaceAll('</look>','').trim();
    return `
    <div class="item">
      <div><span class="kind">先看</span>
        <span class="small">${fmtWhen(r.ts)} · ${esc(r.sender_name||r.sender_id||'')} · #${esc(r.message_id||'')}</span>
        ${silent?'<span class="small" style="color:var(--warn);margin-left:6px">选择不接</span>':''}</div>
      <div class="txt" style="white-space:pre-wrap">${esc(lookText)}</div>
      <div class="small" style="margin-top:4px">回复：${silent?'（<SILENT>）':esc((r.reply||'').slice(0,160))}</div>
    </div>`;
  }).join('');
  document.getElementById('list').innerHTML = head + (html||'<div class="empty">还没有先看记录</div>');
  document.getElementById('detail').innerHTML='<div class="empty">她先看的内容，只读</div>';
}
async function loadRecallQueries(){
  setTab('tab-recall');
  const d=await j('/api/recall_queries?limit=200');
  const rows=Array.isArray(d)?d:[];
  const scene=s=>{
    s=s||'';
    if(s.includes('GroupMessage')) return '群 '+s.split(':').pop();
    if(s.includes('FriendMessage')) return '私聊 '+s.split(':').pop();
    return s;
  };
  const head=`<div class="bar" style="padding:8px 12px;align-items:center">
    <span class="small">检索记录 ${rows.length} 条 · 含系统预取与 0 命中</span>
  </div>`;
  const html=rows.map(r=>{
    const hits=Number(r.hits||0);
    const src=r.trigger==='two_stage'?'系统预取':(r.trigger==='tool'?'她调用':'旧记录');
    const filters=[r.sort,r.scope,r.kind,r.person].filter(Boolean).map(esc).join(' · ');
    const results=Array.isArray(r.results)?r.results:[];
    const detail=results.length?`<details style="margin-top:6px">
      <summary class="small">明细 ${results.length} 条</summary>
      ${results.map(x=>`<div class="small" style="margin:4px 0 4px 8px">
        #${esc(x.id)} · ${esc(x.kind||'')} · ${esc(x.score)} · ${esc((x.text||'').slice(0,160))}</div>`).join('')}
    </details>`:'<div class="small" style="margin-top:4px;opacity:.65">旧记录没有明细</div>';
    return `
    <div class="item">
      <div><span class="kind">检索</span>
        <span class="small">${fmtWhen(r.created_at)} · ${esc(scene(r.session_id))}</span>
        <span class="small" style="margin-left:6px">${src}</span>
        ${r.degraded?'<span class="small" style="color:var(--warn);margin-left:6px">向量降级</span>':''}
        <span class="small" style="margin-left:6px">${hits} 命中</span></div>
      <div class="txt" style="white-space:pre-wrap">${esc((r.query||'（空 query）').slice(0,300))}</div>
      <div class="small" style="margin-top:4px">${filters||'relevance'}${r.since||r.until?` · 时间 ${esc(r.since||'…')} → ${esc(r.until||'…')}`:''}</div>
      ${detail}
    </div>`;
  }).join('');
  document.getElementById('list').innerHTML = head + (html||'<div class="empty">还没有检索记录</div>');
  document.getElementById('detail').innerHTML='<div class="empty">她调用和系统预取的检索都记在这里，包括没查到的，只读</div>';
}
async function loadTwoStage(){
  setTab('tab-pre');
  const d=await j('/api/two_stage?limit=200');
  const rows=Array.isArray(d)?d:[];
  const scene=s=>{
    s=s||'';
    if(s.includes('GroupMessage')) return '群 '+s.split(':').pop();
    if(s.includes('FriendMessage')) return '私聊 '+s.split(':').pop();
    return s||'';
  };
  const head=`<div class="bar" style="padding:8px 12px;align-items:center">
    <span class="small">最近 ${rows.length} 次第一段预想 · 只读</span>
  </div>`;
  const html=rows.map(r=>{
    const mat=String(r.materials||'');
    return `
    <div class="item">
      <div><span class="kind">预想</span>
        <span class="small">${fmtWhen(r.created_at)} · ${esc(scene(r.session_id))}</span>
        <span class="small" style="margin-left:6px">${esc(r.decision||'—')}</span>
        <span class="small" style="margin-left:6px">${mat?'有材料':'没材料'}</span></div>
      <div class="small" style="margin-top:4px">用户：${esc((r.user_text||'').slice(0,240))}</div>
      <div class="txt" style="white-space:pre-wrap;margin-top:4px">${esc((r.thought||r.raw||'（没写想法）').slice(0,800))}</div>
      <div class="small" style="margin-top:4px">查：${esc(r.query||'（没查）')}</div>
      ${mat?`<details style="margin-top:6px"><summary class="small">材料</summary><div class="small" style="white-space:pre-wrap;margin-top:4px">${esc(mat.slice(0,1200))}</div></details>`:''}
    </div>`;
  }).join('');
  document.getElementById('list').innerHTML = head + (html||'<div class="empty">还没有预想记录</div>');
  document.getElementById('detail').innerHTML='<div class="empty">她第一段看到什么、怎么想、有没有查，只读</div>';
}
async function loadSelf(){
  setTab('tab-self');
  const d=await j('/api/self?days=30');
  const traces=Array.isArray(d.traces)?d.traces:[];
  const head=`<div class="bar" style="padding:8px 12px;align-items:center">
    <span class="small">最近 ${esc(d.since||'30 天')} 以来 · 她选择留下的自我记忆 ${traces.length} 条</span>
  </div>`;
  const tr=traces.map(x=>`
    <div class="item">
      <div><span class="kind">自我记忆</span>
        <span class="small">${fmtWhen(x.at)} · #${esc(x.id)}</span></div>
      <div class="txt" style="white-space:pre-wrap">${esc((x.text||'').slice(0,600))}</div>
    </div>`).join('');
  document.getElementById('list').innerHTML = head + (tr || '<div class="empty">她还没有选择留下自我记忆</div>');
  document.getElementById('detail').innerHTML='<div class="empty">只有她自己写“记住：”留下的，才算自我记忆</div>';
}
async function loadThoughts(){
  setTab('tab-thoughts');
  const d=await j('/api/self_thoughts?days=30');
  const rows=Array.isArray(d.thoughts)?d.thoughts:[];
  const head=`<div class="bar" style="padding:8px 12px;align-items:center">
    <span class="small">最近 ${esc(d.since||'30 天')} 的思考过程 ${rows.length} 条 · 未选择留下，不参与检索</span>
  </div>`;
  const html=rows.map(x=>`
    <div class="item">
      <div><span class="kind">思考过程</span>
        <span class="small">${fmtWhen(x.created_at)} · 第 ${esc(x.tick_no)} 次 · ${esc(x.status||'')}</span></div>
      <div class="txt" style="white-space:pre-wrap">${esc((x.text||'').slice(0,800))}</div>
    </div>`).join('');
  document.getElementById('list').innerHTML = head + (html || '<div class="empty">还没有思考过程</div>');
  document.getElementById('detail').innerHTML='<div class="empty">这里只是过程记录；她写“记住：”留下的，才会进「自我」</div>';
}
function setTab(id){document.querySelectorAll('.tabs button').forEach(b=>b.classList.remove('on'));
  document.getElementById(id)?.classList.add('on');}
async function doSearch(q){
  const query=q||document.getElementById('q').value.trim();
  if(!query)return;
  document.getElementById('q').value=query;
  const _all=(document.getElementById('withArchived')?.checked)?1:0;
    const d=await j('/api/search?q='+encodeURIComponent(query)+'&all='+_all);
  document.getElementById('list').innerHTML = d.length?d.map(item).join(''):'<div class="empty">没搜到</div>';
}


async function loadStats(){
  setTab('tab-st');
  const d = await j('/api/overview');
  const KINDN = {fact:'事实',observation:'观察',speculation:'推测',self:'自我',unresolved:'未了',promise:'约定',agreement:'说好的'};
  const kinds = Object.entries(d.by_kind).sort((a,b)=>b[1]-a[1]);
  const kmax = Math.max(1, ...kinds.map(x=>x[1]));
  const ret = d.retention, rmax = Math.max(1, ...Object.values(ret));
  const pct = (n,m)=>Math.round(n*100/m)+'%';
  document.getElementById('list').innerHTML = `<div class="ov">
    <div class="cards">
      <div class="card"><b>${d.active}</b><span>活跃痕迹</span></div>
      <div class="card"><b>${d.archived}</b><span>已归档</span></div>
      <div class="card"><b>${d.versions}</b><span>版本数</span></div>
      <div class="card"><b>${d.episodes}</b><span>经历段数</span></div>
      <div class="card"><b>${d.by_state.open||0}</b><span>在办的未了/约定</span></div>
      <div class="card"><b>${d.size_kb}</b><span>库大小 KB</span></div>
    </div>
    <h4>类型（活跃 ${d.active} 条）</h4>
    ${kinds.map(([k,n])=>`<div class="row"><span>${KINDN[k]||k}</span><span>${n}</span></div>
      <div class="brow"><i style="width:${pct(n,kmax)}"></i></div>`).join('')}
    <h4>还多想得起来</h4>
    ${Object.entries(ret).map(([k,n])=>`<div class="row"><span>${k}</span><span>${n}</span></div>
      <div class="brow"><i style="width:${pct(n,rmax)}"></i></div>`).join('')}
    <h4>还没了结的</h4>
    ${(d.open_items||[]).map(x=>`<div class="li">#${x.id} ${String(x.text).slice(0,40)}</div>`).join('') || '<div class="li" style="color:var(--dim)">没有</div>'}
    <h4>最久没被想起</h4>
    ${(d.forgotten||[]).map(x=>`<div class="li">#${x.id} · ${x.days} 天 · ${String(x.text).slice(0,32)}</div>`).join('')}
  </div>`;
  document.getElementById('detail').innerHTML = '<div class="empty">左边是统计，点条目看详情</div>';
}

let tlOffset = 0;
async function loadOverview(more){
  setTab('tab-ov');
  if (!more) tlOffset = 0;
  const d = await j(`/api/timeline?limit=80&offset=${tlOffset}`);
  const KINDN = {fact:'事实',observation:'观察',speculation:'推测',self:'自我',unresolved:'未了',promise:'约定',agreement:'说好的'};
  const items = d.items || [];
  let html = more ? document.getElementById('list').innerHTML : '';
  let lastDay = null;
  if (!more) {
    html = `<div class="ov"><div class="row" style="margin-bottom:8px">
      <span>共 ${d.total} 条，按时间倒序</span><span class="small">点一条看详情</span></div>`;
  }
  for (const it of items) {
    const day = (it.created_at || '').slice(0, 10);
    const hm = (it.created_at || '').slice(11, 16);
    if (day !== lastDay) {
      html += `<h4 style="margin-top:10px">${day}</h4>`;
      lastDay = day;
    }
    const st = it.state && it.state !== 'open'
      ? ` <span class="small" style="color:${it.state === 'void' ? 'var(--warn)' : 'var(--ok)'}">${it.state === 'void' ? '作废' : '了结'}</span>` : '';
    html += `<div class="item" data-id="${it.id}" onclick="openTrace(${it.id},this)">
      <div><span class="kind k-${it.kind}">${KINDN[it.kind] || it.kind}</span>
        <span class="small">${hm} · #${it.id} · 重要度 ${(it.importance || 0).toFixed(2)}</span>${st}</div>
      <div class="txt">${esc(String(it.text || '').slice(0, 80))}</div></div>`;
  }
  if (items.length === 80) {
    html += `<div style="padding:10px 14px"><button onclick="tlOffset+=80;loadOverview(true)">加载更早的</button></div>`;
  }
  html += '</div>';
  document.getElementById('list').innerHTML = html;
  document.getElementById('detail').innerHTML = '<div class="empty">左边点一条</div>';
}
async function openTrace(id,el){
  document.querySelectorAll('.item').forEach(x=>x.classList.remove('active'));
  el?.classList.add('active');
  const t=await j('/api/trace/'+id);
  cur=t;
  const _all = t.chain || [];
  const _folded = _all.filter(v=>v.folded);
  const _show = window.showFolded ? _all : _all.filter(v=>!v.folded);
  const _foldNote = (!window.showFolded && _folded.length)
    ? `<div class="small" style="margin-bottom:6px">已折叠 ${_folded.length} 个旧版本 ·
         <a href="#" onclick="window.showFolded=1;openTrace(${t.id});return false" style="color:var(--acc)">展开</a></div>`
    : (window.showFolded && _folded.length
        ? `<div class="small" style="margin-bottom:6px">已展开全部 ·
             <a href="#" onclick="window.showFolded=0;openTrace(${t.id});return false" style="color:var(--acc)">收起</a></div>`
        : "");
  const chain=_show.map(v=>`<div class="v">
     <div class="small">v${v.version_no} · ${(v.created_at||'').slice(0,16)} · ${v.reason}${v.folded?' · 已折叠':''}${v.version_no===t.version_no?' · <span class="ok">当前</span>':''}</div>
     <div class="txt">${esc(v.text)}</div></div>`).join('');
  const notes=(t.notes||[]).filter(n=>!['her_words','quote'].includes(n.kind)).map(n=>`<div class="small">[${n.kind}] ${esc(n.text)}</div>`).join('')||'<div class="small">暂无</div>';
  const stateMap={open:'在办',settled:'已了结',void:'已作废',missed:'过点未做',stale:'久了没提',old:'很久没提',dormant:'休眠'};
  const stateTxt=stateMap[t.state||'open']||t.state;
  const stateColor=(t.state==='settled')?'var(--ok)':((t.state&&t.state!=='open')?'var(--warn)':'var(--dim)');
  document.getElementById('detail').innerHTML=`<div class="detail">
    <h2>#${t.id} <span class="kind k-${t.kind}">${KIND[t.kind]||t.kind}</span></h2>
    <div class="row"><span>重要度 ${t.importance.toFixed(2)}</span><span>稳定度 ${t.stability} 天</span>
      <span>此刻 R=${t.r_now}</span><span>想起 ${t.activations} 次</span>
      <span>版本 ${t.version_no}</span><span>场合 ${t.scope_label||'—'}</span>
      <span style="color:${stateColor}">状态 ${stateTxt}</span>${t.avoided?'<span style="color:var(--warn)">回避中</span>':''}</div>
    ${(t.series&&t.series.length)?`<div class="small" style="margin:-4px 0 10px">系列：${t.series.map(s=>`<a href="#" onclick="openSeries(${s.id});return false" style="color:var(--acc)">「${esc(s.name)}」</a>`).join(' · ')}</div>`:''}
    <div class="box"><h3>当前这一版</h3><div class="txt">${esc(t.text)}</div>
      <div style="margin-top:8px">${(t.tags||[]).map(x=>`<span class="tag">${esc(x)}</span>`).join('')}</div>
      ${(t.topics&&t.topics.length)?`<div class="small" style="margin-top:6px">主题：${t.topics.map(esc).join(' · ')}</div>`:''}
      ${t.due_at?`<div class="small" style="margin-top:6px">约定的时间：${String(t.due_at).slice(0,16).replace('T',' ')}</div>`:''}
      ${t.felt?`<div class="small" style="margin-top:6px">当时感觉：${esc(t.felt)}</div>`:''}
      ${t.reaction?`<div class="small" style="margin-top:6px">我的反应：${esc(t.reaction)}</div>`:''}
      ${t.inference?`<div class="small" style="margin-top:6px">推断${typeof t.inference_certainty==='number'?'（'+t.inference_certainty.toFixed(1)+'）':''}：${esc(t.inference)}</div>`:''}
      ${(()=>{const w=(t.notes||[]).filter(n=>n.kind==='her_words').pop();return w?`<div class="small" style="margin-top:6px">她自己的话：${esc(w.text)}</div>`:''})()}
      ${(()=>{const q=(t.notes||[]).filter(n=>n.kind==='quote').pop();return q?`<div class="small" style="margin-top:6px">她留的原话：${esc(q.text)}</div>`:''})()}
      ${t.anchor_quote?`<div class="small" style="margin-top:8px">当时的原话（提炼时留的）：${esc(t.anchor_quote)}</div>`:''}</div>
    <div class="box"><h3>版本链（往回翻）</h3>${_foldNote}<div class="chain">${chain||'<div class="small">只有一版</div>'}</div></div>
    <div class="box"><h3>旁注</h3>${notes}</div>
    <div class="box"><h3>操作</h3>
      <div class="small">改重要度</div>
      <div class="ops"><input id="imp" type="number" min="0" max="1" step="0.05" value="${t.importance.toFixed(2)}" style="width:90px">
        <button onclick="setImp()">保存</button>
        <button onclick="avoid(${!t.avoided})">${t.avoided?'取消回避':'标记回避'}</button>
        <button onclick="fade()">淡化</button>
        <button onclick="settle('settled')">标为了结</button>
        <button onclick="settle('void')">标为作废</button></div>
      <div class="small" style="margin-top:10px">写旁注</div>
      <textarea id="note" placeholder="补一句看法、证据或存疑…"></textarea>
      <div class="ops"><button onclick="addNote()">加旁注</button></div>
      <div class="small" style="margin-top:10px">改写（会追加一版，旧版保留）</div>
      <textarea id="edit" placeholder="新的说法…"></textarea>
      <div class="ops"><button onclick="doEdit()">追加一版</button></div>
    </div>
    ${t.transcript?`<div class="box"><h3>当时那段对话</h3><div class="small" style="white-space:pre-wrap">${esc(t.transcript)}</div></div>`:''}
  </div>`;
}
async function post(u){await j(u,{method:'POST'});openTrace(cur.id);stats();}
async function setImp(){const v=document.getElementById('imp').value;post(`/api/trace/${cur.id}/importance?value=${v}`)}
async function avoid(on){post(`/api/trace/${cur.id}/avoid?on=${on}`)}
async function fade(){post(`/api/trace/${cur.id}/fade`)}
async function settle(st){post(`/api/trace/${cur.id}/settle?state=${st}`)}
async function addNote(){const t=document.getElementById('note').value.trim();if(!t)return;
  await j(`/api/trace/${cur.id}/note?text=${encodeURIComponent(t)}`,{method:'POST'});openTrace(cur.id);}
async function doEdit(){const t=document.getElementById('edit').value.trim();if(!t)return;
  await j(`/api/trace/${cur.id}/edit?text=${encodeURIComponent(t)}`,{method:'POST'});openTrace(cur.id);stats();}
stats();loadRecent();
// 被工作台嵌进来时，藏掉自己的页头，避免重复标题
if (new URLSearchParams(location.search).get('embed') === '1') {
  document.querySelector('header').style.display = 'none';
  document.querySelector('main').style.height = 'calc(100vh - 8px)';
}
// 支持 ?id=123 直接打开某一条
const _q = new URLSearchParams(location.search); if (_q.get('tab') === 'ov') { setTimeout(() => loadOverview(), 200); } if (_q.get('tab') === 'st') { setTimeout(() => loadStats(), 200); } if (_q.get('tab') === 'series') { setTimeout(() => loadSeries(), 200); } if (_q.get('tab') === 'resident') { setTimeout(() => loadResident(), 200); } if (_q.get('tab') === 'looks') { setTimeout(() => loadLooks(), 200); } if (_q.get('tab') === 'recall') { setTimeout(() => loadRecallQueries(), 200); } if (_q.get('tab') === 'pre') { setTimeout(() => loadTwoStage(), 200); } if (_q.get('tab') === 'self') { setTimeout(() => loadSelf(), 200); } if (_q.get('tab') === 'thoughts') { setTimeout(() => loadThoughts(), 200); } const deepId = _q.get('id');
if (deepId) setTimeout(() => openTrace(parseInt(deepId)), 300);
</script></body></html>"""



@app.get("/api/overview")
def api_overview():
    s = db()
    try:
        import math
        from datetime import datetime

        one = lambda sql: s.conn.execute(sql).fetchone()[0]
        out = {
            "total": one("SELECT COUNT(*) FROM trace"),
            "active": one("SELECT COUNT(*) FROM trace WHERE status='active'"),
            "archived": one("SELECT COUNT(*) FROM trace WHERE status='archived'"),
            "avoided": one("SELECT COUNT(*) FROM trace WHERE avoided=1"),
            "versions": one("SELECT COUNT(*) FROM trace_version"),
            "episodes": one("SELECT COUNT(*) FROM episode"),
            "size_kb": round(os.path.getsize(DB) / 1024),
        }
        out["by_kind"] = {r["kind"]: r["n"] for r in s.conn.execute(
            "SELECT kind, COUNT(*) n FROM trace WHERE status='active' GROUP BY kind")}
        out["by_state"] = {r["k"]: r["n"] for r in s.conn.execute(
            """SELECT COALESCE(state,'open') k, COUNT(*) n FROM trace
               WHERE kind IN ('promise','unresolved') GROUP BY 1""")}

        buckets = {"高(>0.8)": 0, "中(0.5-0.8)": 0, "低(0.2-0.5)": 0, "几乎忘(<0.2)": 0}
        now = datetime.now().astimezone()
        for r in s.conn.execute("SELECT stability, last_active_at, created_at FROM trace WHERE status='active'"):
            try:
                anchor = datetime.fromisoformat(r["last_active_at"] or r["created_at"])
                dt = max(0.0, (now - anchor).total_seconds() / 86400)
                R = math.exp(-dt / max(0.1, float(r["stability"] or 7)))
            except Exception:
                R = 1.0
            if R > 0.8:
                buckets["高(>0.8)"] += 1
            elif R > 0.5:
                buckets["中(0.5-0.8)"] += 1
            elif R > 0.2:
                buckets["低(0.2-0.5)"] += 1
            else:
                buckets["几乎忘(<0.2)"] += 1
        out["retention"] = buckets

        out["open_items"] = [{"id": r["id"], "kind": r["kind"], "text": r["text"]}
                             for r in s.conn.execute(
            """SELECT t.id, t.kind, v.text FROM trace t JOIN trace_version v ON v.id=t.head_version_id
               WHERE t.kind IN ('promise','unresolved') AND t.status='active'
                 AND COALESCE(t.state,'open')='open' ORDER BY t.id DESC LIMIT 6""")]
        out["most_recalled"] = [{"id": r["id"], "n": r["activation_count"]}
                                for r in s.conn.execute(
            """SELECT id, activation_count FROM trace WHERE activation_count > 0
               ORDER BY activation_count DESC LIMIT 5""")]
        out["forgotten"] = [{"id": r["id"], "text": r["text"], "days": r["days"]}
                            for r in s.conn.execute(
            """SELECT t.id, v.text,
                      CAST(julianday('now') - julianday(COALESCE(t.last_active_at, t.created_at)) AS INT) days
               FROM trace t JOIN trace_version v ON v.id=t.head_version_id
               WHERE t.status='active' ORDER BY days DESC LIMIT 5""")]
        return out
    finally:
        s.close()



@app.get("/api/timeline")
def api_timeline(limit: int = 80, offset: int = 0, include_archived: bool = False):
    s = db()
    try:
        where = "" if include_archived else "WHERE t.status='active'"
        rows = s.conn.execute(
            f"""SELECT t.id, t.kind, t.importance, t.status, COALESCE(t.state,'open') state,
                       t.created_at, t.activation_count, t.versions_count, v.text
                FROM trace t JOIN trace_version v ON v.id=t.head_version_id
                {where}
                ORDER BY t.created_at DESC, t.id DESC LIMIT ? OFFSET ?""",
            (max(1, min(500, int(limit))), max(0, int(offset)))).fetchall()
        total = s.conn.execute("SELECT COUNT(*) FROM trace").fetchone()[0]
        return {"total": total, "items": [dict(r) for r in rows]}
    finally:
        s.close()

@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=21600)
    ap.add_argument("--open", action="store_true")
    a = ap.parse_args()
    url = f"http://127.0.0.1:{a.port}/"
    if a.open:
        webbrowser.open(url)
    print(f"记忆面板：{url}")
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
