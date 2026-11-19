"""检索与印象（M3）。

规则固定、公开，不按点击率优化：
- 关键词（FTS trigram）与向量相似各占一半，时间邻近与未了项作为固定加成
- 可检索性 R 作为「能不能想起来」的衰减因子
- 印象只输出强度/数量/类别，不含内容、不带动词
"""

from __future__ import annotations

import os

import array
import asyncio
import json
import math
import urllib.request

try:
    from astrbot import logger
except Exception:  # 脱离 AstrBot 单独运行（面板、命令行工具）时用标准 logging
    import logging

    logger = logging.getLogger("absolutely_autonomous_memory")
    if not logger.handlers:
        _h = logging.StreamHandler()
        _h.setFormatter(logging.Formatter("%(asctime)s [memory] %(message)s"))
        logger.addHandler(_h)
    logger.setLevel(logging.INFO)


try:
    from .store import days_between, now_iso
except ImportError:  # 单独跑脚本时
    from store import days_between, now_iso  # type: ignore

# 向量服务：默认本机 Ollama；也可以配成任意 OpenAI 兼容的 /v1/embeddings
EMBED_MODE = "ollama"          # ollama | openai
EMBED_URL = "http://127.0.0.1:11434/api/embed"
EMBED_MODEL = "bge-m3"
EMBED_KEY = ""
EMBED_DISABLED = False
_EMBED_FAILS = 0
_EMBED_LAST_TRY = 0.0
_EMBED_RETRY_SEC = 300


def configure(url=None, model=None, mode=None, key=None) -> dict:
    """按插件配置设置向量服务。"""
    global EMBED_URL, EMBED_MODEL, EMBED_MODE, EMBED_KEY
    if url:
        EMBED_URL = str(url).strip()
    if model:
        EMBED_MODEL = str(model).strip()
    if mode:
        EMBED_MODE = str(mode).strip().lower()
    if key is not None:
        EMBED_KEY = str(key).strip()
    return embedding_status()


def embedding_status() -> dict:
    return {"mode": EMBED_MODE, "url": EMBED_URL, "model": EMBED_MODEL,
            "disabled": EMBED_DISABLED, "fails": _EMBED_FAILS}

# 固定权重（见设计文档；改动需整体替换版本号）
W_KEYWORD = 0.10          # 实测：trigram 关键词基本是噪音，压低它 @1 从 65%→90%
W_VECTOR = 0.80
W_TIME = 0.10
W_KEYWORD_EXACT = 0.35   # 短查询/含数字英文时，字面匹配更重要
BONUS_UNRESOLVED = 0.05   # 只在 base>=0.2 时加，避免约定压过事实
AVAIL_FLOOR = 0.5          # 再难想，也保留一半权重
VEC_FLOOR = 0.40           # bge-m3 对无关中文也有 ~0.4 基线，先扣掉再归一
IMPRESSION_THRESHOLD = 0.20
ENTITY_BONUS = 0.0
CHUNK_MODE = os.environ.get("AAM_CHUNK_MODE", "off")
CHUNK_W = float(os.environ.get("AAM_CHUNK_W", "0.3") or 0.3)
LEGACY_FACTOR = 1.0   # 实测降权会牺牲旧事件召回（75%→60%），默认不降
MIN_SCORE = 0.30          # below this, treat as 'not found'


def _parse_embed(data) -> list[float] | None:
    """兼容 Ollama（embeddings）和 OpenAI（data[].embedding）两种返回。"""
    if not isinstance(data, dict):
        return None
    vec = data.get("embeddings") or []
    if isinstance(vec, list) and vec and isinstance(vec[0], list) and vec[0]:
        return vec[0]
    arr = data.get("data")
    if isinstance(arr, list) and arr and isinstance(arr[0], dict):
        emb = arr[0].get("embedding")
        if isinstance(emb, list) and emb:
            return emb
    return None


def embed_sync(text: str) -> list[float] | None:
    global _EMBED_FAILS, EMBED_DISABLED, _EMBED_LAST_TRY
    import time as _time
    if EMBED_DISABLED:
        if _time.time() - _EMBED_LAST_TRY < _EMBED_RETRY_SEC:
            return None
        EMBED_DISABLED = False
        _EMBED_FAILS = 0
    try:
        headers = {"Content-Type": "application/json"}
        if EMBED_KEY:
            headers["Authorization"] = "Bearer " + EMBED_KEY
        req = urllib.request.Request(
            EMBED_URL,
            data=json.dumps({"model": EMBED_MODEL, "input": text[:2000]}).encode(),
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            vec = _parse_embed(json.loads(r.read().decode()))
        if vec:
            _EMBED_FAILS = 0
            return vec
        raise ValueError("返回里没有向量")
    except Exception as e:  # noqa: BLE001
        _EMBED_FAILS += 1
        _EMBED_LAST_TRY = _time.time()
        if _EMBED_FAILS >= 3 and not EMBED_DISABLED:
            EMBED_DISABLED = True
            logger.warning(
                f"[memory] 向量服务不可用（{EMBED_URL}），检索降级为关键词模式；"
                "请检查 embedding_url / embedding_model 配置")
        else:
            logger.warning(f"[memory] 向量化失败：{type(e).__name__}: {e}")
        return None


async def embed(text: str) -> list[float] | None:
    return await asyncio.to_thread(embed_sync, text)


# ---------- 向量存取 ----------

def pack(vec: list[float]) -> bytes:
    return array.array("f", vec).tobytes()


def unpack(blob: bytes) -> list[float]:
    a = array.array("f")
    a.frombytes(blob)
    return a.tolist()


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def ensure_vectors(store, trace_ids: list[int] | None = None, limit: int = 200) -> int:
    """给缺向量的痕迹补算（同步，调用方自己挑时机）。"""
    if trace_ids:
        rows = [store.trace_row(i) for i in trace_ids]
        rows = [r for r in rows if r]
    else:
        rows = list(store.conn.execute(
            """SELECT t.* FROM trace t LEFT JOIN trace_vec v ON v.trace_id=t.id
               WHERE v.trace_id IS NULL LIMIT ?""", (limit,)))
    if EMBED_DISABLED:
        return 0
    n = 0
    for row in rows:
        head = store.conn.execute("SELECT text, topics FROM trace_version WHERE id=?",
                                  (row["head_version_id"],)).fetchone()
        if not head:
            continue
        try:
            import json as _json
            _topics = _json.loads(head["topics"]) if head["topics"] else []
        except Exception:
            _topics = []
        try:
            from .store import Store          # 作为包 import（AstrBot 加载）
        except ImportError:
            from store import Store           # 以目录形式单独运行（脚本、面板）
        _payload = Store.index_text(head["text"], _topics)
        vec = embed_sync(_payload)
        if not vec:
            continue
        store.conn.execute(
            "INSERT OR REPLACE INTO trace_vec(trace_id, vec, dim, model, updated_at) VALUES (?,?,?,?,datetime('now'))",
            (row["id"], pack(vec), len(vec), EMBED_MODEL),
        )
        n += 1
    store.conn.commit()
    return n


# ---------- 检索 ----------

def _keyword_scores(store, query: str, limit: int = 40) -> dict[int, float]:
    """FTS 命中归一化到 0-1。短词退回子串匹配。"""
    q = (query or "").strip()
    out: dict[int, float] = {}
    if not q:
        return out
    tokens = [t for t in q.split() if t]
    long_enough = bool(tokens) and all(len(t) >= 3 for t in tokens)
    if long_enough:
        try:
            rows = store.conn.execute(
                """SELECT trace_id, bm25(trace_fts) AS s FROM trace_fts WHERE trace_fts MATCH ? LIMIT ?""",
                (q, limit),
            ).fetchall()
            if rows:
                vals = [abs(r["s"]) for r in rows]
                top = max(vals) if vals else 1.0
                for r in rows:
                    out[int(r["trace_id"])] = abs(r["s"]) / top if top else 0.0
                return out
        except Exception:
            pass  # 退回子串匹配
    # 短词 / 混合词：逐个 LIKE
    for tok in (tokens or [q]):
        rows = store.conn.execute(
            """SELECT t.id FROM trace t JOIN trace_version v ON v.id=t.head_version_id
               WHERE v.text LIKE ? OR v.topics LIKE ? LIMIT ?""",
            (f"%{tok}%", f"%{tok}%", limit)).fetchall()
        for r in rows:
            out[int(r["id"])] = max(out.get(int(r["id"]), 0.0), 0.8 if len(tok) < 3 else 0.6)
    if out:
        return out
    # 整句一个词也匹配不到时，用 2-gram 兜底（中文没有空格分词）
    grams = {q[i:i + 2] for i in range(len(q) - 1)}
    total = store.conn.execute("SELECT COUNT(*) FROM trace").fetchone()[0] or 1
    cap = max(4, int(total * 0.03))
    for g in list(grams)[:12]:
        rows = store.conn.execute(
            """SELECT t.id FROM trace t JOIN trace_version v ON v.id=t.head_version_id
               WHERE v.text LIKE ? LIMIT ?""", (f"%{g}%", limit)).fetchall()
        if len(rows) > cap:
            continue  # 这个词太常见，没有区分度
        for r in rows:
            out[int(r["id"])] = max(out.get(int(r["id"]), 0.0), 0.45)
    return out


def search(store, query: str, top_k: int = 5, kind: str = "",
           include_archived: bool = False, since: str = "", until: str = "",
           scope: str = "", person: str = "") -> list[dict]:
    kw = _keyword_scores(store, query)
    qvec = embed_sync(query) if query.strip() else None
    candidates = set(kw)
    if qvec:
        for r in store.conn.execute("SELECT trace_id, vec FROM trace_vec"):
            candidates.add(int(r["trace_id"]))
    if not candidates:
        # 没有关键词命中也无向量时，退回最近激活的几条
        rows = store.conn.execute("SELECT id FROM trace WHERE status='active' AND avoided=0 ORDER BY last_active_at DESC LIMIT ?",
                                  (top_k,)).fetchall()
        candidates = {int(r["id"]) for r in rows}

    _CHUNK_MODE = os.environ.get("AAM_CHUNK_MODE", CHUNK_MODE) or "off"
    try:
        _CHUNK_W = float(os.environ.get("AAM_CHUNK_W", CHUNK_W))
    except Exception:
        _CHUNK_W = 0.3
    # 长文本的句子块：整段抓「那件事」，块抓「那个细节」，按模式组合
    chunk_vecs = {}
    try:
        chunk_vecs = store.all_chunk_vecs()
    except Exception:
        chunk_vecs = {}

    scored = []
    now = now_iso()
    for tid in candidates:
        row = store.trace_row(tid)
        if row is None or row["status"] == "faded" or row["avoided"]:
            continue
        if row["status"] == "archived" and not include_archived:
            continue  # 淡化的、归档的、她选择回避的，都不主动浮现
        head = store.conn.execute(
            "SELECT text, anchor_time, topics, felt, reaction, inference, inference_certainty "
            "FROM trace_version WHERE id=?", (row["head_version_id"],)).fetchone()
        if head is None:
            continue
        when = (head["anchor_time"] or row["created_at"] or "")[:10]
        if since and when and when < since:
            continue
        if until and when and when > until:
            continue
        if scope:
            _sc = row["scope"] if "scope" in row.keys() else ""
            _sc = _sc or ""
            if scope == "private" and not (_sc == "private" or _sc.startswith("private:")):
                continue
            if scope == "group" and not _sc.startswith("group:"):
                continue
            if scope.startswith("group:") and _sc != scope:
                continue
        if person:
            _hay = (head["text"] or "") + " " + (head["topics"] or "")
            if person not in _hay:
                continue
        k = kw.get(tid, 0.0)
        v = 0.0
        if qvec:
            vr = store.conn.execute("SELECT vec FROM trace_vec WHERE trace_id=?", (tid,)).fetchone()
            if vr:
                raw = cosine(qvec, unpack(vr["vec"]))
                blobs = chunk_vecs.get(tid, [])[:20]
                if blobs and _CHUNK_MODE in ("max", "blend"):
                    best = raw
                    for blob in blobs:
                        try:
                            best = max(best, cosine(qvec, unpack(blob)))
                        except Exception:
                            break
                    if _CHUNK_MODE == "max":
                        raw = best
                    else:
                        raw = (1.0 - _CHUNK_W) * raw + _CHUNK_W * best
                v = max(0.0, min(1.0, (raw - VEC_FLOOR) / (1.0 - VEC_FLOOR)))
        tprox = math.exp(-days_between(head["anchor_time"] or row["created_at"], now) / 30.0)
        kw_w = W_KEYWORD
        if not qvec:
            kw_w = max(kw_w, 0.7)   # 没有向量服务：关键词为主
        # 短查询、或含数字/英文：这类靠字面，给关键词一条活路
        try:
            _q = (query or "").strip()
            if len(_q) <= 6 or any((c.isdigit() or c.isascii()) and not c.isspace() for c in _q):
                kw_w = max(W_KEYWORD, W_KEYWORD_EXACT)
        except Exception:
            pass
        base = kw_w * k + W_VECTOR * v + W_TIME * tprox
        if (row["kind"] in ("unresolved", "promise") and row["status"] == "active"
                and base >= 0.2):
            base += BONUS_UNRESOLVED
        r = store.retrievability(row, now)
        final = base * (AVAIL_FLOOR + (1 - AVAIL_FLOOR) * r)
        # 旧插件迁移来的观察：写成了几百字日记，和新笔记抢位置，降一档
        if row["kind"] == "observation" and (row["source"] or "") == "livingmemory":
            try:
                _f = float(os.environ.get("AAM_LEGACY_FACTOR", LEGACY_FACTOR) or 1.0)
            except Exception:
                _f = LEGACY_FACTOR
            final *= _f
        if kind and row["kind"] != kind:
            continue
        scored.append({
            "id": int(row["id"]),
            "kind": row["kind"],
            "text": head["text"],
            "anchor_time": head["anchor_time"],
            "importance": round(float(row["importance"]), 2),
            "certainty": None,
            "score": round(final, 3),
            "r_now": round(r, 3),
            "activations": row["activation_count"],
            "state": row["state"] if "state" in row.keys() else "open",
            "scope": row["scope"] if "scope" in row.keys() else "",
            "anchor_quote": head["anchor_quote"] if "anchor_quote" in head.keys() else "",
            "felt": head["felt"] if "felt" in head.keys() else "",
            "reaction": head["reaction"] if "reaction" in head.keys() else "",
            "inference": head["inference"] if "inference" in head.keys() else "",
            "inference_certainty": head["inference_certainty"]
                if "inference_certainty" in head.keys() else None,
        })
    # 实体加分：查询里出现了某个实体名，就给它关联的痕迹加一点分（1 跳，不做多跳）
    try:
        import os as _os
        _bonus = float(_os.environ.get("AAM_ENTITY_BONUS", ENTITY_BONUS) or 0)
    except Exception:
        _bonus = ENTITY_BONUS
    if _bonus > 0 and query:
        try:
            boost = {}
            for r in store.entity_traces(query, 80):
                boost[int(r["trace_id"])] = _bonus
            for h in scored:
                if int(h["id"]) in boost:
                    h["score"] = round(h["score"] + boost[int(h["id"])], 4)
                    h["entity_hit"] = True
        except Exception:
            pass

    scored.sort(key=lambda x: -x["score"])
    scored = _collapse_similar(scored, store)
    scored = [h for h in scored if h["score"] >= MIN_SCORE]
    return scored[:top_k]



def _norm(t: str) -> str:
    return "".join(ch for ch in (t or "") if ch.isalnum())


def _collapse_similar(hits: list[dict], store=None) -> list[dict]:
    """结果里同一件事/同一系列只留一条代表，其余折叠成计数。

    只在最前面的 60 条里做，向量一次性读出来——不然条数一多会变成 O(n²) 查库。
    代表条目带 group_size / group_ids / group_first / group_last，检索层显示
    「同系列共 N 条」，不把这些发生从库里删掉。
    """
    import difflib

    hits = hits[:60]
    vecs: dict[int, list[float]] = {}
    series_map: dict[int, dict] = {}
    ids = [int(h["id"]) for h in hits]
    if store is not None:
        try:
            q = ",".join("?" * len(ids))
            for r in store.conn.execute(f"SELECT trace_id, vec FROM trace_vec WHERE trace_id IN ({q})", ids):
                vecs[int(r["trace_id"])] = unpack(r["vec"])
        except Exception:
            vecs = {}
        try:
            series_map = store.series_for_traces(ids)
        except Exception:
            series_map = {}

    kept: list[dict] = []
    for h in hits:
        hn = _norm(h["text"])
        sid = int((series_map.get(int(h["id"])) or {}).get("id") or 0)
        sname = str((series_map.get(int(h["id"])) or {}).get("name") or "")
        h["series_id"] = sid
        h["series_name"] = sname
        dup = None
        if sid:
            for k in kept:
                if int(k.get("series_id") or 0) == sid:
                    dup = k
                    break
        else:
            for k in kept:
                if k.get("series_id"):
                    continue
                if k["kind"] != h["kind"]:
                    continue
                if (k.get("scope") or "") != (h.get("scope") or ""):
                    continue
                if difflib.SequenceMatcher(None, hn, _norm(k["text"])).ratio() >= 0.8:
                    dup = k
                    break
                a, b = vecs.get(int(h["id"])), vecs.get(int(k["id"]))
                if a and b and cosine(a, b) >= 0.88:
                    dup = k
                    break
        if dup is not None:
            dup["group_size"] = int(dup.get("group_size") or 1) + 1
            dup.setdefault("group_ids", [int(dup["id"])]).append(int(h["id"]))
            if sid:
                dup["group_label"] = sname
                dup["group_kind"] = "series"
            t = str(h.get("anchor_time") or "")
            if t:
                first = str(dup.get("group_first") or "")
                last = str(dup.get("group_last") or "")
                if not first or t < first:
                    dup["group_first"] = t
                if not last or t > last:
                    dup["group_last"] = t
            continue
        h["group_size"] = 1
        h["group_ids"] = [int(h["id"])]
        h["group_first"] = str(h.get("anchor_time") or "")
        h["group_last"] = str(h.get("anchor_time") or "")
        if sid:
            h["group_label"] = sname
            h["group_kind"] = "series"
        kept.append(h)
    return kept


def impression(store, text: str) -> dict | None:
    """只给强度/数量/类别；低于阈值返回 None。"""
    if not (text or "").strip():
        return None
    hits = search(store, text, top_k=6)
    if not hits:
        return None
    top = hits[0]
    if top["score"] < IMPRESSION_THRESHOLD:
        return None
    relevant = [h for h in hits if h["score"] >= 0.30]
    if not relevant:
        return None
    open_unresolved = any(h["kind"] in ("unresolved", "promise") and h.get("state", "open") == "open"
                          for h in relevant)
    kind = "未了" if open_unresolved else "话题"
    return {"strength": round(min(1.0, top["score"]), 2), "count": len(relevant), "kind": kind}


def related_open_items(store, text: str, session_id: str = "",
                        min_sim: float = 0.55, limit: int = 1) -> list[dict]:
    """这句话可能和哪条挂着的约定/未了的事有关？（只递材料，不判定了结）"""
    text = (text or "").strip()
    if len(text) < 3:
        return []
    qv = embed_sync(text)
    if not qv:
        return []
    rows = list(store.conn.execute(
        """SELECT t.id, t.kind, v.text, tv.vec, e.session_id
           FROM trace t
           JOIN trace_version v ON v.id=t.head_version_id
           LEFT JOIN trace_vec tv ON tv.trace_id=t.id
           LEFT JOIN episode e ON e.id=t.episode_id
           WHERE t.kind IN ('promise','unresolved') AND t.status='active' AND t.avoided=0
             AND COALESCE(t.state,'open')='open'"""))
    scored = []
    for r in rows:
        if session_id and (r["session_id"] or "") and r["session_id"] != session_id:
            continue
        if not r["vec"]:
            continue
        try:
            sim = cosine(qv, unpack(r["vec"]))
        except Exception:
            continue
        if sim >= min_sim:
            scored.append({"id": int(r["id"]), "kind": r["kind"],
                           "text": r["text"] or "", "sim": round(float(sim), 3)})
    scored.sort(key=lambda x: -x["sim"])
    return scored[:int(limit)]


def impression_line(imp: dict | None) -> str | None:
    if not imp:
        return None
    s = float(imp["strength"])
    if s < 0.20:
        return None
    weakest = " · 似有若无" if s < 0.5 else ""
    kind = imp.get("kind") or "话题"
    if s >= 0.65:
        if kind == "未了":
            tail = "这里有件还没结的事对得上，要不要翻翻？"
        else:
            tail = "这条和一件旧事对得上，要不要看看？"
    elif s >= 0.45:
        tail = "这里似乎有一件相关的事，要不要翻翻？"
    else:
        tail = "似乎有点相关，要不要翻翻？不翻也没关系。"
    return (f"[印象 · 熟悉度 {s:.2f} · {imp['count']} 条 · {kind}]{weakest} "
            f"{tail}")
