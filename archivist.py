"""落成痕迹：把一段对话交给模型，提炼成若干条「痕迹」（M2）。

原则（见设计文档）：
- 只留值得留下的，宁缺毋滥
- 第一人称、一句话、原子事实
- 不写过程、不写流水账
- 推测必须标 certainty
"""

from __future__ import annotations

import os

import json
import re

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
    from . import retriever
except ImportError:  # 单独跑脚本时
    import retriever  # type: ignore

SYSTEM = (
    "你在一段对话结束后，以第一人称给自己留几笔。"
    "只留真正值得留下的，宁少勿多。"
    "直接输出 JSON，不要解释。"
)

TEMPLATE = """下面是一段刚发生的对话（格式：[角色] 内容，你自己是「她」）。

如果下面的「还没了结的事」里，有哪件被这次对话说清了、办完了、或者作废了，
请在 updates 里标出来（没有就不写 updates）。
如果对话里已经能看出某条约定的条件满足了——比如他说下课了、说要去吃饭、时间明显过去了——也可以标 settled，并在 note 里写清依据。

{open_items}

请挑出值得长期留下的，输出 JSON：
{{"items":[{{"kind":"...","text":"...","importance":0.5,"certainty":null,"event_time":null,"due_at":null,"due_time":null,"recur":null,"entities":["..."],"topics":["..."],"anchor_quote":"...","felt":"闷","reaction":"没接话","inference":"她可能累了","inference_certainty":0.6,"tags":["..."]}}],
 "updates":[{{"id":123,"state":"settled|void|open","note":"一句话说明"}}]}}

kind 只能是这六种：
- fact：他明确说过的、关于他自己的事实（喜好、习惯、身份、正在忙的事、家人朋友）
- observation：你观察到的（他的状态、气氛、你们之间发生了什么）
- speculation：你的猜测或推断（必须给 certainty，0~1）
- self：你自己此刻的想法、感受、在意的事
- unresolved：还没了结的事（他没回的、你等着问的）
- promise：他说定或答应的事，**且有明确时间或明确待办**（例如「十点半前写日记」）
- agreement：**没有时间的承诺/长期说法**（例如「以后不舒服就直接说」「饿了会喊他」）
  它不欠着、不提醒，只是「我们说好过」；写的时候别硬加时间

写作要求：
- text 用第一人称，40 字以内，像你心里记下的一笔，不是事件报告
- **text 只写发生的事和可观察的动作/原话**；不写「她有点烦」「我觉得他…」这种替人下的判断
- felt：当时的感觉，1–3 个模糊词就行（闷 / 松 / 急 / 憋着 / 说不清），不要写成一整句
- reaction：你当时的反应（动作或原话），不解释
- inference：你对别人的推断（如「她可能累了」），必须配 inference_certainty（0–1）；没有推断就留空
- 一条只写一件事；不要写「他跟我说…然后我说…」这种过程
- **写名字，不要写「他」「她」**：对话行方括号里的名字就是说话人；如果那里只有「他」「她」，才用代词
- 寒暄、玩笑、没有信息量的重复、你自己单方面的客套，都不要
- 不要写「我没想起来」「我记不清」「我没答上」这类关于你自己记忆的挫败——那不是未了的事，是噪音
- unresolved 只记外部没办完的（他没回的、你要问的、答应要做还没做的）
- importance：这件事对以后的判断有多大用，按下面的基准给死：
  · 0.9–1.0：只给会改变你们关系、或长期约束你的约定（例如「以后不许忘」「我答应过每天喊你」）
  · 0.6–0.8：明确的事实或承诺（他要去医院、他下月出差、他给你起了名字）
  · 0.3–0.5：有点用但不确定以后会不会用到（他在翻什么书、他睡不好）
  · 0.1–0.2：日常闲聊、随口一说（新开的面馆、语气上的小玩笑）
  · 大多数条目应该落在 0.2–0.6；**不要习惯性给 0.8 以上**，那会让小事和大事一样难忘
- topics：2–3 个主题词，用来回答「这条记忆是关于什么的」。
  · 至少 2 个必须是**别人可能用来问的词**（如「换客户端」「白饭减半」「生日」），2–6 个字；
  · 最多 1 个可以用你自己的说法（如「那句噎住的话」）；
  · 只能从原文里概括，不许联想、不许加原文没有的概念；
  · 禁用这些词：情绪、聊天、日常、事情、主人、我、他、她、它。
- entities：这条记忆里**明确出现**的人、物、群、作品的名字，最多 3 个，只写名字（如「某人」「记忆插件」），不写代词、不写形容词；没有就给空数组。
- event_time：这件事**本身**什么时候发生/成立，ISO 格式带时区，比如 2026-11-11T15:00:00+08:00。
  原文说了「今天下午三点」「明天」「上周」就换算成绝对时间；没说、说不准就填 null，不要猜。
  只有「一点」「两点」这种没说上午还是下午的，看不出来就只写日期（不带时刻），别硬选。
- due_at：**只有 promise（约定）才填**——这件事该在什么时候做，绝对时间带时区。
  比如「十点半前写日记」→ 当天 22:30；「明早九点叫我」→ 次日 09:00；「明天下午接我」→ 明天那个时刻。
  **填了才会到点提醒，不填就永远不会提醒**，所以原文给了时间就一定要填。
  说不出确切时刻才填 null，**不许猜**；不是约定一律 null。
- due_time / recur：**只有循环约定才填**。
  「以后每天早上叫我起床」「每天提醒我吃药」→ recur="daily"，due_time="07:00"，
  due_at 留空（系统按 due_time 算下一次）。
  recur 只允许 daily（每周的先不填）。
  **注意：一次性的时间（明天九点、18号早上八点）必须写 due_at，不要写 due_time。**
  例：「18号早上八点提醒我带充电器」→ due_at="2026-11-18T08:00:00+08:00"，due_time=null，recur=null。
- anchor_quote：对话里最能定位这件事的一句原话（照抄，可截断）
- tags：最多 2 个短标签（人/主题）
- 最多 5 条；没有值得留的就输出 {{"items":[]}}

对话：
{conversation}
"""


TOPIC_BAN = {"情绪", "聊天", "日常", "事情", "主人", "我", "他", "她", "它"}


def _clean_topics(raw) -> list[str]:
    """过滤主题词：2–6 字、去禁词、去重、最多 3 个。"""
    out = []
    for x in (raw or [])[:6]:
        t = str(x).strip().strip("「」“”\"'")
        if not (2 <= len(t) <= 6):
            continue
        if t in TOPIC_BAN:
            continue
        if t not in out:
            out.append(t)
    return out[:3]


TOPIC_REVIEW_TPL = """这些是你以前的记忆，还没给它们起过名。
你回头看一遍，给每条起 2–3 个主题词——就是「这条记忆是关于什么的」。

{items}

规则：
- 至少 2 个必须是**别人可能用来问的词**（比如「换客户端」「白饭减半」「生日」），2–6 个字；
- 最多 1 个可以用你自己的说法；
- 只能从原文里概括，别加原文没有的事；
- 不想给某条起名，就把它留空。

只输出 JSON：{{"items":[{{"id":123,"topics":["换客户端","断连"]}}]}}"""


def open_items_block(store) -> str:
    rows = store.conn.execute(
        """SELECT t.id, t.kind, v.text FROM trace t JOIN trace_version v ON v.id=t.head_version_id
           WHERE t.kind IN ('promise','unresolved') AND t.status='active'
             AND COALESCE(t.state,'open') IN ('open','missed')
             ORDER BY t.id DESC LIMIT 10""").fetchall()
    if not rows:
        return "（没有）"
    return "\n".join(f"- #{int(r['id'])} [{r['kind']}] {r['text']}" for r in rows)


def build_prompt(lines: list[dict], store=None) -> str:
    body = []
    for m in lines:
        name = (m.get("sender") or "").strip()
        who = name if name else ("他" if m.get("role") == "user" else "她")
        ts = (m.get("created_at") or "")[11:16]
        body.append(f"[{who} {ts}] {m.get('text','')}"[:400])
    return TEMPLATE.format(conversation="\n".join(body),
                           open_items=open_items_block(store) if store is not None else "（没有）")


def _safe_certainty(value):
    if value is None:
        return None
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


def parse_items(text: str) -> tuple[list[dict], list[dict]]:
    raw = (text or "").strip()
    if not raw:
        return []
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    items = data.get("items")
    updates = data.get("updates")
    if not isinstance(items, list):
        items = []
    if not isinstance(updates, list):
        updates = []
    out = []
    valid = ("fact", "observation", "speculation", "self", "unresolved", "promise", "agreement")
    for it in items[:5]:
        if not isinstance(it, dict):
            continue
        kind = str(it.get("kind") or "").strip()
        text_ = str(it.get("text") or "").strip()
        if kind not in valid or not text_:
            continue
        try:
            importance = float(it.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        certainty = it.get("certainty")
        if certainty is not None:
            try:
                certainty = max(0.0, min(1.0, float(certainty)))
            except (TypeError, ValueError):
                certainty = None
        if kind == "speculation" and certainty is None:
            certainty = 0.5
        tags = it.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        out.append({
            "kind": kind,
            "text": text_[:200],
            "importance": max(0.0, min(1.0, importance)),
            "certainty": certainty,
            "event_time": str(it.get("event_time") or "").strip()[:32] or None,
            "entities": [str(x).strip()[:24] for x in (it.get("entities") or [])
                         if isinstance(x, (str, int)) and str(x).strip()][:3],
            "topics": _clean_topics(it.get("topics")),
            "due_at": (str(it.get("due_at") or "").strip()[:32] or None)
                      if it.get("kind") == "promise" else None,
            "due_time": (str(it.get("due_time") or "").strip()[:5] or None)
                        if it.get("kind") == "promise" else None,
            "recur": ("daily" if str(it.get("recur") or "").strip().lower() == "daily" else "")
                     if it.get("kind") == "promise" else "",
            "anchor_quote": str(it.get("anchor_quote") or "").strip()[:200] or None,
            "felt": str(it.get("felt") or "").strip()[:24] or None,
            "reaction": str(it.get("reaction") or "").strip()[:120] or None,
            "inference": str(it.get("inference") or "").strip()[:120] or None,
            "inference_certainty": _safe_certainty(it.get("inference_certainty")),
            "tags": [str(t)[:12] for t in tags[:2]],
        })
    outs = []
    for u in updates[:6]:
        if not isinstance(u, dict):
            continue
        try:
            oid = int(u.get("id"))
        except (TypeError, ValueError):
            continue
        st = str(u.get("state") or "").strip()
        if st not in ("settled", "void", "open"):
            continue
        outs.append({"id": oid, "state": st, "note": str(u.get("note") or "").strip()[:60]})
    return out, outs


async def extract(provider, lines: list[dict], store=None) -> tuple[list[dict], list[dict]]:
    if provider is None or not lines:
        return []
    try:
        resp = await provider.text_chat(
            prompt=build_prompt(lines, store),
            system_prompt=SYSTEM,
            session_id="memory-archivist",
            persist=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[memory] 提炼失败：{type(e).__name__}: {e}")
        return []
    items, updates = parse_items(getattr(resp, "completion_text", "") or "")
    if items:
        logger.info(f"[memory] 提炼出 {len(items)} 条：" +
                    "；".join(f"{i['kind']}:{i['text'][:16]}" for i in items))
    if updates:
        logger.info("[memory] 了结标记：" + "；".join(f"#{u['id']}→{u['state']}" for u in updates))
    return items, updates


NOISE = ("我没想起来", "我没记起来", "我记不清", "记不清了", "我没答上", "我答不上",
         "没答上来", "我没说上来", "我不知道他", "我想不起来")


def is_self_noise(item: dict) -> bool:
    """过滤「关于她自己记不记得」的自言自语——那不是未了的事。"""
    if item.get("kind") not in ("unresolved", "observation", "self", "speculation"):
        return False
    t = item.get("text", "")
    return any(n in t for n in NOISE)


def _norm(t: str) -> str:
    return "".join(ch for ch in (t or "") if ch.isalnum())


def find_near_duplicate(store, text: str, kind: str) -> int | None:
    """同类型里找重复的旧痕迹：**跟它的所有版本比**（不只是当前版本）。

    判定：向量相似 ≥0.90 且 字面相似 ≥0.60，两个条件同时满足才算重复。
    """
    import difflib

    norm = _norm(text)
    if len(norm) < 4:
        return None
    vec = retriever.embed_sync(text) if len(norm) >= 10 else None

    # 每条痕迹的全部版本，一次读出来
    versions: dict[int, list[str]] = {}
    for r in store.conn.execute(
            """SELECT v.trace_id AS tid, v.text FROM trace t
               JOIN trace_version v ON v.trace_id = t.id
               WHERE t.kind=? AND t.status='active' AND t.merged_into IS NULL""", (kind,)):
        versions.setdefault(int(r["tid"]), []).append(r["text"] or "")

    best_id, best_ratio, best_cos = None, 0.0, 0.0
    cand_ids = list(versions)
    for tid in cand_ids:
        for vt in versions[tid]:
            ratio = difflib.SequenceMatcher(None, norm, _norm(vt)).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_id = tid

    if vec is None:
        # 短句只认几乎相同（差一个字可能就是另一件事）
        return best_id if best_ratio >= 0.92 else None

    for r in store.conn.execute(
            """SELECT v.trace_id, v.vec FROM trace_vec v JOIN trace t ON t.id=v.trace_id
               WHERE t.kind=? AND t.status='active' AND t.merged_into IS NULL""", (kind,)):
        sim = retriever.cosine(vec, retriever.unpack(r["vec"]))
        if sim > best_cos:
            best_cos, best_id = sim, int(r["trace_id"])

    if best_cos >= 0.90 and best_ratio >= 0.60:
        return best_id
    return None



def suppress_similar(store, text: str, kind: str, lo: float = 0.75, hi: float = 0.90) -> int | None:
    """相似但没到重复的旧痕迹：降低它的稳定度（想起来会压住旁边的），不删。"""
    vec = retriever.embed_sync(text)
    if not vec:
        return None
    best_id, best_sim = None, 0.0
    for r in store.conn.execute(
            """SELECT v.trace_id, v.vec FROM trace_vec v JOIN trace t ON t.id=v.trace_id
               WHERE t.kind=? AND t.status='active' AND t.avoided=0""", (kind,)):
        sim = retriever.cosine(vec, retriever.unpack(r["vec"]))
        if sim > best_sim:
            best_sim, best_id = sim, int(r["trace_id"])
    if best_id is not None and lo <= best_sim < hi:
        row = store.trace_row(best_id)
        if row:
            new_s = max(1.0, float(row["stability"] or 7.0) * 0.9)
            store.conn.execute("UPDATE trace SET stability=? WHERE id=?", (new_s, best_id))
            store.conn.commit()
            return best_id
    return None



RESOLVE_SYSTEM = (
    "你在帮她整理记忆。给你几条「新知道的事实」和一份「还没了结的事」，"
    "判断哪些事已经被这些事实了结了。只输出 JSON。"
)

RESOLVE_TEMPLATE = """新知道的事实：
{facts}

还没了结的事：
{opens}

哪些事被上面的事实了结了？
- 办完了、说清了 → state = "settled"
- 作废了、根本不成立 → state = "void"
- 只是相关、但还没了结 → 不要列出

只输出 JSON：{{"resolved":[{{"id":123,"state":"settled|void","why":"不超过 15 字"}}]}}

判断要严：只是提到同一个话题、时间地点不同、或者事情还没真办完的，都不算。
"""

RESOLVE_MAX = 5


async def resolve_conflicts(store, new_texts: list[str], provider, session: str = "",
                            limit: int = RESOLVE_MAX) -> list[dict]:
    """用新事实去撞「还没了结的事」：一次调用，让模型自己扫。

    比逐对算相似度可靠——「明天不用去医院」和「喊你起床带你去复查」
    在向量上只是中等相似，但语义上确实了结了。
    """
    import json as _json

    if not new_texts or provider is None:
        return []
    opens = list(store.conn.execute(
        """SELECT t.id, t.kind, v.text FROM trace t JOIN trace_version v ON v.id=t.head_version_id
           WHERE t.kind IN ('promise','unresolved') AND t.status='active'
             AND COALESCE(t.state,'open')='open' ORDER BY t.id DESC LIMIT 12"""))
    if not opens:
        return []

    facts_block = "\n".join(f"- {t}" for t in new_texts[:8])
    opens_block = "\n".join(f"- #{int(o['id'])} [{o['kind']}] {o['text']}" for o in opens)
    try:
        resp = await provider.text_chat(
            prompt=RESOLVE_TEMPLATE.format(facts=facts_block, opens=opens_block),
            system_prompt=RESOLVE_SYSTEM, session_id="memory-resolve", persist=False)
        raw = (getattr(resp, "completion_text", "") or "").strip()
        m = raw[raw.find("{"): raw.rfind("}") + 1]
        data = _json.loads(m)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[memory] 事实了结判定失败：{type(e).__name__}: {e}")
        return []

    known = {int(o["id"]): o for o in opens}
    # 新事实的向量，用来判断是哪一条事实导致的（多条时取最像的）
    fact_vecs = []
    for t in new_texts[:8]:
        v = retriever.embed_sync(t)
        if v:
            fact_vecs.append((t, v))
    fact_rows = {}
    for text, _vec in fact_vecs:
        r = store.conn.execute(
            """SELECT t.id FROM trace t JOIN trace_version v ON v.id=t.head_version_id
               WHERE v.text=? ORDER BY t.id DESC LIMIT 1""", (text,)).fetchone()
        if r:
            fact_rows[text] = (int(r["id"]), text)

    def causing_fact(promise_text: str) -> tuple[int, str] | None:
        """哪条新事实让它了结的：向量最像的那条。"""
        if not fact_rows:
            return None
        pv = retriever.embed_sync(promise_text)
        if not pv:
            return next(iter(fact_rows.values()))
        best = None
        best_cos = -1.0
        for text, vec in fact_vecs:
            if text not in fact_rows:
                continue
            c = retriever.cosine(pv, vec)
            if c > best_cos:
                best_cos, best = c, fact_rows[text]
        return best

    done = []
    for u in (data.get("resolved") or [])[:max(1, int(limit or RESOLVE_MAX))]:
        try:
            oid = int(u.get("id"))
        except (TypeError, ValueError):
            continue
        st = str(u.get("state") or "")
        if oid not in known or st not in ("settled", "void"):
            continue
        done.append(settle_with_fact(store, oid, st, str(u.get("why") or "")[:40],
                                     new_texts, session))
    return done

def _pick_fact(store, promise_text: str, candidates: list[str], exclude_id: int | None = None):
    """从候选事实里挑出最可能是「了结来源」的那条。

    必须排除两件事：
      - 被了结的那条自己（否则会出现「我说了 A；因此 A 作废」的自指）
      - 约定/未了类的痕迹（它们不是"事实"，不能当成了结来源）
    """
    rows = {}
    for text in candidates[:10]:
        r = store.conn.execute(
            """SELECT t.id, t.kind FROM trace t JOIN trace_version v ON v.id=t.head_version_id
               WHERE v.text=? ORDER BY t.id DESC LIMIT 1""", (text,)).fetchone()
        if not r:
            continue
        if exclude_id is not None and int(r["id"]) == int(exclude_id):
            continue
        if r["kind"] in ("promise", "unresolved"):
            continue
        rows[text] = int(r["id"])
    if not rows:
        return None
    pv = retriever.embed_sync(promise_text)
    if not pv:
        text, fid = next(iter(rows.items()))
        return fid, text
    best, best_cos = None, -1.0
    for text, fid in rows.items():
        v = retriever.embed_sync(text)
        if not v:
            continue
        c = retriever.cosine(pv, v)
        if c > best_cos:
            best_cos, best = c, (fid, text)
    if best is None:
        text, fid = next(iter(rows.items()))
        return fid, text
    if best_cos < 0.55:
        # 跟被了结的事几乎不相干，不硬挂——只改状态，不往无关痕迹上写「因此…」
        logger.info(f"[memory] 来源事实相似度只有 {best_cos:.2f}，不挂到那条上")
        return None
    return best


def settle_with_fact(store, oid: int, state: str, why: str, candidates: list[str],
                     session: str = "") -> dict:
    """收掉一条约定/未了：改状态 + 尽量转成事实的一版 + 归档 + 记更正。

    两条路径（事实判定 / 提炼时标注）都走这里，保证行为一致。
    """
    row = store.trace_row(oid)
    if not row:
        return {"id": oid, "ok": False}
    head = store.conn.execute("SELECT text FROM trace_version WHERE id=?",
                              (row["head_version_id"],)).fetchone()
    promise_text = (head["text"] if head else "") or ""
    store.settle(oid, state, f"被事实了结：{why}" if why else "被事实了结")

    src = _pick_fact(store, promise_text, candidates, exclude_id=oid)
    if src:
        fid, ftext = src
        tail = "作废" if state == "void" else "了结"
        delta = f"「{promise_text[:20]}」{tail}" + (f"（{why}）" if why else "")
        try:
            store.add_resolution(fid, delta)
            store.add_note(fid, f"因此了结了 #{oid}：{promise_text[:30]}", kind="evidence")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 转事实失败：{type(e).__name__}")
        store.archive(oid, merged_into=fid)
    else:
        logger.info(f"[memory] #{oid} 只改了状态（没找到来源事实）")

    if session:
        store.add_correction(
            session,
            f"#{oid}「{promise_text[:30]}」已经{'作废' if state == 'void' else '了结'}"
            + (f"：{why}" if why else "") + "。别再按原来的说法讲。",
        )
    return {"id": oid, "state": state, "why": why, "as_fact": src[0] if src else None}


SELF_SYSTEM = (
    "你在帮她整理记忆。给她几条「她自己说过的想法/自我认知」，和几条「新知道的事实」，"
    "判断哪些自我认知被这些事实纠正了。只输出 JSON。"
)

SELF_TEMPLATE = """她自己的说法：
{selves}

新知道的事实：
{facts}

哪些「她的说法」被事实纠正了？
- 只有事实直接推翻了那句话里的**事实性判断**才算（例如她说「他讨厌我」，事实是他只是没回消息）
- 只是情绪、喜好、态度（「我有点怕」「我喜欢他」）被事实影响，不算纠正，不要列出
- 拿不准就不要列

只输出 JSON：{{"revised":[{{"id":123,"note":"不超过 25 字，说明事实是什么"}}]}}
"""

SELF_MAX = 3


async def reconcile_self(store, new_texts: list[str], provider) -> list[dict]:
    """事实能不能纠正她的自我认知：只挂旁注，不改她的原话。"""
    import json as _json

    if not new_texts or provider is None:
        return []
    selves = list(store.conn.execute(
        """SELECT t.id, v.text FROM trace t JOIN trace_version v ON v.id=t.head_version_id
           WHERE t.kind='self' AND t.status='active' AND COALESCE(t.source,'live')!='meta'
           ORDER BY t.id DESC LIMIT 8"""))
    if not selves:
        return []

    sblock = "\n".join(f"- #{int(r['id'])} {r['text']}" for r in selves)
    fblock = "\n".join(f"- {t}" for t in new_texts[:8])
    try:
        resp = await provider.text_chat(
            prompt=SELF_TEMPLATE.format(selves=sblock, facts=fblock),
            system_prompt=SELF_SYSTEM, session_id="memory-self", persist=False)
        raw = (getattr(resp, "completion_text", "") or "").strip()
        data = _json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[memory] 自我纠正判定失败：{type(e).__name__}")
        return []

    known = {int(r["id"]) for r in selves}
    fact_id = None
    if new_texts:
        r = store.conn.execute(
            """SELECT t.id FROM trace t JOIN trace_version v ON v.id=t.head_version_id
               WHERE v.text=? ORDER BY t.id DESC LIMIT 1""", (new_texts[0],)).fetchone()
        if r:
            fact_id = int(r["id"])

    done = []
    for u in (data.get("revised") or [])[:SELF_MAX]:
        try:
            sid = int(u.get("id"))
        except (TypeError, ValueError):
            continue
        if sid not in known:
            continue
        note = str(u.get("note") or "").strip()[:40]
        if not note:
            continue
        store.add_note(sid, f"有新事实纠正了这条判断：{note}" + (f"（见 #{fact_id}）" if fact_id else ""),
                       kind="evidence")
        if fact_id:
            store.add_note(fact_id, f"因此纠正了她的说法 #{sid}：{note}", kind="evidence")
        done.append({"id": sid, "note": note})
        logger.info(f"[memory] 事实纠正自我 #{sid}：{note}")
    return done


def _mk_chunks(store, trace_id: int, text: str) -> None:
    """长文本按句建块（向量异步补，检索时没有块就退回整段）。"""
    try:
        n = store.rebuild_chunks(int(trace_id), text or "")
        if n:
            logger.info(f"[memory] #{trace_id} 切成 {n} 块")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[memory] 切块失败：{type(e).__name__}")


def _link_entities(store, trace_id: int, names: list[str]) -> None:
    """把这条记忆提到的实体登记下来（只登记，不建图）。

    默认关闭：实测实体加分对召回没有提升（2026-11-11），
    需要实验时设 AAM_ENTITY=1 再开。
    """
    if os.environ.get("AAM_ENTITY", "0") != "1":
        return
    for name in names or []:
        try:
            eid = store.upsert_entity(name)
            if eid:
                store.add_mention(int(trace_id), eid)
        except Exception:  # noqa: BLE001
            continue


def write_items(store, items: list[dict], lines: list[dict], scope: str, session_id: str) -> int:
    """落库：一个 episode + N 条 trace。返回写入条数。"""
    if not items:
        return 0
    transcript = "\n".join(f"[{m.get('role')}] {m.get('text','')}" for m in lines)
    first_ts = (lines[0].get("created_at") if lines else None) or ""
    last_ts = (lines[-1].get("created_at") if lines else None) or ""
    # 离上一段很近（默认 10 分钟）就并进去，别把一段聊天切成好几段
    reuse = float(os.environ.get("AAM_REUSE_EPISODE_MIN", "10") or 10)
    episode_id = None
    if reuse > 0:
        try:
            last = store.conn.execute(
                """SELECT id, ended_at FROM episode WHERE session_id=? ORDER BY id DESC LIMIT 1""",
                (session_id,)).fetchone()
            if last and last["ended_at"]:
                from datetime import datetime as _dt
                gap_min = (_dt.fromisoformat(last_ts) - _dt.fromisoformat(last["ended_at"])).total_seconds() / 60
                if 0 <= gap_min <= reuse:
                    store.conn.execute(
                        """UPDATE episode SET transcript = transcript || char(10) || ?, ended_at=?
                           WHERE id=?""", (transcript, last_ts, last["id"]))
                    store.conn.commit()
                    episode_id = int(last["id"])
                    logger.info(f"[memory] 并入上一段对话 #{episode_id}（隔 {gap_min:.0f} 分钟）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 复用对话段失败：{type(e).__name__}")
    if episode_id is None:
        episode_id = store.add_episode(
            scope, session_id=session_id, started_at=first_ts, ended_at=last_ts,
            transcript=transcript, source="live", source_ref=session_id,
        )
    n = 0
    for it in items:
        if is_self_noise(it):
            continue
        if store.find_duplicate(it["text"]):
            continue
        near = find_near_duplicate(store, it["text"], it["kind"])
        if near:
            # 同一件事换了个说法：不新建，给旧的追加一版
            store.add_version(
                near, it["text"], reason="reconsolidation", caused_by_type="episode",
                change_class="minor", anchor_quote=it.get("anchor_quote") or "",
                event_time=it.get("event_time") or "", due_at=it.get("due_at") or "",
            due_time=it.get("due_time") or "", recur=it.get("recur") or "",
            topics=it.get("topics") or [],
            felt=it.get("felt") or "", reaction=it.get("reaction") or "",
            inference=it.get("inference") or "",
            inference_certainty=it.get("inference_certainty"),
            )
            _link_entities(store, near, it.get("entities") or [])
            _mk_chunks(store, near, it["text"])
            logger.info(f"[memory] 归并到 #{near}：{it['text'][:20]}")
            continue
        suppress_similar(store, it["text"], it["kind"])
        new_id = store.add_trace(
            it["text"], it["kind"], importance=it["importance"], episode_id=episode_id,
            anchor_quote=it.get("anchor_quote") or "", anchor_time=last_ts,
            event_time=it.get("event_time") or "",
            due_at=it.get("due_at") or "", due_time=it.get("due_time") or "",
            recur=it.get("recur") or "", topics=it.get("topics") or [],
            certainty=it.get("certainty"), tags=it.get("tags") or [],
            felt=it.get("felt") or "", reaction=it.get("reaction") or "",
            inference=it.get("inference") or "",
            inference_certainty=it.get("inference_certainty"),
            source="live", scope=scope,
        )
        _link_entities(store, new_id, it.get("entities") or [])
        _mk_chunks(store, new_id, it["text"])
        try:
            if (float(it.get("importance") or 0) >= 0.65
                    and it.get("kind") in ("self", "unresolved", "promise", "agreement")):
                store.enqueue_trigger("memory", int(new_id), 5)
        except Exception:
            pass
        n += 1
    return n
