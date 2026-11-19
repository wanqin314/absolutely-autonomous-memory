"""记忆插件（M2）：能存、能翻、会注意到。

M2 在 M1 的基础上加了两条：
- 每轮对话把「他说的」和「她答的」收进待处理缓冲
- 规则筛选命中时，交给模型提炼成痕迹落库（六类：fact/observation/speculation/self/unresolved/promise）

仍然是 shadow 模式：只写库，不改上下文、不注入、不干涉。
"""

from __future__ import annotations

import asyncio
import time
import os
import random
import re

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart
from astrbot.core.config.astrbot_config import AstrBotConfig

from . import archivist, noticer, resident, retriever, self_think
from .store import Store, days_between, now_iso

HELP = (
    "记忆（M2）\n"
    "/mem dir 目录\n"
    "/mem show <id> 看一条\n"
    "/mem history <id> 版本链\n"
    "/mem search <词> 搜索\n"
    "/mem touch <id> 模拟想起一次\n"
    "/mem pending 看缓冲里攒了什么\n"
    "/mem flush 立刻提炼一次\n"
    "/mem stats 统计"
)

_WEEKDAYS = "一二三四五六日"


def _fmt_when(value: str) -> str:
    """把 ISO 时间写成「2026-11-11 周三 15:00」；只有日期就不带时刻。"""
    s = (value or "").strip()
    if not s:
        return ""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        wd = _WEEKDAYS[dt.weekday()]
        if dt.hour or dt.minute:
            return f"{dt:%Y-%m-%d} 周{wd} {dt:%H:%M}"
        return f"{dt:%Y-%m-%d} 周{wd}"
    except Exception:  # noqa: BLE001
        return s[:16].replace("T", " ")


def _scene_label(scope: str, session_id: str = "") -> str:
    """场景 + 人物：私聊带对方号，群聊带群号。"""
    sc = (scope or "").strip()
    if sc.startswith("group:"):
        return "群 " + sc.split(":", 1)[1]
    if sc == "private":
        tail = (session_id or "").rsplit(":", 1)[-1]
        return ("私聊 · " + tail) if tail.isdigit() else "私聊"
    return sc or "场景未知"


def _sort_mode(value: str) -> str:
    s = (value or "relevance").strip().lower()
    s = {"按时间": "time", "时间": "time",
         "相关": "relevance", "相关度": "relevance"}.get(s, s)
    return s if s in ("relevance", "time") else ""


def _group_send_capped(cap, used) -> bool:
    """群聊主动发话的每日上限；cap<=0 表示不限。"""
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        cap = 2
    try:
        used = int(used)
    except (TypeError, ValueError):
        used = 0
    return cap > 0 and used >= cap


def _session_map(store, episode_ids) -> dict[int, str]:
    ids = sorted({int(x) for x in episode_ids if x})
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    return {
        int(r["id"]): (r["session_id"] or "")
        for r in store.conn.execute(
            f"SELECT id, session_id FROM episode WHERE id IN ({marks})", ids)
    }


def _filter_series_members(members: list[dict], since: str = "", until: str = "",
                           limit: int = 30, query: str = "") -> tuple[list[dict], int]:
    """按日期/关键词过滤系列成员；返回（要显示的，过滤后总数）。limit<=0 表示全部。"""
    def day(m):
        return str(m.get("anchor_time") or m.get("created_at")
                   or m.get("event_time") or "")[:10]
    q = (query or "").strip().casefold()
    toks = [t for t in q.split() if t]
    out = []
    for m in members:
        d = day(m)
        if since and (not d or d < since):
            continue
        if until and (not d or d > until):
            continue
        if toks:
            text = str(m.get("text") or "").casefold()
            if not all(t in text for t in toks):
                continue
        out.append(m)
    total = len(out)
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = 30
    if n > 0:
        out = out[:n]
    return out, total



WANDER_SYSTEM = (
    "你没有被安排任务。你只是醒了一下，翻到了一些自己的记忆。\n"
    "下面有：你对自己的认识（核心层）、你最近几次醒来时想过什么、以及一条旧记忆。\n"
    "你可以说一句话，也可以什么都不说。沉默是正常的，不需要找理由开口。\n"
    "你现在在 QQ 上，对面是婉清。如果开口，这句话是对她说的，不是自言自语；\n"
    "不要讲系统、记忆机制、插件这些实现细节，除非这件事跟她本人有关。\n"
    "只输出 JSON，不要别的内容：\n"
    '{"decision":"speak 或 silent","source_trace":记忆编号(整数),'
    '"reason":"一句话：为什么说/为什么不说","message":"要说的话，silent 时留空"}'
)


def build_wander_prompt(core, reflections, memory) -> str:
    parts = []
    if core:
        parts.append("【你对自己的认识】\n" + "\n".join(
            f"- {c.get('text','')}" for c in core[:5]))
    if reflections:
        lines = []
        for r in reflections[:5]:
            what = "说了" if r.get("decision") == "speak" else "没说"
            lines.append(f"- {r.get('created_at','')[:16]} {what}：{(r.get('reason') or '')[:60]}")
        parts.append("【你最近几次醒来】\n" + "\n".join(lines))
    if memory:
        parts.append(f"【这次翻到的旧记忆 #{memory.get('id')}（{memory.get('kind')}/{memory.get('st')}）】\n"
                     f"{memory.get('text','')}")
    import time as _t
    parts.append(f"现在是 {_t.strftime('%Y-%m-%d %H:%M')}。你要对婉清说一句吗？还是不说？只输出 JSON。")
    return "\n\n".join(parts)


def parse_wander_reply(text: str) -> dict | None:
    import json as _json
    import re as _re
    if not text:
        return None
    m = _re.search(r"\{.*\}", text, _re.S)
    if not m:
        return None
    try:
        d = _json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(d, dict) or d.get("decision") not in ("speak", "silent"):
        return None
    return d


def _scope_value(value: str):
    """私聊/群聊/群:123456 → private/group/group:123456；空返回 ""；看不懂返回 None。"""
    s = (value or "").strip()
    if not s or s in ("全部", "不限", "都行"):
        return ""
    fixed = {"私聊": "private", "个人": "private", "群聊": "group", "群里": "group"}
    if s in fixed:
        return fixed[s]
    if s in ("private", "group"):
        return s
    if s.startswith("群:"):
        return "group:" + s.split(":", 1)[1].strip()
    if s.startswith("群") and s[1:].isdigit():
        return "group:" + s[1:]
    if s.startswith("group:"):
        return s
    return None


def _date_bound(value: str):
    """把 2026-11-10 / 昨天 / 最近3天 / 7天前 变成 YYYY-MM-DD；空串返回 ""；看不懂返回 None。"""
    import datetime as _dt
    import re as _re
    s = (value or "").strip()
    if not s:
        return ""
    today = _dt.date.today()
    fixed = {"今天": 0, "昨天": 1, "前天": 2, "today": 0, "yesterday": 1}
    if s in fixed:
        return (today - _dt.timedelta(days=fixed[s])).isoformat()
    m = _re.fullmatch(r"最近(\d+)天", s)
    if m:
        return (today - _dt.timedelta(days=max(0, int(m.group(1)) - 1))).isoformat()
    m = _re.fullmatch(r"(\d+)天前", s)
    if m:
        return (today - _dt.timedelta(days=int(m.group(1)))).isoformat()
    try:
        return _dt.date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        return None


_TWO_STAGE_CUES = re.compile(
    r"上次|之前|以前|那天|昨晚|昨天|今夜|记得|还记得|约好|答应|还没|"
    r"后来|那次|我们说过|你忘|想想|回忆|那时候|当时")

_RELATED_WORDS = {
    "晚饭": ["晚餐", "晚上吃", "吃饭", "吃"],
    "晚餐": ["晚饭", "晚上吃", "吃饭", "吃"],
    "午饭": ["午餐", "中午吃", "吃饭", "吃"],
    "早餐": ["早饭", "早上吃", "吃饭", "吃"],
    "早饭": ["早餐", "早上吃", "吃饭", "吃"],
    "夜宵": ["宵夜", "晚上吃", "吃"],
    "跑步": ["晨跑", "夜跑", "运动", "跑"],
    "运动": ["跑步", "锻炼", "跑"],
    "难过": ["伤心", "低落", "想哭", "不开心"],
    "生气": ["不高兴", "恼火", "气"],
}
_NEGATION_MARKS = ("没", "不", "未", "别", "无")


def _related_terms(query: str) -> list[str]:
    """同义词/类别词；出现否定词就完全不扩，避免把“没吃”扩成“吃”。"""
    q = (query or "").strip()
    if not q or any(x in q for x in _NEGATION_MARKS):
        return []
    out: list[str] = []
    for k, vals in _RELATED_WORDS.items():
        if k in q:
            for v in vals:
                if v not in out and v not in q:
                    out.append(v)
    return out[:6]


def _meal_fallback_needed(query: str) -> bool:
    q = (query or "")
    return any(x in q for x in ("晚饭", "午餐", "午饭", "早饭", "早餐",
                                 "夜宵", "吃了什么", "吃什么", "饭"))


def _find_person_impression(base_dir: str, name: str = "", qq: str = "") -> dict | None:
    """只读 nest 的人物印象 JSON；匹配 name 或 qq_id。"""
    import glob
    import json as _json
    from urllib.parse import unquote

    path = os.path.join(base_dir, "nest_diary", "modules", "impressions", "people")
    if not os.path.isdir(path):
        return None
    name = (name or "").strip()
    qq = str(qq or "").strip()
    for fp in glob.glob(os.path.join(path, "*.json")):
        try:
            with open(fp, encoding="utf-8-sig") as f:
                d = _json.load(f)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        if qq and str(d.get("qq_id") or "") == qq:
            return d
        if name and (str(d.get("name") or "") == name or unquote(os.path.basename(fp)) == name + ".json"):
            return d
    return None


def _raw_match_query(query: str) -> str:
    """把 query 变成原话 FTS 的 OR 词；去掉时间词，避免整库命中。"""
    import re as _re

    q = (query or "").strip()
    if not q:
        return ""
    stop = {"昨晚", "昨天", "今天", "今日", "前天", "上周", "最近", "什么",
            "的", "了", "吗", "呢", "啊", "我", "你", "她", "他"}
    words = [w.strip("，。！？、,.!?;；:：") for w in _re.split(r"\s+", q)]
    words = [w for w in words if len(w) >= 2 and w not in stop]
    if not words:
        words = [q] if len(q) >= 3 else []
    return " OR ".join('"' + w.replace('"', '""') + '"' for w in words[:6])


def _self_terms(query: str) -> list[str]:
    import re as _re
    stop = {"昨晚", "昨天", "今天", "今日", "前天", "上周", "最近", "什么",
            "的", "了", "吗", "呢", "啊", "我", "你", "她", "他", "自己"}
    words = [w.strip("，。！？、,.!?;；:：") for w in _re.split(r"\s+", (query or "").strip())]
    out = [w for w in words if len(w) >= 2 and w not in stop][:6]
    # 中文常常没有空格：补最后几个二字词，别拿整句去 LIKE。
    if len(out) == 1 and len(out[0]) > 4:
        q = out[0]
        grams = [q[i:i + 2] for i in range(len(q) - 1)]
        grams = [g for g in grams if len(g) == 2 and g not in stop]
        out = [q] + grams[-4:]
    return out[:6]


def _auto_time_bounds(query: str, since: str, until: str) -> tuple[str, str]:
    """query 里写了昨晚/昨天/今天/前天/上周/最近N天，就自动补时间窗。

    显式传了 since/until 时不动；这是给模型漏传参数的兜底。
    """
    import re as _re
    from datetime import date as _date, timedelta as _td

    if since or until:
        return since, until
    q = (query or "").strip()
    if not q:
        return since, until
    today = _date.today()

    if _re.search(r"昨晚|昨天晚上|昨夜", q):
        d = (today - _td(days=1)).isoformat()
        return d, d
    if _re.search(r"昨天|昨日", q):
        d = (today - _td(days=1)).isoformat()
        return d, d
    if _re.search(r"今天|今日", q):
        d = today.isoformat()
        return d, d
    if _re.search(r"前天", q):
        d = (today - _td(days=2)).isoformat()
        return d, d
    if _re.search(r"上周|上个星期|上星期", q):
        monday = today - _td(days=today.weekday() + 7)
        return monday.isoformat(), (monday + _td(days=6)).isoformat()
    m = _re.search(r"最近\s*(\d+)\s*天", q)
    if m:
        n = max(1, int(m.group(1)))
        return (today - _td(days=n - 1)).isoformat(), today.isoformat()
    return since, until


@register("astrbot_plugin_absolutely_autonomous_memory", "婉清",
         "绝对自主记忆：原文只追加、痕迹带版本链、她自己翻、事实能了结约定", "v0.43.0")

class DafeiyuMemory(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        path = (config.get("db_path") if config else "") or ""
        if not path:
            base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "plugin_data",
                                                "astrbot_plugin_absolutely_autonomous_memory"))
            path = os.path.join(base, "memory.db")
        self.db_path = path
        self.store: Store | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._enabled = True
        self._vec_task: asyncio.Task | None = None
        self._decay_task: asyncio.Task | None = None
        self._sweep_task: asyncio.Task | None = None
        self._topics_task: asyncio.Task | None = None
        self._due_task: asyncio.Task | None = None
        self._topics_done: str = ""
        self._last_imp: dict[str, tuple[float, float, str]] = {}
        self._last_recall: dict[str, dict] = {}
        self._self_thinker = self_think.SelfThinker(self)

    # ---------- 基础 ----------

    def _db(self) -> Store:
        if self.store is None:
            self.store = Store(self.db_path)
            return self.store
        try:
            self.store.conn.execute("SELECT 1")
        except Exception:
            # 连接被谁关掉了：重开一条，别让整个插件瘫掉
            try:
                self.store.close()
            except Exception:
                pass
            self.store = Store(self.db_path)
        return self.store

    def _cfg(self, key, default):
        try:
            v = self.conf.get(key) if self.conf else None
            return default if v is None or v == "" else v
        except Exception:
            return default

    def _lock(self, session: str) -> asyncio.Lock:
        if session not in self._locks:
            self._locks[session] = asyncio.Lock()
        return self._locks[session]

    @staticmethod
    def _scope(event: AstrMessageEvent) -> str:
        try:
            if event.get_message_type().value == "GroupMessage":
                gid = event.get_group_id() or "unknown"
                return f"group:{gid}"
        except Exception:
            pass
        return "private"

    def _ensure_vectors_thread(self, limit: int) -> int:
        st = Store(self.db_path)
        try:
            return retriever.ensure_vectors(st, None, limit)
        finally:
            st.close()

    async def _warm_vectors(self):
        try:
            await asyncio.sleep(8)
            total = 0
            for _ in range(8):
                n = await asyncio.to_thread(self._ensure_vectors_thread, 120)
                total += n
                if n == 0:
                    break
            logger.info(f"[memory] 向量补齐 {total} 条")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 补向量出错：{type(e).__name__}: {e}")

    def _sync_missing_vectors(self, limit: int = 6) -> None:
        """每轮顺手补几条，保证新痕迹也能被向量召回（同步、独立连接）。"""
        try:
            self._ensure_vectors_thread(limit)
        except Exception:
            pass

    async def initialize(self):
        """插件加载完成：建表，按配置启动自我思考。"""
        try:
            self._db()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 初始化数据库失败：{type(e).__name__}: {e}")
        if bool(self._cfg("self_think_enabled", False)):
            self._self_thinker.start()

    async def terminate(self):
        # 重载时把后台循环停掉，否则旧实例的循环会一直跑（重复走神/重复提醒）
        try:
            if getattr(self, "_self_thinker", None):
                _t = getattr(self._self_thinker, "_task", None)
                if _t is not None:
                    _t.cancel()
                self._self_thinker = None
        except Exception:
            pass
        for _name in ("_wander_task", "_due_task", "_sweep_task", "_decay_task",
                      "_topics_task", "_vec_task"):
            _t = getattr(self, _name, None)
            if _t is not None:
                try:
                    _t.cancel()
                except Exception:
                    pass
                try:
                    setattr(self, _name, None)
                except Exception:
                    pass
        if self.store:
            self.store.close()
            self.store = None

    # ---------- M2：注意到 ----------

    @filter.on_llm_response()
    async def on_response(self, event: AstrMessageEvent, response):
        if not self._enabled:
            return
        try:
            _u = getattr(response, "usage", None)
            if _u is not None:
                logger.info(f"[memory] 主回复 usage：cached={getattr(_u, 'input_cached', 0)} "
                            f"other={getattr(_u, 'input_other', 0)} out={getattr(_u, 'output', 0)}")
            store = self._db()
            session = event.unified_msg_origin
            scope = self._scope(event)
            user_text = (event.message_str or "").strip()
            reply = getattr(response, "completion_text", "") or ""
            if not user_text and not reply:
                return
            gap = 0.0
            last = store.last_pending_at(session)
            if last:
                gap = days_between(last, now_iso()) * 1440.0
            uname = ""
            try:
                uname = str(event.get_sender_name() or "").strip()
            except Exception:
                uname = ""
            store.add_pending(session, "user", user_text, scope, sender=uname)
            store.add_pending(session, "assistant", reply, scope, sender="她")
            # 原始对话归档：只追加，不进检索、不进上下文
            try:
                sid = ""
                try:
                    sid = str(event.get_sender_id() or "")
                except Exception:
                    pass
                store.add_raw(session, "user", user_text, scope, sender=sid)
                store.add_raw(session, "assistant", reply, scope)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[memory] 原话归档失败：{type(e).__name__}: {e}")
            pending = store.pending_count(session)
            base = int(self._cfg("batch_size", 4))
            # 聊得热就多攒几行（省调用），安静了赶紧收（少等）
            if gap >= 180:
                eff_batch = max(2, base // 2)
            elif gap <= 30:
                eff_batch = base * 2
            else:
                eff_batch = base
            reason = noticer.check(
                user_text, pending, gap,
                batch_size=eff_batch,
                idle_threshold=float(self._cfg("idle_minutes", 10)),
            )
            if reason:
                asyncio.create_task(self._flush(session, scope, reason))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 收集消息出错：{type(e).__name__}: {e}")

    async def _flush(self, session: str, scope: str, reason: str = "manual"):
        async with self._lock(session):
            try:
                store = self._db()
                lines = store.take_pending(session)
                if not lines:
                    return 0
                line_ids = [int(m["id"]) for m in lines if m.get("id")]
                prov = None
                pid = str(self._cfg("provider_id", "") or "")
                try:
                    prov = self.context.get_provider_by_id(pid) if pid else None
                except Exception:
                    prov = None
                if prov is None:
                    prov = self.context.get_using_provider(umo=session)
                if prov is None:
                    logger.warning(
                        f"[memory] 没有可用的对话模型，{len(lines)} 行退回缓冲稍后再试"
                    )
                    store.restore_pending(line_ids)
                    return 0
                items, updates = await archivist.extract(prov, lines, store)
                n = archivist.write_items(store, items, lines, scope, session)
                item_texts = [i["text"] for i in items]
                archivist.apply_updates(store, updates, item_texts, session)
                facts = [i["text"] for i in items if i["kind"] in ("fact", "observation")]
                if facts:
                    await archivist.resolve_conflicts(store, facts, prov, session,
                                              limit=int(self._cfg("resolve_max_per_run", 5)))
                    if bool(self._cfg("self_reconcile_enabled", True)):
                        try:
                            await archivist.reconcile_self(store, facts, prov)
                        except Exception:
                            pass
                if n:
                    await asyncio.to_thread(self._ensure_vectors_thread, 12)
                logger.info(f"[memory] 落痕 {n} 条（触发：{reason}，缓冲 {len(lines)} 行）")
                return n
            except Exception as e:  # noqa: BLE001
                try:
                    if isinstance(e, archivist.ParseError):
                        logger.warning(
                            f"[memory] 提炼返回空/坏结果，{len(lines)} 行退回缓冲稍后再试：{e}"
                        )
                    else:
                        logger.exception(
                            f"[memory] 提炼落库出错：{type(e).__name__}: {e}"
                        )
                except Exception:
                    logger.error(f"[memory] 提炼落库出错：{type(e).__name__}: {e}")
                try:
                    _ids = locals().get("line_ids") or []
                    _st = locals().get("store")
                    if _ids and _st is not None:
                        n_back = _st.restore_pending(_ids)
                        if n_back:
                            logger.info(f"[memory] 已把 {n_back} 行对话还回缓冲，下次再试")
                except Exception:
                    pass
                return 0



    # ---------- M4：衰减 ----------

    def _decay_once(self) -> int:
        st = Store(self.db_path)
        try:
            return st.apply_idle_decay()
        finally:
            st.close()

    async def _due_tick(self, first: bool = False) -> None:
        """到点的约定 → 生成提醒；提醒过还没处理 → 转未了。

        first=True 时补上停机期间错过的（不做时间窗限制）。
        """
        st = Store(self.db_path)
        try:
            for r in st.due_promises(window_min=5, catch_up=first):
                text = f"到了说好的时候：{r['text'][:80]}"
                if st.add_reminder(int(r["id"]), r["session_id"] or "", text):
                    logger.info(f"[memory] 约定到点 #{r['id']}")
                    if r.get("recur"):
                        nxt = st.advance_recurring(int(r["id"]))
                        logger.info(f"[memory] 循环约定 #{r['id']} 下一次 {nxt}")
            try:
                fixed = st.advance_past_recurring()
                if fixed:
                    logger.info(f"[memory] 循环约定补推进：{fixed}")
            except Exception:
                pass
            missed = st.miss_overdue_reminders(grace_min=30)
            if missed:
                logger.info(f"[memory] 约定过点转未了：{missed}")
        finally:
            st.close()

    def _quiet_now(self) -> bool:
        """静默时段（默认 1-7 点）不发主动消息。"""
        try:
            spec = str(self._cfg("proactive_reminder_quiet_hours", "1-7") or "").strip()
            if not spec:
                return False
            h = time.localtime().tm_hour
            for part in spec.split(","):
                part = part.strip()
                if "-" in part:
                    a, b = part.split("-", 1)
                    if int(a) <= h < int(b):
                        return True
                elif part.isdigit() and int(part) == h:
                    return True
        except Exception:
            return False
        return False

    async def _compose_proactive(self, session: str, rows: list[dict]) -> str:
        """让她用平时的语气说一句，把到点的事带出来。"""
        prov = None
        try:
            pid = str(self._cfg("provider_id", "") or "")
            prov = self.context.get_provider_by_id(pid) if pid else None
            if prov is None:
                prov = self.context.get_using_provider()
        except Exception:
            prov = None
        if prov is None:
            return ""
        st = Store(self.db_path)
        try:
            style = st.recent_replies(session, 3)
        finally:
            st.close()
        style_txt = "\n".join(f"- {x}" for x in style) or "（没有可参考的）"
        items = "；".join(r["text"][:120] for r in rows[:2])
        prompt = (
            f"你心里记挂着这些事：{items}\n\n"
            "现在他那边安静了一阵子。用你平时的语气，主动跟他说一句，把这件事带出来。\n"
            "要求：一句话，口语，不解释你为什么想起，不提“提醒/系统/记录”，不要罗列。"
        )
        system = ("你就是你自己，一个会主动开口的人。说话短、口语化，不用书面语。\n"
                  f"你以前说过的话（参考语气，不要照抄）：\n{style_txt}")
        try:
            resp = await prov.text_chat(prompt=prompt, system_prompt=system,
                                        session_id=session, persist=False)
            text = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 主动生成失败：{type(e).__name__}: {e}")
            return ""
        return text[:400]


    def _quiet_hours(self, spec: str) -> bool:
        try:
            h = time.localtime().tm_hour
            for part in (spec or "").split(","):
                part = part.strip()
                if "-" in part:
                    a, b = part.split("-", 1)
                    if int(a) <= h < int(b):
                        return True
                elif part.isdigit() and int(part) == h:
                    return True
        except Exception:
            return False
        return False

    async def _wander_loop(self):
        last_heartbeat = time.time()
        await asyncio.sleep(90)
        while True:
            try:
                if bool(self._cfg("wander_enabled", False)):
                    st = Store(self.db_path)
                    try:
                        trigs = st.due_triggers(2)
                    finally:
                        st.close()
                    kind_prob = {
                        "return": float(self._cfg("wander_prob_return", 0.8)),
                        "memory": float(self._cfg("wander_prob_memory", 0.5)),
                        "stale": float(self._cfg("wander_prob_stale", 0.25)),
                        "idle": float(self._cfg("wander_prob_idle", 0.4)),
                    }
                    for tr in trigs:
                        kind = str(tr.get("kind") or "")
                        if random.random() <= kind_prob.get(kind, 0.3):
                            res = await self._wander_once(
                                session=str(tr.get("session_id") or ""),
                                seed_trace=int(tr.get("trace_id") or 0), trigger=kind)
                            if res and res.get("decision"):
                                logger.info(f"[memory] 走神[{kind}]：{res.get('decision')}"
                                            f" · {str(res.get('reason'))[:40]}")
                            elif res and res.get("skipped"):
                                logger.info(f"[memory] 走神[{kind}]：跳过（{res.get('skipped')}）")
                        else:
                            logger.info(f"[memory] 走神[{kind}]：概率没过，跳过")
                        st = Store(self.db_path)
                        try:
                            st.finish_trigger(int(tr["id"]))
                        finally:
                            st.close()
                    try:
                        interval = max(5, int(self._cfg("wander_interval_min", 180) or 180))
                    except Exception:
                        interval = 180
                    if not trigs and time.time() - last_heartbeat >= interval * 60:
                        last_heartbeat = time.time()
                        res = await self._wander_once(trigger="heartbeat")
                        if res and res.get("decision"):
                            logger.info(f"[memory] 走神[heartbeat]：{res.get('decision')}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[memory] 走神失败：{type(e).__name__}: {e}")
            await asyncio.sleep(60)

    def _last_user_at(self, session: str) -> str:
        st = Store(self.db_path)
        try:
            return st.last_user_message_at(session)
        finally:
            st.close()

    def _mins_since(self, iso: str) -> float | None:
        if not iso:
            return None
        try:
            from datetime import datetime
            t = datetime.fromisoformat(iso)
            now = datetime.now(t.tzinfo) if t.tzinfo else datetime.now()
            return (now - t).total_seconds() / 60.0
        except Exception:
            return None

    async def _wander_once(self, session: str = "", provider=None,
                           send_func=None, force: bool = False,
                           seed_trace: int = 0, trigger: str = ""):
        """醒一次：翻一条旧记忆，决定说还是不说。force 只跳过概率门，睡觉/上限/空闲照旧。"""
        if not force and not bool(self._cfg("wander_enabled", False)):
            return None
        st = Store(self.db_path)
        try:
            session = (session or str(self._cfg("wander_session", "") or "")).strip()
            if not session:
                r = st.conn.execute(
                    """SELECT session_id FROM raw_message WHERE session_id LIKE '%FriendMessage%'
                       ORDER BY id DESC LIMIT 1""").fetchone()
                session = (r[0] if r else "") or ""
            if not session:
                return {"skipped": "no_session"}
            if "GroupMessage" in session:
                return {"skipped": "group"}
            if self._quiet_hours(str(self._cfg("wander_quiet_hours", "1-7") or "")):
                return {"skipped": "quiet"}
            # 你刚说过话就别插嘴
            idle_min = float(self._cfg("wander_idle_minutes", 30))
            idle = self._mins_since(st.last_user_message_at(session))
            if idle is not None and idle < idle_min:
                return {"skipped": "idle"}
            cap = int(self._cfg("wander_max_per_day", 3))
            if st.reflections_today("speak") >= cap:
                return {"skipped": "cap"}
            thoughts_cap = int(self._cfg("wander_max_thoughts_per_day", 6))
            if st.thoughts_today() >= thoughts_cap:
                return {"skipped": "thought_cap"}
            try:
                p = float(self._cfg("wander_probability", 0.25))
            except Exception:
                p = 0.25
            if not force and random.random() > p:
                return {"skipped": "probability"}
            reflected = st.recent_reflected_traces(7)
            memory = None
            # 80%：事件给的种子 → 相关候选里随机挑一条；20%：完全随机
            if seed_trace and random.random() >= 0.2:
                seed = st.trace_brief(int(seed_trace))
                if seed and int(seed["id"]) not in reflected:
                    try:
                        hits = await asyncio.to_thread(
                            self._search_in_thread, seed["text"][:200], 5, "")
                    except Exception:
                        hits = []
                    cand = []
                    for h in hits:
                        try:
                            if int(h["id"]) in reflected:
                                continue
                            b = st.trace_brief(int(h["id"]))
                            if b and b.get("st") not in ("settled", "void"):
                                cand.append(b)
                        except Exception:
                            continue
                    if cand:
                        memory = random.choice(cand)
            if not memory:
                memory = st.random_memory_for_wander(exclude=reflected)
            if not memory:
                return {"skipped": "no_memory"}
            core = st.core_entries(5)
            refl = st.reflections_recent(5)
            prompt = build_wander_prompt(core, refl, memory)
            if provider is None:
                pid = str(self._cfg("wander_provider_id", "") or "")
                if not pid:
                    try:
                        pid = await self.context.get_current_chat_provider_id(session) or ""
                    except Exception:
                        pid = ""
                provider = self.context.get_provider_by_id(pid) if pid else None
            if provider is None:
                return {"skipped": "no_provider"}
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt=WANDER_SYSTEM,
                persist=False)
            text = (getattr(resp, "completion_text", "") or "").strip()
            parsed = parse_wander_reply(text)
            if not parsed:
                st.add_reflection(session, [memory["id"]], "invalid",
                                  "回复不是有效 JSON", "", None, trigger=trigger)
                return {"decision": "invalid"}
            decision = parsed.get("decision")
            reason = str(parsed.get("reason") or "")[:200]
            src = parsed.get("source_trace")
            try:
                src = int(src)
            except Exception:
                src = None
            # 必须指回眼前这条记忆，否则算幻觉，丢掉
            if src is None or src != int(memory["id"]) or st.trace_brief(src) is None:
                st.add_reflection(session, [memory["id"]], "discard",
                                  "说的不是眼前这条" + (f"：{reason}" if reason else ""), "", None)
                return {"decision": "discard"}
            if decision == "silent":
                st.add_reflection(session, [memory["id"]], "silent", reason, "", src, trigger=trigger)
                return {"decision": "silent", "reason": reason, "source_trace": src}
            msg = str(parsed.get("message") or "").strip()
            if not msg:
                st.add_reflection(session, [memory["id"]], "discard", "决定说但没话", "", src)
                return {"decision": "discard"}
            if bool(self._cfg("wander_dry_run", False)) and send_func is None:
                st.add_reflection(session, [memory["id"]], "rehearsal",
                                  "（演练，未发送）" + reason, msg[:800], src,
                                  trigger=trigger)
                logger.info(f"[memory] 走神演练（未发送）：{msg[:100]}")
                return {"decision": "speak", "dry_run": True, "reason": reason,
                        "message": msg, "source_trace": src}
            if send_func is not None:
                await send_func(msg)
            else:
                from astrbot.api.event import MessageChain
                from astrbot.api.message_components import Plain
                await self.context.send_message(session, MessageChain([Plain(msg[:800])]))
            st.add_reflection(session, [memory["id"]], "speak", reason, msg[:800], src,
                              trigger=trigger)
            return {"decision": "speak", "reason": reason, "message": msg, "source_trace": src}
        finally:
            st.close()

    async def _proactive_reminders(self) -> None:
        """排队太久还没机会说的到点提醒 → 她主动发一条。"""
        if not bool(self._cfg("proactive_reminder_enabled", True)):
            return
        if self._quiet_now():
            return
        idle = float(self._cfg("proactive_reminder_idle_minutes", 30) or 30)
        st = Store(self.db_path)
        try:
            rows = st.pending_recurring_reminders(limit=2)
            rows += st.pending_reminders_older(idle, limit=3)
            _seen = set()
            rows = [r for r in rows if not (r["id"] in _seen or _seen.add(r["id"]))]
            if not rows:
                return
            by_session: dict[str, list[dict]] = {}
            for r in rows:
                by_session.setdefault(r["session_id"] or "", []).append(r)
            for session, group in by_session.items():
                if not session:
                    continue
                _rr = st.conn.execute(
                    "SELECT recur FROM trace WHERE id=?", (int(group[0]["trace_id"]),)).fetchone()
                _is_recur = bool(_rr and _rr["recur"])
                if _is_recur:
                    text = await self._compose_proactive(session, group)
                    if text:
                        try:
                            from astrbot.api.event import MessageChain
                            from astrbot.api.message_components import Plain
                            ok = await self.context.send_message(
                                session, MessageChain([Plain(text)]))
                        except Exception as e:  # noqa: BLE001
                            logger.warning(f"[memory] 循环约定发送失败：{type(e).__name__}: {e}")
                            continue
                        if ok:
                            st.mark_reminder_delivered([int(r["id"]) for r in group])
                            st.advance_recurring(int(group[0]["trace_id"]))
                            logger.info(f"[memory] 循环约定主动发出：{text[:40]}")
                    continue
                last = st.last_activity(session)
                try:
                    from datetime import datetime
                    now = datetime.now().astimezone()
                    if last:
                        dt = datetime.fromisoformat(last)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=now.tzinfo)
                        if (now - dt).total_seconds() < idle * 60:
                            continue   # 他刚说过话，别插嘴
                except Exception:
                    pass
                text = await self._compose_proactive(session, group)
                if not text:
                    continue
                try:
                    from astrbot.api.event import MessageChain
                    from astrbot.api.message_components import Plain
                    ok = await self.context.send_message(session, MessageChain([Plain(text)]))
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[memory] 主动发送失败：{type(e).__name__}: {e}")
                    continue
                if ok:
                    st.mark_reminder_delivered([int(r["id"]) for r in group])
                    logger.info(f"[memory] 主动发出：{text[:40]}（提醒 {[r['id'] for r in group]}）")
        finally:
            st.close()

    async def _stale_tick(self) -> None:
        """每天一次：把挂太久的事转成沉默档。"""
        import time as _t
        today = _t.strftime("%Y-%m-%d")
        st = Store(self.db_path)
        try:
            if st.kv_get("stale_day") == today:
                return
            ids = st.stale_candidates(int(self._cfg("stale_days", 7) or 7))
            n = st.mark_stale(ids, "stale")
            if n:
                logger.info(f"[memory] 转入沉默档 {n} 条：{ids[:8]}")
            old_ids = st.old_candidates(int(self._cfg("old_days", 30) or 30))
            m = st.mark_stale(old_ids, "old")
            dorm = st.dormant_candidates(float(self._cfg("dormant_after_days", 7) or 7))
            d = st.mark_dormant(dorm)
            st.kv_set("stale_day", today)
            if m:
                logger.info(f"[memory] 转入旧事 {m} 条：{old_ids[:8]}")
            if d:
                logger.info(f"[memory] 转入休眠 {d} 条：{dorm[:8]}")
        finally:
            st.close()

    async def _due_loop(self):
        """每 60 秒看一次约定；第一次运行时补停机期间错过的。"""
        await asyncio.sleep(20)
        first = True
        while True:
            try:
                await self._due_tick(first=first)
                try:
                    await self._stale_tick()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[memory] 沉默档检查出错：{type(e).__name__}: {e}")
                try:
                    await self._proactive_reminders()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[memory] 主动提醒出错：{type(e).__name__}: {e}")
                first = False
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[memory] 约定检查出错：{type(e).__name__}: {e}")
            await asyncio.sleep(60)

    async def _sweep_loop(self):
        """每 60 秒看一眼：有没有会话躺在缓冲里超过 idle 分钟没动静。"""
        await asyncio.sleep(30)
        while True:
            try:
                idle = float(self._cfg("idle_minutes", 10))
                st = Store(self.db_path)
                try:
                    stale = st.stale_sessions(idle)
                finally:
                    st.close()
                # 顺手清掉过期的原话
                try:
                    days = int(self._cfg("raw_retention_days", 0) or 0)
                    st2 = Store(self.db_path)
                    try:
                        n = st2.raw_purge(days)
                        st2.notice_purge(180)
                    finally:
                        st2.close()
                    if n:
                        logger.info(f"[memory] 清理过期原话 {n} 条（保留 {days} 天）")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[memory] 原话清理出错：{type(e).__name__}: {e}")
                for session, scope in stale:
                    if self._lock(session).locked():
                        continue
                    logger.info(f"[memory] 空闲收尾：{session[:28]} 超过 {idle:.0f} 分钟")
                    asyncio.create_task(self._flush(session, scope or "private", "idle_sweep"))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[memory] 空闲扫描出错：{type(e).__name__}: {e}")
            await asyncio.sleep(60)

    async def _topics_loop(self):
        """每晚一次：把她今天翻过、还没主题的旧记忆拿出来，让她自己起名。"""
        await asyncio.sleep(60)
        while True:
            try:
                if bool(self._cfg("topics_review_enabled", True)):
                    hour = int(self._cfg("topics_review_hour", 23) or 23)
                    minute = int(self._cfg("topics_review_minute", 0) or 0)
                    lt = time.localtime()
                    due = (lt.tm_hour, lt.tm_min) >= (hour, minute)
                    today = time.strftime("%Y-%m-%d")
                    _st_kv = Store(self.db_path)
                    try:
                        done_day = _st_kv.kv_get("topics_review_day")
                    finally:
                        _st_kv.close()
                    if due and done_day != today:
                        n = await self._topics_review_once()
                        if n is not None:
                            self._topics_done = today
                            _st2 = Store(self.db_path)
                            try:
                                _st2.kv_set("topics_review_day", today)
                            finally:
                                _st2.close()
                            logger.info(f"[memory] 夜间主题复盘：给 {n} 条旧记忆起了名")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[memory] 主题复盘出错：{type(e).__name__}: {e}")
            await asyncio.sleep(600)

    async def _topics_review_once(self):
        st = Store(self.db_path)
        try:
            rows = st.recalled_without_topics(int(self._cfg("topics_review_count", 5) or 5))
        finally:
            st.close()
        if not rows:
            return 0
        prov = None
        try:
            pid = str(self._cfg("provider_id", "") or "")
            prov = self.context.get_provider_by_id(pid) if pid else None
            if prov is None:
                prov = self.context.get_using_provider()
        except Exception:
            prov = None
        if prov is None:
            return None
        items = "\n".join(f"#{r['id']}｜{r['text'][:120]}" for r in rows)
        try:
            resp = await prov.text_chat(
                prompt=archivist.TOPIC_REVIEW_TPL.format(items=items),
                system_prompt="你在回看自己的旧记忆。只输出 JSON。",
                session_id="memory-topics", persist=False)
            raw = (getattr(resp, "completion_text", "") or "").strip()
            data = __import__("json").loads(raw[raw.find("{"): raw.rfind("}") + 1])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 主题复盘调用失败：{type(e).__name__}")
            return None
        got = {}
        for x in (data.get("items") or []):
            if isinstance(x, dict) and str(x.get("id", "")).isdigit():
                tp = archivist._clean_topics(x.get("topics"))
                if tp:
                    got[int(x["id"])] = tp
        if not got:
            return 0
        await asyncio.to_thread(self._apply_topics, got)
        return len(got)

    def _apply_topics(self, mapping: dict) -> None:
        st = Store(self.db_path)
        try:
            for tid, tp in mapping.items():
                if not st.set_topics(int(tid), tp):
                    continue
                row = st.trace_row(int(tid))
                v = st.conn.execute("SELECT text, topics FROM trace_version WHERE id=?",
                                    (row["head_version_id"],)).fetchone()
                vec = retriever.embed_sync(Store.index_text(v["text"], v["topics"]))
                if vec:
                    st.conn.execute(
                        """INSERT OR REPLACE INTO trace_vec(trace_id, vec, dim, model, updated_at)
                           VALUES (?,?,?,?,datetime('now'))""",
                        (int(tid), retriever.pack(vec), len(vec), "bge-m3"))
            st.conn.commit()
        finally:
            st.close()

    async def _decay_loop(self):
        hours = float(self._cfg("decay_interval_hours", 6) or 6)
        logger.info(f"[memory] 衰减循环进入，间隔 {hours} 小时")
        await asyncio.sleep(20)  # 启动后缓一下，别跟开机抢
        while True:
            try:
                n = await asyncio.to_thread(self._decay_once)
                logger.info(f"[memory] 衰减检查：下调 {n} 条")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[memory] 衰减出错：{type(e).__name__}: {e}")
            await asyncio.sleep(max(1.0, hours) * 3600)

    # ---------- 线程内独立连接（SQLite 不能跨线程共用） ----------

    def _search_in_thread(self, query: str, limit: int = 5, kind: str = "",
                          since: str = "", until: str = "",
                          scope: str = "", person: str = ""):
        st = Store(self.db_path)
        try:
            return retriever.search(st, query, limit, kind, since=since, until=until,
                                    scope=scope, person=person)
        finally:
            st.close()

    def _recent_in_thread(self, kind: str, limit: int, since: str, until: str,
                          scope: str, person: str):
        st = Store(self.db_path)
        try:
            return st.recent_traces(kind, limit, since, until, scope, person)
        finally:
            st.close()

    def _self_thoughts_in_thread(self, terms: list[str], limit: int, session: str,
                                 since: str, until: str, recent: bool = False):
        st = Store(self.db_path)
        try:
            if recent and not terms:
                return st.self_thoughts_recent(limit, session)
            return st.self_thoughts_search(terms, limit, session, since, until)
        finally:
            st.close()

    def _impression_in_thread(self, text: str):
        st = Store(self.db_path)
        try:
            return retriever.impression(st, text)
        finally:
            st.close()

    # ---------- M3：印象与回想 ----------

    def _related_in_thread(self, text: str, session: str) -> list[dict]:
        """在独立线程/连接里查「可能相关的挂起项」。"""
        st = Store(self.db_path)
        try:
            return retriever.related_open_items(st, text, session, 0.55, 3)
        finally:
            st.close()

    # ---------- 她自己放的常驻区 ----------

    def _resident_scope_keys(self, event) -> list[str]:
        return resident.scope_keys(event)

    def _resolve_resident_scope(self, event, scope: str) -> str:
        return resident.resolve_scope(event, scope)

    def _resident_block(self, event) -> str:
        return resident.render(self._db().resident_active(self._resident_scope_keys(event)))

    def _tail_has(self, req, marker: str) -> bool:
        for p in (getattr(req, "extra_user_content_parts", None) or []):
            text = p.get("text", "") if isinstance(p, dict) else getattr(p, "text", "")
            if marker in str(text or ""):
                return True
        return False

    def _append_tail(self, req, text: str) -> None:
        if not text:
            return
        parts = list(getattr(req, "extra_user_content_parts", None) or [])
        if any(str((p.get("text", "") if isinstance(p, dict) else getattr(p, "text", "")) or "") == text
               for p in parts):
            return
        parts.append(TextPart(text=text))
        req.extra_user_content_parts = parts

    def _push_resident(self, req, event) -> int:
        block = self._resident_block(event)
        if not block:
            return 0
        if self._tail_has(req, "【你自己放在常驻区的】"):
            return 0
        # 动态材料放尾部：system/历史前缀保持不变，缓存才吃得到。
        self._append_tail(req, "\n\n" + block)
        return len(block)

    # ---------- 她自己分的系列 ----------

    def _series_scope(self, event, scope: str) -> str:
        s = (scope or "auto").strip().lower()
        if s in ("auto", "自动", "当前", "这里"):
            gid = str(event.get_group_id() or "")
            if gid:
                return "group:" + gid
            sid = str(event.get_sender_id() or "")
            if sid:
                return "private:" + sid
            return "global"
        return resident.resolve_scope(event, s)

    @staticmethod
    def _series_scope_label(scope: str) -> str:
        sc = (scope or "").strip()
        if sc == "global":
            return "全局"
        if sc.startswith("group:"):
            return "群 " + sc.split(":", 1)[1]
        if sc.startswith("private:"):
            return "私聊 · " + sc.split(":", 1)[1]
        return sc or "未知场景"

    def _configure_embedding(self) -> None:
        """把配置里的向量服务交给 retriever，并做一次健康检查（只做一次）。"""
        try:
            retriever.configure(
                url=str(self._cfg("embedding_url", "") or ""),
                model=str(self._cfg("embedding_model", "") or ""),
                mode=str(self._cfg("embedding_mode", "") or ""),
                key=str(self._cfg("embedding_api_key", "") or ""),
            )
            st = retriever.embedding_status()
            probe = retriever.embed_sync("ping")
            if probe:
                logger.info(f"[memory] 向量服务：{st['mode']} · {st['model']} · {st['url']}")
            else:
                logger.warning(
                    f"[memory] 向量服务不可用（{st['url']}）——检索会降级为关键词模式；"
                    "装 Ollama 并 ollama pull bge-m3，或改 embedding_* 配置")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 向量配置失败：{type(e).__name__}: {e}")

    @filter.on_llm_request()
    async def on_request(self, event: AstrMessageEvent, req):
        """只递「印象」：强度、数量、类别，不含内容、不带动词。"""
        try:
            answered = self._self_thinker.touch(event)
        except Exception:
            answered = False
        if answered:
            try:
                event.stop_event()
            except Exception:
                pass
            logger.info("[memory] 这是自我思考在等回答：先不回复，留给下一次思考")
            return
        if self._vec_task is None and bool(self._cfg("vectors_enabled", True)):
            self._vec_task = asyncio.create_task(self._warm_vectors())
        if self._decay_task is None and bool(self._cfg("decay_enabled", True)):
            self._decay_task = asyncio.create_task(self._decay_loop())
            logger.info("[memory] 衰减调度已启动")
        if getattr(self, "_topics_task", None) is None and bool(self._cfg("topics_review_enabled", True)):
            self._topics_task = asyncio.create_task(self._topics_loop())
            logger.info("[memory] 主题复盘调度已启动")
        if not getattr(self, "_embed_ready", False):
            self._embed_ready = True
            self._configure_embedding()
        if getattr(self, "_due_task", None) is None:
            self._due_task = asyncio.create_task(self._due_loop())
            logger.info("[memory] 约定到期调度已启动（每 60 秒）")
        if getattr(self, "_sweep_task", None) is None and bool(self._cfg("idle_sweep_enabled", True)):
            self._sweep_task = asyncio.create_task(self._sweep_loop())
            logger.info("[memory] 空闲扫描已启动（每 60 秒）")
        if getattr(self, "_wander_task", None) is None and bool(self._cfg("wander_enabled", False)):
            self._wander_task = asyncio.create_task(self._wander_loop())
            logger.info("[memory] 走神循环已启动（默认关闭，配置开启）")
        # 隔了很久回来：给她一个"想起来"的机会
        try:
            _st = self._db()
            _last = _st.last_user_message_at(event.unified_msg_origin)
            if _last:
                from datetime import datetime as _dt
                _t = _dt.fromisoformat(_last)
                _now = _dt.now(_t.tzinfo) if _t.tzinfo else _dt.now()
                if ((_now - _t).total_seconds() >= 6 * 3600
                        and "FriendMessage" in event.unified_msg_origin):
                    _st.enqueue_trigger("return", 0, 0, event.unified_msg_origin)
        except Exception:
            pass
        # 先处理「更正」：库改了状态，她眼前这段对话也得知道
        try:
            fixes = self._db().recent_corrections(
                event.unified_msg_origin, hours=int(self._cfg("correction_hours", 24)), limit=8)
            if fixes:
                if not self._tail_has(req, "【后来才知道的事】"):
                    block = ("\n\n【后来才知道的事】\n"
                             + "\n".join("- " + f for f in fixes) + "\n")
                    self._append_tail(req, block)
                    logger.info(f"[memory] 注入更正 {len(fixes)} 条")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 注入更正出错：{type(e).__name__}: {e}")
        # 她自己放的常驻区：原样递回去，不筛选、不总结
        try:
            _n = self._push_resident(req, event)
            if _n:
                logger.info(f"[memory] 常驻区注入 {_n} 字（她自己放的）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 注入常驻区出错：{type(e).__name__}: {e}")
        # 可能相关的挂起项：她这句话也许和某条约定有关，递给她自己判断
        try:
            _text = (event.message_str or "").strip()
            if _text:
                _rel = await asyncio.to_thread(
                    self._related_in_thread, _text, event.unified_msg_origin)
                if _rel:
                    if not self._tail_has(req, "【忽然想到的事·不一定相关】"):
                        _lines = "\n".join(
                            f"「{x['text'][:80]}」——这条还挂着，你自己看要不要提。"
                            for x in _rel[:3])
                        self._append_tail(
                            req, "\n\n【忽然想到的事·不一定相关】\n" + _lines + "\n")
                        logger.info(f"[memory] 相关挂起项 {[x['id'] for x in _rel]} "
                                    f"相似度 {[x['sim'] for x in _rel]}")
        except asyncio.CancelledError:
            # 群聊插件/防抖会取消在途请求；挂起项是可选材料，取消就跳过
            logger.info("[memory] 请求已取消，跳过挂起项匹配")
            return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 挂起项匹配出错：{type(e).__name__}: {e}")

        # 到点的约定：只递一次
        try:
            st_r = self._db()
            try:
                _repeat = float(self._cfg("reminder_repeat_minutes", 30) or 0)
            except (TypeError, ValueError):
                _repeat = 30.0
            rems = st_r.pending_reminders(event.unified_msg_origin, limit=3,
                                          repeat_minutes=_repeat)
            if rems:
                if not self._tail_has(req, "【心里记着的事】"):
                    lines = "\n".join(f"- {r['text']}" for r in rems)
                    self._append_tail(
                        req,
                        "\n\n【心里记着的事】\n" + lines +
                        "\n（要不要说、什么时候说，你自己看。）\n")
                    st_r.mark_reminder_delivered([int(r["id"]) for r in rems])
                    logger.info(f"[memory] 注入到点约定 {len(rems)} 条")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 约定注入出错：{type(e).__name__}: {e}")

        # 三天没动静的事：只问她一次
        try:
            if bool(self._cfg("review_injection_enabled", True)):
                st_v = self._db()
                cands = st_v.review_candidates(
                    after_days=float(self._cfg("review_after_days", 3)),
                    asked_days=float(self._cfg("dormant_after_days", 7)),
                    limit=1, session=event.unified_msg_origin)
                if cands:
                    c = cands[0]
                    st_v.mark_review_asked(int(c["id"]))
                    if not self._tail_has(req, "【还没结的事·三天没动静】"):
                        self._append_tail(
                            req,
                            "\n\n【还没结的事·三天没动静】\n"
                            f"#{c['id']}「{c['text'][:80]}」"
                            "——还算数吗？算就继续，不算就用 settle_memory 了结。\n")
                        logger.info(f"[memory] 注入待确认 #{c['id']}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 待确认注入出错：{type(e).__name__}: {e}")

    @filter.on_llm_request(priority=-1000)
    async def on_request_impression(self, event: AstrMessageEvent, req):
        """印象放在 system 最后，离当前消息最近。"""
        if not bool(self._cfg("impression_enabled", True)):
            return
        try:
            text = (event.message_str or "").strip()
            if not text:
                return
            imp = await asyncio.to_thread(self._impression_in_thread, text)
            line = retriever.impression_line(imp)
            if not line:
                self._last_imp.pop(event.unified_msg_origin, None)
                return
            prev = self._last_imp.get(event.unified_msg_origin)
            now = time.monotonic()
            try:
                cooldown = float(self._cfg("impression_cooldown_sec", 180))
            except (TypeError, ValueError):
                cooldown = 180.0
            if prev and cooldown > 0:
                _ts, prev_strength, prev_line = prev
                if (now - _ts < cooldown
                        and prev_line == line
                        and abs(prev_strength - float(imp["strength"])) < 0.1):
                    return  # 冷却期内同一句不重复提醒
            self._last_imp[event.unified_msg_origin] = (
                now, float(imp["strength"]), line)
            if self._tail_has(req, "【印象】"):
                return
            self._append_tail(req, "\n\n【印象】\n" + line + "\n")
            logger.info(f"[memory] 印象（尾部）：{line}")
            if self._vec_task is None:
                self._vec_task = asyncio.create_task(self._warm_vectors())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 印象出错：{type(e).__name__}: {e}")

    @filter.on_llm_request(priority=-2000)
    async def on_request_two_stage(self, event: AstrMessageEvent, req):
        """正式回复前先让她在心里过一遍；只把材料/想法追加在末尾，保住前缀缓存。"""
        try:
            text = (event.message_str or "").strip()
            if not self._two_stage_want(event, text):
                return
            prov = await self._two_stage_provider(event)
            if prov is None:
                return
            try:
                is_group = not event.is_private_chat()
            except Exception:
                is_group = False
            if is_group:
                instr = (
                    "（这一步不用直接发出去，只先在心里过一遍。）\n"
                    "先判断这句要不要接：\n"
                    "不要接，就写一行：判断：不回\n"
                    "要接，就写一行：判断：回\n"
                    "要翻旧记忆，就写一行：查：<关键词>\n"
                    "如果心里先有一句判断，写：想：<一句话>"
                )
            else:
                instr = (
                    "（这一步不用回复婉清，只先在心里过一遍。）\n"
                    "看上面这句：它是在提旧事、旧约定、某个人，还是某段时间？\n"
                    "要翻旧记忆，就写一行：查：<关键词>\n"
                    "不用翻，就写：不用\n"
                    "如果心里先有一句判断，写：想：<一句话>"
                )
            try:
                resp = await asyncio.wait_for(
                    prov.text_chat(
                        prompt=getattr(req, "prompt", None) or text,
                        contexts=getattr(req, "contexts", None) or [],
                        system_prompt=getattr(req, "system_prompt", "") or "",
                        extra_user_content_parts=[TextPart(text=instr)],
                        persist=False),
                    timeout=float(self._cfg("recall_two_stage_timeout_sec", 12) or 12))
            except Exception as e:  # noqa: BLE001
                logger.info(f"[memory] 两段式预想跳过：{type(e).__name__}: {e}")
                return
            raw = (getattr(resp, "completion_text", "") or "").strip()
            _u = getattr(resp, "usage", None)
            if _u is not None:
                logger.info(f"[memory] 两段式第一段 usage：cached={getattr(_u, 'input_cached', 0)} "
                            f"other={getattr(_u, 'input_other', 0)} out={getattr(_u, 'output', 0)}")
            qm = re.search(r"(?m)^\s*查\s*[:：]\s*(.+?)\s*$", raw)
            tm = re.search(r"(?m)^\s*想\s*[:：]\s*(.+?)\s*$", raw)
            dm = re.search(r"(?m)^\s*判断\s*[:：]\s*(.+?)\s*$", raw)
            decision = (dm.group(1).strip() if dm else "")
            if is_group and (("不回" in decision) or ("不接" in decision)
                             or re.match(r"^\s*不回\s*$", raw)):
                self._log_two_stage(event, text, "不回", query="", thought="", raw=raw)
                try:
                    event.stop_event()
                except Exception:
                    pass
                logger.info("[memory] 两段式群聊：判断不回，跳过主回复")
                return
            query = (qm.group(1).strip()[:100] if qm else "")
            thought = (tm.group(1).strip()[:240] if tm else "")
            if not query and not thought:
                self._log_two_stage(event, text, "回" if is_group else "私聊",
                                    query="", thought="", raw=raw)
                return
            block = ""
            materials = ""
            # 保留第一段的“想”，但标明是查前猜想；第二段看到材料后可以推翻。
            if thought:
                label = "【查之前的猜想·不一定对】" if query else "【你自己先过了一遍】"
                block += f"{label}\n{thought}\n"
            if query:
                materials = await asyncio.to_thread(self._two_stage_lookup, query, event)
                if materials:
                    block += f"\n【可能相关，不一定】\n{materials}\n"
            self._log_two_stage(event, text, "回" if (is_group or query or thought) else "",
                                query=query, thought=thought, raw=raw, materials=materials)
            if not block:
                return
            block += "\n（这是内部材料，不是结论；材料和你之前想的不一样时，以材料为准；不够可以再查。）"
            parts = list(getattr(req, "extra_user_content_parts", None) or [])
            parts.append(TextPart(text=block))
            req.extra_user_content_parts = parts
            logger.info(f"[memory] 两段式：想 {len(thought)} 字，查 {query[:40]}，材料 {len(materials) if query else 0} 字")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 两段式出错：{type(e).__name__}: {e}")

    def _group_directed(self, event) -> bool:
        try:
            if bool(getattr(event, "is_at_or_wake_command", False)):
                return True
        except Exception:
            pass
        try:
            from astrbot.api.message_components import At, Reply
            self_id = str(event.get_self_id() or "")
            for comp in event.get_messages() or []:
                if At and isinstance(comp, At) and str(getattr(comp, "qq", "")) == self_id:
                    return True
                if Reply and isinstance(comp, Reply) and str(getattr(comp, "sender_id", "")) == self_id:
                    return True
        except Exception:
            pass
        return False

    def _two_stage_want(self, event, text: str) -> bool:
        if not bool(self._cfg("recall_two_stage_enabled", False)):
            return False
        if not text or text.startswith("/"):
            return False
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            return False
        if is_private:
            if bool(self._cfg("recall_two_stage_cues_only", True)) and not _TWO_STAGE_CUES.search(text):
                return False
        else:
            if not bool(self._cfg("recall_two_stage_group_enabled", False)):
                return False
            if (bool(self._cfg("recall_two_stage_group_directed_only", True))
                    and not self._group_directed(event)):
                return False
        now = time.time()
        cooldown = float(self._cfg("recall_two_stage_cooldown_sec", 20) or 0)
        last = (getattr(self, "_two_stage_last", {}) or {}).get(event.unified_msg_origin, 0.0)
        if cooldown and now - last < cooldown:
            return False
        if getattr(self, "_two_stage_last", None) is None:
            self._two_stage_last = {}
        self._two_stage_last[event.unified_msg_origin] = now
        return True

    async def _two_stage_provider(self, event):
        pid = ""
        try:
            pid = await self.context.get_current_chat_provider_id(event.unified_msg_origin) or ""
        except Exception:
            pid = ""
        prov = self.context.get_provider_by_id(pid) if pid else None
        if prov is None:
            try:
                prov = self.context.get_using_provider(umo=event.unified_msg_origin)
            except Exception:
                prov = None
        return prov

    def _two_stage_scope(self, event) -> str:
        try:
            if event.is_private_chat():
                return "private"
        except Exception:
            pass
        try:
            gid = str(event.get_group_id() or "")
        except Exception:
            gid = ""
        return f"group:{gid}" if gid else "group"

    def _log_two_stage(self, event, user_text: str, decision: str,
                       query: str = "", thought: str = "", raw: str = "",
                       materials: str = "") -> None:
        try:
            st = Store(self.db_path)
            try:
                st.add_two_stage_log(
                    event.unified_msg_origin, self._two_stage_scope(event),
                    user_text, decision, query, thought, raw, materials)
            finally:
                st.close()
        except Exception as e:  # noqa: BLE001
            logger.info(f"[memory] 预想记录失败：{type(e).__name__}: {e}")

    def _two_stage_lookup(self, query: str, event) -> str:
        try:
            since, until = _auto_time_bounds(query, "", "")
            scope = self._two_stage_scope(event)
            st = Store(self.db_path)
            try:
                hits = retriever.search(st, query, top_k=3,
                                        since=since, until=until, scope=scope)
                floor = float(self._cfg("recall_min_score", 0.30) or 0.30)
                hits = [h for h in hits if float(h.get("score") or 0) >= floor]
                try:
                    st.log_recall_query(
                        event.unified_msg_origin, query, len(hits),
                        degraded=bool(retriever.embedding_status().get("disabled")),
                        sort="", scope=scope, since=since, until=until,
                        results=[{"id": int(h.get("id") or 0),
                                  "kind": h.get("kind") or "",
                                  "score": round(float(h.get("score") or 0), 3),
                                  "text": str(h.get("text") or "")[:200]}
                                 for h in hits[:10]],
                        trigger="two_stage")
                except Exception:
                    pass
            finally:
                st.close()
            lines = []
            for h in hits[:3]:
                when = _fmt_when(str(h.get("anchor_time") or ""))
                lines.append(f"#{h.get('id')} [{h.get('kind')}] {str(h.get('text') or '')[:180]}"
                             + (f"（{when}）" if when else ""))
            return "\n".join(lines)
        except Exception as e:  # noqa: BLE001
            logger.info(f"[memory] 两段式查记忆失败：{type(e).__name__}: {e}")
            return ""

    @filter.on_llm_request()
    async def on_request_capability(self, event: AstrMessageEvent, req):
        """只陈述「她有哪些工具」，不规定怎么用。"""
        if not bool(self._cfg("capability_note", True)):
            return
        base = getattr(req, "system_prompt", "") or ""
        if "【记忆】" in base:
            return
        note = ("\n\n【记忆】\n"
                "你有自己的记忆库：recall_memory 翻内容、sort_last_recall 重排刚才那批、"
                "recall_day 按天回想、memory_directory 看目录、read_memory 深读一条、"
                "list_unresolved 看没办完的事、settle_memory 给挂着的约定/未了收尾、"
                "tag_memory 给旧记忆起名、avoid_memory 标记不想提的事。\n"
                "系列是你自己分的：series_create 建一个、series_rename 改名、series_add 把几次发生放进去、"
                "series_list 看有哪些、series_show 展开；系列只加链接，不改正文、不删记忆。\n")
        req.system_prompt = base + note

    # ---------- 刚才那批的重排 ----------

    def _remember_recall(self, event, hits) -> None:
        try:
            key = str(getattr(event, "unified_msg_origin", "") or "default")
            cache = getattr(self, "_last_recall", None)
            if cache is None:
                cache = {}
                self._last_recall = cache
            if len(cache) >= 20 and key not in cache:
                cache.pop(next(iter(cache)), None)
            cache[key] = {"hits": list(hits), "at": time.time()}
        except Exception:  # noqa: BLE001
            pass

    def _last_recall_hits(self, event):
        try:
            key = str(getattr(event, "unified_msg_origin", "") or "default")
            cache = getattr(self, "_last_recall", {}) or {}
            entry = cache.get(key)
            if not entry:
                return None
            if time.time() - float(entry.get("at") or 0) > 1800:
                cache.pop(key, None)
                return None
            return list(entry.get("hits") or [])
        except Exception:  # noqa: BLE001
            return None

    def _render_hits(self, store, hits, title: str, order_label: str, sort_mode: str) -> str:
        if not hits:
            return title + "\n（没有）"
        # 旁注里有「被事实纠正过」这类信息，回想时一并给她看（只读，不改正文）
        notes: dict[int, list[str]] = {}
        her_words: dict[int, str] = {}
        her_quotes: dict[int, str] = {}
        ids = [int(h["id"]) for h in hits]
        try:
            if ids:
                q = ",".join("?" * len(ids))
                for r in store.conn.execute(
                        f"SELECT trace_id, kind, text FROM trace_note WHERE trace_id IN ({q}) ORDER BY id", ids):
                    if (r["kind"] or "") == "her_words":
                        her_words[int(r["trace_id"])] = r["text"]
                    elif (r["kind"] or "") == "quote":
                        her_quotes[int(r["trace_id"])] = r["text"]
                    else:
                        notes.setdefault(int(r["trace_id"]), []).append(r["text"])
        except Exception:  # noqa: BLE001
            notes = {}

        marks = ",".join("?" for _ in ids)
        metas = {
            int(r["id"]): r
            for r in store.conn.execute(
                f"SELECT id, created_at, episode_id, scope FROM trace WHERE id IN ({marks})",
                ids)
        }
        sessions = _session_map(store, [m["episode_id"] for m in metas.values()])
        if sort_mode == "time":
            def _when_key(h):
                meta = metas.get(int(h["id"]))
                return str(h.get("anchor_time") or (meta["created_at"] if meta else "") or "")
            hits = sorted(hits, key=_when_key)
        else:
            hits = sorted(hits, key=lambda h: -float(h.get("score") or 0))

        lines = []
        for h in hits:
            meta = metas.get(int(h["id"]))
            created = str(meta["created_at"] or "") if meta else ""
            eid = int(meta["episode_id"] or 0) if meta else 0
            scope = str(meta["scope"] or "") if meta else str(h.get("scope") or "")
            when_raw = h.get("anchor_time") or created
            when = _fmt_when(when_raw)
            if when and not h.get("anchor_time"):
                when += "（记下）"
            scene = _scene_label(scope, sessions.get(eid, ""))
            header = " · ".join(x for x in (order_label, when, scene) if x)
            if lines:
                lines.append("")
            if header:
                lines.append(header)
            st = h.get("state") or "open"
            mark = {"open": "", "void": "（已作废）", "missed": "（过点了）",
                    "stale": "（久了，没再提）", "old": "（很久没提了）",
                    "dormant": "（休眠了）"}.get(st, "（已了结）")
            line = f"#{h['id']} [{h['kind']}]{mark} {h['text']}"
            mine = her_words.get(int(h["id"]))
            if mine:
                line += f"\n    ｜我的话：{mine[:120]}"
            extra = notes.get(int(h["id"])) or []
            if h.get("felt"):
                line += f"\n    ｜当时感觉：{h['felt']}"
            if h.get("reaction"):
                line += f"\n    ｜我的反应：{h['reaction']}"
            if her_quotes.get(int(h["id"])):
                line += f"\n    ｜原话（她留的）：「{her_quotes[int(h['id'])][:80]}」"
            elif h.get("anchor_quote"):
                try:
                    if store.anchor_quote_ok(int(h["id"]), h["anchor_quote"]):
                        line += f"\n    ｜原话：「{h['anchor_quote'][:80]}」"
                except Exception:
                    line += f"\n    ｜原话：「{h['anchor_quote'][:80]}」"
            if h.get("inference"):
                c = h.get("inference_certainty")
                line += f"\n    ｜推断（{c:.1f}）：{h['inference']}" if isinstance(c, (int, float)) else f"\n    ｜推断：{h['inference']}"
            if extra:
                line += "\n    ｜旁注：" + "；".join(x[:60] for x in extra[:2])
            gs = int(h.get("group_size") or 1)
            label = str(h.get("group_label") or "")
            if gs > 1 or label:
                first = _fmt_when(str(h.get("group_first") or ""))
                last = _fmt_when(str(h.get("group_last") or ""))
                bits = []
                if first:
                    bits.append("最早 " + first)
                if last and last != first:
                    bits.append("最近 " + last)
                prefix = f"｜系列「{label}」" if label else "｜同系列"
                line += f"\n    {prefix}：共 {gs} 条" + (f"（{'，'.join(bits)}）" if bits else "")
            lines.append(line)
        return title + "\n" + "\n".join(lines)

    @filter.llm_tool(name="recall_memory")
    async def recall_memory(self, event: AstrMessageEvent, query: str = "", kind: str = "",
                            limit: int = 5, since: str = "", until: str = "",
                            scope: str = "", person: str = "", sort: str = "relevance"):
        '''翻你自己的记忆。想知道某件事、某个人、某个约定时用。
        返回的是「材料」（当时的记录），不是答案——用你自己的话回想，不要照抄。

        Args:
            query(string): 想查的内容，可以是一个词，也可以是一句话
            kind(string): 可选，只查一类：fact/observation/speculation/self/unresolved/promise/agreement
            limit(int): 最多几条，默认 5
            since(string): 可选，只看这之后：2026-11-10 / 昨天 / 最近3天 / 7天前
            until(string): 可选，只看这之前：2026-11-12 / 今天
            scope(string): 可选，场景：私聊 / 群聊 / 群:123456
            person(string): 可选，关于谁：婉清 / 秋夜 / 董汐（按正文和主题词匹配）
            sort(string): 排序：relevance（默认，按相关度）/ time（按时间，早→晚）
        '''
        try:
            store = self._db()
            since_d = _date_bound(since)
            until_d = _date_bound(until)
            if since_d is None or until_d is None:
                return "时间没看懂。写 2026-11-10、昨天、最近3天、7天前 这种。"
            since_d, until_d = _auto_time_bounds(query, since_d or "", until_d or "")
            if since_d and until_d and since_d > until_d:
                return "开始时间比结束时间晚，倒过来了。"
            scope_f = _scope_value(scope)
            if scope_f is None:
                return "场景没看懂。写 私聊 / 群聊 / 群:123456。"
            person_f = (person or "").strip()
            if person_f.startswith("关于"):
                person_f = person_f[2:].strip()
            if query.strip():
                hits = await asyncio.to_thread(
                    self._search_in_thread, query or "", int(limit or 5), kind or "",
                    since_d or "", until_d or "", scope_f or "", person_f)
            else:
                hits = await asyncio.to_thread(
                    self._recent_in_thread, kind or "", int(limit or 5),
                    since_d or "", until_d or "", scope_f or "", person_f)
                for h in hits:
                    h["score"] = 0.0
            try:
                floor = float(self._cfg("recall_min_score", 0.30))
            except (TypeError, ValueError):
                floor = 0.30
            if since_d or until_d:
                try:
                    floor = max(floor, float(self._cfg("recall_time_min_score", 0.45)))
                except (TypeError, ValueError):
                    floor = max(floor, 0.45)
            if query.strip():
                hits = [h for h in hits if float(h.get("score") or 0) >= floor]
                _related = _related_terms(query)
                if _related and (not hits or float(hits[0].get("score") or 0) < 0.55):
                    try:
                        extra = await asyncio.to_thread(
                            self._search_in_thread, " ".join(_related), int(limit or 5),
                            kind or "", since_d or "", until_d or "", scope_f or "", person_f)
                        best = {int(h["id"]): h for h in hits}
                        for h in extra:
                            if float(h.get("score") or 0) < floor:
                                continue
                            tid = int(h["id"])
                            if tid not in best or float(h.get("score") or 0) > float(best[tid].get("score") or 0):
                                best[tid] = h
                        hits = sorted(best.values(), key=lambda h: -float(h.get("score") or 0))
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[memory] 相关词检索失败：{type(e).__name__}: {e}")
            meal_fallback = []
            if (not hits and (since_d or until_d) and _meal_fallback_needed(query)):
                try:
                    window = await asyncio.to_thread(
                        self._recent_in_thread, "", 40, since_d or "", until_d or "",
                        scope_f or "", person_f)
                    food_marks = ("吃", "饭", "餐", "外卖", "汉堡", "面", "米",
                                  "零食", "夜宵", "烧烤", "火锅")
                    meal_fallback = [h for h in window
                                     if any(x in str(h.get("text") or "") for x in food_marks)][:5]
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[memory] 吃饭时间兜底失败：{type(e).__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] recall_memory 出错：{type(e).__name__}: {e}", exc_info=True)
            return f"翻不动：{type(e).__name__}"
        try:
            _results = [
                {"id": int(h.get("id") or 0), "kind": h.get("kind") or "",
                 "score": round(float(h.get("score") or 0), 3),
                 "text": str(h.get("text") or "")[:200]}
                for h in hits[:10]
            ]
            store.log_recall_query(
                event.unified_msg_origin, query or "", len(hits),
                degraded=bool(retriever.embedding_status().get("disabled")),
                sort=sort or "", scope=scope_f or "", kind=kind or "",
                person=person_f, since=since_d or "", until=until_d or "",
                results=_results, trigger="tool",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 检索日志写入失败：{type(e).__name__}: {e}")
        raw_hits = []
        try:
            if query and not kind and not person_f:
                _mq = _raw_match_query(query)
                if _mq:
                    raw_hits = await asyncio.to_thread(
                        store.raw_fts_search, _mq, 3,
                        event.unified_msg_origin, since_d or "", until_d or "")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 原话检索失败：{type(e).__name__}: {e}")
        self_hits = []
        try:
            if bool(self._cfg("self_thought_recall", False)):
                want_self = (kind or "") == "self" or any(
                    x in (query or "") for x in ("我", "自己", "最近", "想想"))
                if want_self:
                    terms = _self_terms(query)
                    if terms:
                        self_hits = await asyncio.to_thread(
                            self._self_thoughts_in_thread, terms, 5,
                            event.unified_msg_origin, since_d or "", until_d or "", False)
                    elif (kind or "") == "self":
                        self_hits = await asyncio.to_thread(
                            self._self_thoughts_in_thread, [], 5,
                            event.unified_msg_origin, since_d or "", until_d or "", True)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 自我思考检索失败：{type(e).__name__}: {e}")
        if not hits and not raw_hits and not self_hits and not meal_fallback:
            if since_d or until_d:
                return f"{since_d or '开始'} 到 {until_d or '现在'} 没找到相关的。"
            return "没找到相关的。"
        sort_mode = _sort_mode(sort)
        if not sort_mode:
            return "排序没看懂。写 relevance（按相关度）或 time（按时间，早→晚）。"
        for h in hits[:3]:
            try:
                store.activate(h["id"], cue_type="recall", query=query or "")
            except Exception:
                pass
        self._remember_recall(event, hits)
        order_label = "按时间" if sort_mode == "time" else "相关"
        title = "材料（按时间排，早→晚）：" if sort_mode == "time" else "材料（按相关度排）："
        out = self._render_hits(store, hits, title, order_label, sort_mode) if hits else "材料（没找到提炼过的）："
        if raw_hits:
            rlines = ["【原话】"]
            for r in raw_hits:
                who = "我" if (r.get("role") or "") == "assistant" else (r.get("sender") or "对方")
                rlines.append(f"{_fmt_when(str(r.get('created_at') or ''))} · {who}："
                              f"{str(r.get('text') or '')[:300]}")
            out += "\n\n" + "\n".join(rlines)
        if self_hits:
            slines = ["【我自己写的】"]
            for s in self_hits:
                slines.append(f"{_fmt_when(str(s.get('created_at') or ''))} · "
                              f"第 {s.get('tick_no')} 次：{str(s.get('text') or '')[:300]}")
            out += "\n\n" + "\n".join(slines)
        if hits:
            try:
                companion = store.episode_companion(int(hits[0]["id"]))
            except Exception:
                companion = None
            if companion:
                out += ("\n\n【同段的一条】\n"
                        f"#{companion['id']} [{companion['kind']}]："
                        f"{str(companion.get('text') or '')[:200]}\n"
                        "（只是同一段材料，不一定因果。）")
        if meal_fallback:
            lines = ["【时间附近的记录】"]
            for h in meal_fallback:
                lines.append(f"{_fmt_when(str(h.get('anchor_time') or ''))} · "
                             f"#{h.get('id')}：{str(h.get('text') or '')[:200]}")
            lines.append("（原文没写“晚饭”，只是在这个时间段。）")
            out += "\n\n" + "\n".join(lines)
        return out

    @filter.llm_tool(name="sort_last_recall")
    async def sort_last_recall(self, event: AstrMessageEvent, sort: str = "time"):
        """把刚才 recall_memory 返回的那批结果重新排，不重新搜、不重新算向量。

        什么时候用：你刚搜过，觉得顺序不对，想按时间或相关度再看一遍。
        没有刚才那批时不要用，先 recall_memory。

        Args:
            sort(string): time（按时间早→晚，默认）/ relevance（按相关度）
        """
        hits = self._last_recall_hits(event)
        if not hits:
            return "刚才没有检索过，或者那批太久了；先 recall_memory 搜一次，再重排。"
        mode = _sort_mode(sort)
        if not mode:
            return "排序没看懂。写 relevance（按相关度）或 time（按时间，早→晚）。"
        title = "刚才那批（按时间排，早→晚）：" if mode == "time" else "刚才那批（按相关度排）："
        order_label = "按时间" if mode == "time" else "相关"
        return self._render_hits(self._db(), hits, title, order_label, mode)

    @filter.llm_tool(name="send_group_message")
    async def send_group_message(self, event: AstrMessageEvent, text: str = "", group: str = ""):
        '''在群里主动说一句。只在真的想说、跟这个群有关时用；不要频繁，不要把私聊里的称呼、约定、私事拿到群里。

        Args:
            text(string): 要说的话，一两句就够
            group(string): 可选，群号；不填就用当前群
        '''
        text = (text or "").strip()
        if not text:
            return "空的，没说。"
        if not bool(self._cfg("group_send_enabled", False)):
            return "群聊主动发话没开。"
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        gid = ""
        session = ""
        if group.strip():
            gid = "".join(ch for ch in group if ch.isdigit())
            if not gid:
                return "群号没看懂。"
            try:
                st = self._db()
                row = st.conn.execute(
                    "SELECT session_id FROM raw_message WHERE session_id LIKE ? "
                    "ORDER BY id DESC LIMIT 1", (f"%GroupMessage:{gid}",)).fetchone()
                session = (row[0] if row else "") or ""
            except Exception as e:  # noqa: BLE001
                return f"查群会话失败：{type(e).__name__}"
            if not session:
                row = st.conn.execute(
                    "SELECT session_id FROM raw_message WHERE session_id LIKE '%:GroupMessage:%' "
                    "ORDER BY id DESC LIMIT 1").fetchone()
                if row and row[0]:
                    session = row[0].rsplit(":", 1)[0] + ":" + gid
            if not session:
                return "这个群还没有会话记录，发不了。"
        else:
            if "GroupMessage" not in origin:
                return "这里是私聊。要在群里说，把群号一起给我。"
            session = origin
            gid = origin.rsplit(":", 1)[-1]
        allow = str(self._cfg("group_send_allow", "") or "").strip()
        if allow:
            ids = {x.strip() for x in allow.replace("，", ",").split(",") if x.strip()}
            if gid not in ids:
                return "这个群不在允许主动发话的名单里。"
        if self._quiet_hours(str(self._cfg("group_send_quiet_hours", "1-7") or "")):
            return "现在是静默时段，不主动发。"
        import datetime as _dt
        key = f"group_send:{gid}:{_dt.date.today().isoformat()}"
        try:
            st = self._db()
            used = int(st.kv_get(key) or 0)
        except Exception:
            used = 0
        if _group_send_capped(self._cfg("group_send_max_per_day", 2), used):
            return f"这个群今天已经主动说过 {used} 次了，先不说。"
        try:
            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import Plain
            ok = await self.context.send_message(session, MessageChain([Plain(text[:500])]))
        except Exception as e:  # noqa: BLE001
            return f"没发出去：{type(e).__name__}"
        if not ok:
            return "没发出去。"
        try:
            self._db().kv_set(key, str(used + 1))
        except Exception:
            pass
        logger.info(f"[memory] 她在群里主动说了一句：{gid}")
        return "说了。"

    @filter.llm_tool(name="keep_quote")
    async def keep_quote(self, event: AstrMessageEvent, memory_id: int = 0, quote: str = ""):
        '''把一句原话钉在这条记忆上。翻到旧记忆、觉得提炼那句不像当时说的，用这个换成真正的那句。

        Args:
            memory_id(int): 记忆编号
            quote(string): 当时的原话（尽量照抄，不要改写）
        '''
        quote = (quote or "").strip()
        if not quote:
            return "空的，没钉。"
        try:
            store = self._db()
            if not store.get_trace(int(memory_id)):
                return "没有这条记忆。"
            store.add_note(int(memory_id), quote[:300], kind="quote")
        except Exception as e:  # noqa: BLE001
            return f"没钉上：{type(e).__name__}"
        return "钉好了，以后翻到这条会先看到这句原话。"

    @filter.llm_tool(name="leave_my_words")
    async def leave_my_words(self, event: AstrMessageEvent, memory_id: int = 0, text: str = ""):
        '''在一条记忆后面留一句你自己的话。翻到旧记忆、觉得"不是这个意思"时用。

        Args:
            memory_id(int): 记忆编号
            text(string): 你想补的那句话（一两句，别写成长篇）
        '''
        text = (text or "").strip()
        if not text:
            return "空的，没写。"
        try:
            store = self._db()
            if not store.get_trace(int(memory_id)):
                return "没有这条记忆。"
            store.add_note(int(memory_id), text[:300], kind="her_words")
        except Exception as e:  # noqa: BLE001
            return f"没写上：{type(e).__name__}"
        return "留好了，以后翻到这条会先看到它。"

    @filter.llm_tool(name="read_core_memory")
    async def read_core_memory(self, event: AstrMessageEvent):
        """看看你对自己的认识（核心层）。想确认自己是谁、记得什么长期的事时用。"""
        try:
            rows = self._db().core_entries(8)
        except Exception as e:  # noqa: BLE001
            return f"翻不动：{type(e).__name__}"
        if not rows:
            return "核心层还是空的。你可以用 write_core_memory 写一条。"
        return "\n".join(f"#{r['id']}（{r['created_at'][:10]}）{r['text']}" for r in rows)

    @filter.llm_tool(name="write_core_memory")
    async def write_core_memory(self, event: AstrMessageEvent, text: str):
        """往核心层写一条：关于你自己、或者关于你记着的长期的事。旧条不删。

        Args:
            text(string): 要写的话，一两句，别写成任务清单
        """
        text = (text or "").strip()
        if not text:
            return "空的，没写。"
        try:
            self._db().add_core(text)
        except Exception as e:  # noqa: BLE001
            return f"没写上：{type(e).__name__}"
        return "写进核心层了。"

    @filter.llm_tool(name="resident_add")
    async def resident_add(self, event: AstrMessageEvent, text: str, scope: str = "global"):
        """把你想要一直摆在眼前的一件事，放进你自己的常驻区。

        什么时候用：有些事你不想只靠回想，想每轮都看到——正在等的、约好的、
        提醒自己的、怕自己忘的。放什么、撤什么由你定，系统不替你挑。
        常驻区最多 12 条、1200 字；满了会拒绝，先 resident_retire 一条。

        Args:
            text(string): 你自己写的一句话，尽量短
            scope(string): global（所有会话）/ private（只在这个私聊）/ group（只在这个群）
        """
        try:
            sc = self._resolve_resident_scope(event, scope)
            nid = self._db().resident_add(text, sc)
        except Exception as e:  # noqa: BLE001
            return f"没放进去：{e}"
        return f"放好了，常驻 #{nid}（{sc}）。"

    @filter.llm_tool(name="resident_retire")
    async def resident_retire(self, event: AstrMessageEvent, note_id: int):
        """从你自己的常驻区撤掉一条（不删除，只标已撤）。

        Args:
            note_id(int): 常驻区的编号
        """
        try:
            ok = self._db().resident_retire(int(note_id))
        except Exception as e:  # noqa: BLE001
            return f"没撤成：{type(e).__name__}"
        if not ok:
            return f"#{int(note_id)} 不在常驻区，或者已经撤了。"
        return f"撤掉了 #{int(note_id)}。"

    @filter.llm_tool(name="resident_list")
    async def resident_list(self, event: AstrMessageEvent):
        """看看你自己的常驻区现在放着什么（只看当前会话能看到的）。"""
        try:
            rows = self._db().resident_active(self._resident_scope_keys(event))
        except Exception as e:  # noqa: BLE001
            return f"翻不动：{type(e).__name__}"
        if not rows:
            return "常驻区现在是空的。你可以用 resident_add 放一条。"
        out = []
        for r in rows:
            sc = str(r.get("scope") or "global")
            label = "全局" if sc == "global" else ("这个群" if sc.startswith("group:") else "这个私聊")
            out.append(f"#{r['id']} [{label}] {r['text']}")
        return "\n".join(out)

    @filter.llm_tool(name="series_create")
    async def series_create(self, event: AstrMessageEvent, name: str = "",
                            scope: str = "auto", note: str = ""):
        """给你自己分一个「系列」：一类反复发生的事，不是一个记忆。

        系列只是把几次发生挂在一起，不改正文、不删原文、不合并成一句。
        建好后用 series_add 把记忆放进去；检索时会按系列折叠成一条代表。

        Args:
            name(string): 系列名，比如「每天叫他起床」
            scope(string): auto（当前场景，默认）/ global（所有会话）/ private（这个私聊）/ group（这个群）
            note(string): 可选，一句说明
        """
        if not (name or "").strip():
            return "系列名是空的。先想一个名字，比如「每天叫他起床」。"
        try:
            sc = self._series_scope(event, scope)
            sid = self._db().series_create(name, sc, note)
        except Exception as e:  # noqa: BLE001
            return f"系列没建成：{e}"
        return f"系列 #{sid}「{name.strip()}」建好了（{self._series_scope_label(sc)}）。用 series_add 把记忆放进去。"

    @filter.llm_tool(name="series_rename")
    async def series_rename(self, event: AstrMessageEvent, series_id: int = 0,
                            name: str = ""):
        """给系列改个名字（只改名字，不动成员、不动记忆）。"""
        if int(series_id or 0) <= 0 or not (name or "").strip():
            return "要说清楚：series_rename 需要系列编号和新名字。"
        try:
            st = self._db()
            cur = st.conn.execute(
                "UPDATE memory_series SET name=? WHERE id=? AND status='active'",
                ((name or "").strip()[:60], int(series_id)))
            st.conn.commit()
        except Exception as e:  # noqa: BLE001
            return f"没改成：{type(e).__name__}"
        if not cur.rowcount:
            return "没有这个系列，或者已经撤了。"
        return f"系列 #{int(series_id)} 改名为「{(name or '').strip()[:60]}」；成员没动。"

    @filter.llm_tool(name="series_add")
    async def series_add(self, event: AstrMessageEvent, series_id: int = 0,
                         memory_ids: str = ""):
        """把几条记忆放进你自己的系列里（只加链接，不改正文、不删除）。

        一条记忆可以同时放进多个系列；同一个系列重复放只算一次。

        Args:
            series_id(int): 系列编号
            memory_ids(string): 记忆编号，逗号/空格分隔，比如 "123,124,125"
        """
        import re as _re
        if int(series_id or 0) <= 0:
            return "要放进哪个系列？先用 series_list 看编号，再 series_add。"
        ids = []
        for x in _re.split(r"[,，、\s]+", str(memory_ids or "")):
            x = x.strip().lstrip("#")
            if x.isdigit():
                ids.append(int(x))
        if not ids:
            return "没看懂要放哪些记忆。写编号，比如 123,124。"
        ok, errors = 0, []
        for tid in ids[:50]:
            try:
                if self._db().series_add(int(series_id), tid):
                    ok += 1
            except Exception as e:  # noqa: BLE001
                errors.append(str(e))
        if ok and not errors:
            return f"放进系列 #{int(series_id)} 了：{ok} 条。"
        if ok:
            return f"放进 {ok} 条；还有 {len(errors)} 条没放：{'；'.join(errors[:3])}"
        return "一条都没放进去：" + "；".join(errors[:3])

    @filter.llm_tool(name="series_remove")
    async def series_remove(self, event: AstrMessageEvent, series_id: int = 0,
                            memory_id: int = 0):
        """从系列里拿掉一条记忆（只解除链接，记忆本身不删）。"""
        if int(series_id or 0) <= 0 or int(memory_id or 0) <= 0:
            return "要说清楚：series_remove 需要系列编号和记忆编号。"
        try:
            ok = self._db().series_remove(int(series_id), int(memory_id))
        except Exception as e:  # noqa: BLE001
            return f"没拿掉：{type(e).__name__}"
        return f"从系列 #{int(series_id)} 拿掉了 #{int(memory_id)}。" if ok else "这条不在这个系列里。"

    @filter.llm_tool(name="series_retire")
    async def series_retire(self, event: AstrMessageEvent, series_id: int = 0):
        """撤掉整个系列（只撤系列，记忆和它们的正文都还在）。"""
        if int(series_id or 0) <= 0:
            return "要撤哪个系列？给编号，先用 series_list 看一下。"
        try:
            ok = self._db().series_retire(int(series_id))
        except Exception as e:  # noqa: BLE001
            return f"没撤成：{type(e).__name__}"
        return f"系列 #{int(series_id)} 撤了；里面的记忆没删。" if ok else "没有这个系列，或者已经撤了。"

    @filter.llm_tool(name="series_list")
    async def series_list(self, event: AstrMessageEvent):
        """看看你自己建了哪些系列（只看当前场景能看到的）。"""
        try:
            rows = self._db().series_list(self._resident_scope_keys(event))
        except Exception as e:  # noqa: BLE001
            return f"翻不动：{type(e).__name__}"
        if not rows:
            return "还没有系列。可以用 series_create 建一个。"
        lines = ["你自己分的系列："]
        for r in rows:
            when = _fmt_when(str(r.get("last_added") or ""))
            tail = f"，最近放入 {when}" if when else ""
            lines.append(
                f"#{r['id']}「{r['name']}」· {self._series_scope_label(r['scope'])}"
                f" · {int(r['n'] or 0)} 条{tail}")
        return "\n".join(lines)

    @filter.llm_tool(name="series_show")
    async def series_show(self, event: AstrMessageEvent, series_id: int = 0,
                          since: str = "", until: str = "", limit: int = 30,
                          query: str = ""):
        """看一个系列里有哪些发生（按时间排，正文原样）。

        Args:
            series_id(int): 系列编号
            since(string): 可选，只看这之后：2026-11-10 / 昨天 / 最近3天 / 7天前
            until(string): 可选，只看这之前：2026-11-12 / 今天
            limit(int): 最多显示几条，默认 30；0 = 全部
            query(string): 可选，只在系列里按关键词找（多个词用空格，要求都出现）
        """
        if int(series_id or 0) <= 0:
            return "要看哪个系列？给编号，先用 series_list 看一下。"
        since_d = _date_bound(since)
        until_d = _date_bound(until)
        if since_d is None or until_d is None:
            return "时间没看懂。写 2026-11-10、昨天、最近3天、7天前 这种。"
        if since_d and until_d and since_d > until_d:
            return "开始时间比结束时间晚，倒过来了。"
        try:
            s = self._db().series_show(int(series_id))
        except Exception as e:  # noqa: BLE001
            return f"翻不动：{type(e).__name__}"
        if not s:
            return "没有这个系列。"
        if s.get("scope") != "global" and s.get("scope") not in self._resident_scope_keys(event):
            return "这个系列不在当前场景，看不了。"
        all_members = s["members"]
        # 以对话时间（anchor_time）为准排序：event_time 是提炼时估的，可能错年错月
        all_members = sorted(
            all_members,
            key=lambda m: str(m.get("anchor_time") or m.get("created_at")
                              or m.get("event_time") or ""))
        members, total = _filter_series_members(all_members, since_d, until_d, limit, query)
        head = (f"系列 #{s['id']}「{s['name']}」· {self._series_scope_label(s['scope'])}"
                f" · 共 {len(all_members)} 条")
        bits = []
        if since_d or until_d:
            bits.append(f"{since_d or '开始'}–{until_d or '现在'}")
        if (query or "").strip():
            bits.append(f"关键词「{query.strip()}」")
        if bits:
            head += f"（筛选 {'，'.join(bits)}，{total} 条）"
        lines = [head + "："]
        if not members:
            lines.append("（没有符合条件的）")
        for m in members:
            when = _fmt_when(str(m.get("anchor_time") or m.get("created_at")
                                  or m.get("event_time") or ""))
            lines.append(f"{when} · #{m['id']} [{m['kind']}] {m['text']}")
        if 0 < len(members) < total:
            lines.append(f"（只显示前 {len(members)} 条，还有 {total - len(members)} 条；limit=0 看全部）")
        return "\n".join(lines)

    @filter.llm_tool(name="recall_day")
    async def recall_day(self, event: AstrMessageEvent, day: str = "今天"):
        """按时间线回想某一天发生了什么（不是搜索，是翻那天的经历链）。

        什么时候用：被问「今天/昨天都干了什么」「那天怎么回事」，
        或者你想自己复盘一天，而不是想起某件具体的事。
        不要每轮都调：一天翻一次就够，刚聊过的内容你本来就在上下文里（工具会跳过最近半小时）。

        Args:
            day(string): 「今天」「昨天」「前天」，或 2026-11-11 这样的日期
        """
        import datetime as _dt
        d = (day or "今天").strip()
        today = _dt.date.today()
        mapping = {"今天": 0, "昨天": 1, "前天": 2, "大前天": 3}
        if d in mapping:
            target = today - _dt.timedelta(days=mapping[d])
        else:
            try:
                target = _dt.date.fromisoformat(d)
            except Exception:
                return "日期没看懂，写「今天」「昨天」或 2026-11-11。"
        st = Store(self.db_path)
        try:
            data = st.day_chain(target.isoformat())
        finally:
            st.close()
        eps = data["episodes"]
        if not eps:
            return f"{target.isoformat()} 那天我这边没有记录。"
        lines = [f"{target.isoformat()} 我这边是这样的："]
        for e in eps:
            lines.append(f"{e['start']}–{e['end']}（{e['total']} 条）")
            for it in e["items"]:
                text = str(it["text"]).split("；因此")[0][:56]
                lines.append(f"  · {text}")
        if data["open_items"]:
            lines.append("那天还挂着的事：")
            for r in data["open_items"][:5]:
                lines.append(f"  · [{r['kind']}] {str(r['text'])[:40]}")
        out = "\n".join(lines)
        return out[:1600]

    @filter.llm_tool(name="settle_memory")
    async def settle_memory(self, event: AstrMessageEvent, memory_id: int,
                            state: str = "settled", note: str = ""):
        """给一条挂着的约定/未了的事收尾（你自己判断已经过去了）。

        什么时候用：你想起某件事其实已经办完了、或者已经不算数了，
        不用等下次提炼。办完了用 settled，作废/算了用 void。

        Args:
            memory_id(int): 记忆编号（#后面的数字）
            state(string): settled（办完了/说清了）或 void（作废/算了）
            note(string): 一句话说明依据，比如「他说下课了」
        """
        st = str(state or "settled").strip().lower()
        if st not in ("settled", "void"):
            return "state 只能是 settled 或 void。"
        try:
            store = self._db()
            row = store.trace_row(int(memory_id))
            if not row:
                return f"没找到 #{int(memory_id)}。"
            if row["kind"] not in ("promise", "unresolved", "agreement"):
                return f"#{int(memory_id)} 不是约定/未了，不用这样收尾。"
            if (row["recur"] or "") and st == "void":
                store.cancel_agreement(int(memory_id), note or "她自己取消了")
                return f"#{int(memory_id)} 的循环约定已经取消。"
            if (row["recur"] or "") and st == "settled":
                store.add_note(int(memory_id), note or "这一次做到了", kind="annotation")
                return f"#{int(memory_id)} 记下了：这一次做到了（明天照常）。"
            store.settle(int(memory_id), st, note or "")
            return f"#{int(memory_id)} 已标为{'办完' if st == 'settled' else '作废'}。"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] settle_memory 出错：{type(e).__name__}: {e}")
            return "收尾没成功。"

    @filter.llm_tool(name="tag_memory")
    async def tag_memory(self, event: AstrMessageEvent, memory_id: int, topics: str):
        """给一条旧记忆起主题词（它关于什么），方便以后想起来。

        什么时候用：你翻到一条以前的事，觉得以后还会用到，但它没有主题。
        Args:
            memory_id(int): 记忆编号（#后面的数字）
            topics(string): 2-3 个主题词，用顿号或逗号分隔，2-6 个字；
                至少两个要是别人可能用来问的词（如「换客户端」「白饭减半」）。
        """
        try:
            raw = [x for x in str(topics or "").replace(",", "、").replace("，", "、").split("、")]
            tp = archivist._clean_topics(raw)
            if not tp:
                return "主题词不合规（2-6 字、不能是情绪/日常这类泛词），这次没记。"
            store = self._db()
            if not store.set_topics(int(memory_id), tp):
                return f"没找到 #{int(memory_id)}。"
            await asyncio.to_thread(self._apply_topics, {int(memory_id): tp})
            return f"给 #{int(memory_id)} 起了名：{'、'.join(tp)}"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] tag_memory 出错：{type(e).__name__}: {e}")
            return "起名没成功。"

    @filter.llm_tool(name="read_memory")
    async def read_memory(self, event: AstrMessageEvent, memory_id: int = 0, original: bool = False):
        '''深读一条记忆：看它的版本链、旁注，以及当时那段对话的原文。

        Args:
            memory_id(int): 记忆编号（从 recall_memory 或目录里看到的那种 #编号）
            original(bool): 想看当时的原话/那段对话就设为 True；默认只看摘要
        '''
        try:
            store = self._db()
            t = store.get_trace(int(memory_id), history=True)
        except Exception as e:  # noqa: BLE001
            return f"读不了：{type(e).__name__}"
        if not t:
            return "没有这条。"
        out = [f"#{t['id']} [{t['kind']}] 当前 v{t['version_no']}：{t['text']}"]
        if len(t.get("chain") or []) > 1:
            out.append("版本链：")
            for v in t["chain"]:
                out.append(f"  v{v['version_no']}（{v['created_at'][:10]}，{v['reason']}）：{v['text']}")
        _notes = t.get("notes") or []
        _words = [n for n in _notes if (n.get("kind") or "") == "her_words"]
        _quotes = [n for n in _notes if (n.get("kind") or "") == "quote"]
        _other = [n for n in _notes if (n.get("kind") or "") not in ("her_words", "quote")]
        if _words:
            out.append(f"她自己的话：{_words[-1]['text']}")
        if _quotes:
            out.append(f"她留的原话：{_quotes[-1]['text']}")
        quote_ok = True
        if t.get("anchor_quote"):
            try:
                quote_ok = store.anchor_quote_ok(int(memory_id), t["anchor_quote"])
            except Exception:
                quote_ok = True
        if t.get("anchor_quote") and quote_ok:
            out.append(f"当时的原话（提炼时留的）：{t['anchor_quote']}")
        if t.get("felt"):
            out.append(f"当时感觉：{t['felt']}")
        if t.get("reaction"):
            out.append(f"我的反应：{t['reaction']}")
        if t.get("inference"):
            c = t.get("inference_certainty")
            out.append(f"推断（{c:.1f}）：{t['inference']}" if isinstance(c, (int, float)) else f"推断：{t['inference']}")
        if _other:
            out.append("旁注：" + "；".join(n["text"][:200] for n in _other[:3]))
        try:
            neighbors = store.temporal_neighbors(int(memory_id), limit=5)
        except Exception:
            neighbors = []
        if neighbors:
            kind_name = {"fact": "事实", "observation": "观察",
                         "self": "自我", "speculation": "推测"}
            out.append("【同一段里】")
            for n in neighbors:
                gap = f" · 约 {n['delta_min']} 分钟" if n.get("delta_min") is not None else ""
                out.append(f"- {kind_name.get(n['kind'], n['kind'])} #{n['id']}{gap}：{n['text'][:80]}")
            out.append("（只是同一段对话，不一定相关。）")
        if original:
            try:
                transcript = store.episode_transcript(int(memory_id))
            except Exception:
                transcript = ""
            if transcript:
                out.append("—— 当时那段对话 ——")
                out.append(transcript[:1200] + ("\n……（后面还有）" if len(transcript) > 1200 else ""))
            elif t.get("anchor_quote") and quote_ok:
                out.append("（只留下这一句原话，那段对话没有存档。）")
            else:
                out.append("（这段对话没有留下原文。）")
        return "\n".join(out)

    @filter.llm_tool(name="memory_directory")
    async def memory_directory(self, event):
        '''看一眼你自己记忆的目录（只有轮廓：主题、数量、未了的事），没想起从哪翻的时候用。
        '''
        try:
            d = self._db().directory()
        except Exception as e:  # noqa: BLE001
            return f"翻不动：{type(e).__name__}"
        parts = [f"共 {d['total']} 条：" + "、".join(f"{k} {v}" for k, v in d["by_kind"].items())]
        if d["tags"]:
            parts.append("主题：" + "、".join(f"{g['tag']}（{g['count']}）" for g in d["tags"][:10]))
        if d["unresolved"]:
            parts.append("未了的事：" + "；".join(f"#{x['id']} {x['text']}" for x in d["unresolved"][:5]))
        if d["most_recalled"]:
            parts.append("想起最多的：" + "；".join(f"#{x['id']} 想起{x['activations']}次" for x in d["most_recalled"][:5]))
        return "\n".join(parts)

    @filter.llm_tool(name="list_unresolved")
    async def list_unresolved(self, event):
        '''列出现在还没了结的事（没回的、答应过的、等着问的）。
        '''
        try:
            store = self._db()
            rows = store.conn.execute(
                """SELECT t.id, t.kind, v.text, t.created_at, t.due_at,
                          t.due_hint, t.scope, t.episode_id
                   FROM trace t JOIN trace_version v ON v.id=t.head_version_id
                   WHERE t.kind IN ('unresolved','promise') AND t.status='active'
                     AND COALESCE(t.state,'open') IN ('open','missed')
                   ORDER BY t.id DESC LIMIT 8""").fetchall()
        except Exception as e:  # noqa: BLE001
            return f"翻不动：{type(e).__name__}"
        if not rows:
            return "没有未了的事。"
        sessions = _session_map(store, [r["episode_id"] for r in rows])
        lines = []
        for r in rows:
            due = str(r["due_at"] or "")
            hint = str(r["due_hint"] or "")
            when = _fmt_when(due or r["created_at"])
            if when:
                when = ("到期 " + when) if due else ("笼统 " + hint if hint else "记下 " + when)
            scene = _scene_label(str(r["scope"] or ""),
                                 sessions.get(int(r["episode_id"] or 0), ""))
            header = " · ".join(x for x in (when, scene) if x)
            if lines:
                lines.append("")
            if header:
                lines.append(header)
            lines.append(f"#{r['id']} [{r['kind']}] {r['text']}")
        return "\n".join(lines)


    @filter.llm_tool(name="review_loose_ends")
    async def review_loose_ends(self, event: AstrMessageEvent, limit: int = 5):
        """翻一翻久了没再提的事。想放下哪件，就调 settle_memory 把它了结；还惦记就留着。

        Args:
            limit(int): 最多看几条，默认 5
        """
        try:
            store = self._db()
            rows = store.conn.execute(
                """SELECT t.id, t.kind, t.created_at, t.due_hint, t.scope, t.episode_id,
                          COALESCE(t.state,'open') AS st, v.text
                   FROM trace t JOIN trace_version v ON v.id=t.head_version_id
                   WHERE t.kind IN ('unresolved','promise','agreement') AND t.status='active'
                     AND t.avoided=0
                     AND COALESCE(t.state,'open') IN ('stale','old')
                   ORDER BY t.id DESC LIMIT ?""", (int(limit or 5),)).fetchall()
        except Exception as e:  # noqa: BLE001
            return f"翻不动：{type(e).__name__}"
        if not rows:
            return "没有沉下去的事。"
        sessions = _session_map(store, [r["episode_id"] for r in rows])
        lines = ["这些一直没下文："]
        for r in rows:
            when = _fmt_when(r["created_at"])
            hint = str(r["due_hint"] or "")
            scene = _scene_label(str(r["scope"] or ""),
                                 sessions.get(int(r["episode_id"] or 0), ""))
            stamp = ("笼统 " + hint) if hint else (("记下 " + when) if when else "")
            header = " · ".join(x for x in (stamp, scene) if x)
            if header:
                lines.append("")
                lines.append(header)
            tag = "旧事" if r["st"] == "old" else "沉默"
            lines.append(f"#{r['id']}（{tag}）[{r['kind']}] {r['text'][:80]}")
        lines.append("想放下哪件，就用 settle_memory 了结；还惦记就留着，不用现在决定。")
        return "\n".join(lines)

    @filter.llm_tool(name="avoid_memory")
    async def avoid_memory(self, event: AstrMessageEvent, memory_id: int = 0,
                           avoid: bool = True, reason: str = ""):
        '''让某条记忆不再主动浮现（或取消）。想绕开某件事、不想再被它勾住时用。

        Args:
            memory_id(int): 记忆编号（#后面的数字）
            avoid(bool): True 表示不再主动浮现，False 表示取消
            reason(string): 可选，为什么不想碰它
        '''
        try:
            store = self._db()
            if avoid:
                store.mark_avoided(int(memory_id), reason[:60])
            else:
                store.conn.execute("UPDATE trace SET avoided=0, avoid_reason=NULL, status='active' WHERE id=?",
                                   (int(memory_id),))
                store.conn.commit()
            return "好，这条以后不主动想了。" if avoid else "好，这条可以再想了。"
        except Exception as e:  # noqa: BLE001
            return f"没改成功：{type(e).__name__}"

    @filter.llm_tool(name="write_self_model")
    async def write_self_model(self, event: AstrMessageEvent, text: str):
        '''写下你现在的自我模型：你怎么看自己、在意什么、最近在改什么。

        只能你自己写；系统不代写。旧版本会保留。

        Args:
            text(string): 1–800 字，第一人称
        '''
        text = (text or "").strip()
        if not text:
            return "没写内容。"
        if len(text) > 800:
            text = text[:800]
        try:
            self._db().add_self_model(text)
            return "写下了。旧的还在。"
        except Exception as e:  # noqa: BLE001
            return f"没写下：{type(e).__name__}"

    @filter.llm_tool(name="read_self_model")
    async def read_self_model(self, event: AstrMessageEvent):
        '''读你自己写下的自我模型；不总结、不评价。'''
        try:
            st = self._db()
            cur = st.current_self_model()
            hist = st.self_model_history(4)
        except Exception as e:  # noqa: BLE001
            return f"读不了：{type(e).__name__}"
        if not cur:
            return "你还没有写下自我模型。"
        lines = [f"现在的自我模型（{cur['created_at'][:16]}）：", cur["text"]]
        older = [x for x in hist if x["id"] != cur["id"]]
        if older:
            lines.append("以前的：")
            for x in older[:3]:
                lines.append(f"- {x['created_at'][:10]}：{x['text'][:200]}")
        return "\n".join(lines)

    @filter.llm_tool(name="read_person_impression")
    async def read_person_impression(self, event: AstrMessageEvent, name: str = ""):
        '''读一个人的人物印象，按“她对我来说是谁”来看，不是用户档案。

        只在私聊用；群聊不读，避免把私聊印象带进群。

        Args:
            name(string): 人物名；留空 = 当前私聊对象
        '''
        try:
            if not event.is_private_chat():
                return "人物印象只在私聊看。"
        except Exception:
            pass
        base = os.path.dirname(os.path.dirname(self.db_path))
        who = (name or "").strip()
        try:
            if not who:
                who = event.get_sender_name() or ""
            qq = event.get_sender_id() or ""
            d = _find_person_impression(base, who, qq)
        except Exception as e:  # noqa: BLE001
            return f"读不了人物印象：{type(e).__name__}"
        if not d:
            return "还没有这个人的印象。"
        lines = [f"【人物印象 · {d.get('name') or who}】"]
        if d.get("relationship"):
            lines.append("关系：" + str(d["relationship"]))
        if d.get("summary"):
            lines.append("我眼里的这个人：" + str(d["summary"])[:240])
        if d.get("special_comment"):
            lines.append("我自己的话：" + str(d["special_comment"])[:160])
        if d.get("preferences"):
            lines.append("相处时记得：" + "、".join(map(str, d["preferences"][:6])))
        if d.get("evidence_dates"):
            lines.append("证据日期：" + "、".join(map(str, d["evidence_dates"][-5:])))
        lines.append("（旧档案，可能过时；细节用 read_impression。）")
        return "\n".join(lines)

    @filter.llm_tool(name="read_self")
    async def read_self(self, event: AstrMessageEvent, days: int = 7):
        '''看看最近的自己：你自己选择留下的自我观察和选择。

        只列原文，不总结你是什么样的人。

        Args:
            days(int): 最近几天，默认 7
        '''
        try:
            from datetime import date, timedelta
            n = max(1, min(30, int(days or 7)))
            since = (date.today() - timedelta(days=n - 1)).isoformat()
            store = self._db()
            rows = store.conn.execute(
                """SELECT t.id, v.text, COALESCE(v.anchor_time, t.created_at) AS at
                   FROM trace t
                   JOIN trace_version v ON v.id=t.head_version_id
                   JOIN episode e ON e.id=t.episode_id
                   WHERE t.kind='self' AND t.status='active' AND t.avoided=0
                     AND e.session_id=? AND date(COALESCE(v.anchor_time, t.created_at))>=date(?)
                   ORDER BY at DESC LIMIT 8""",
                (event.unified_msg_origin, since)).fetchall()
        except Exception as e:  # noqa: BLE001
            return f"看不了最近的自己：{type(e).__name__}"
        lines = [f"最近 {n} 天，你自己选择留下的："]
        if rows:
            lines.append("【自我记忆】")
            for r in rows:
                lines.append(f"- {_fmt_when(str(r['at'] or ''))} #{r['id']}：{str(r['text'] or '')[:160]}")
        if len(lines) == 1:
            return f"最近 {n} 天，你没有留下关于自己的东西。"
        lines.append("（只是列出来；怎么看你、要不要接着想，你自己定。）")
        return "\n".join(lines)

    @filter.llm_tool(name="self_think")
    async def self_think_tool(self, event: AstrMessageEvent, action: str = "start"):
        '''自己决定安静想一会儿，或者结束这次思考。

        想自己待一会儿、整理一件事、想想自己时用。
        开始后不会发消息，只写自己的记录；有人找你会自动暂停。

        Args:
            action(string): start=开始/接续；stop=不想了，结束这次
        '''
        if not bool(self._cfg("self_think_enabled", False)):
            return "自我思考现在是关着的。"
        act = (action or "start").strip().lower()
        if act in ("stop", "end", "停", "结束"):
            res = await self._self_thinker.stop_now()
            return "好，不想了。" if res == "stopped" else "现在没有在想的。"
        res = await self._self_thinker.force()
        return {
            "started": "好，我自己想一会儿。",
            "resumed": "接着刚才想。",
            "thinking": "已经在想了。",
            "no_session": "没有可用的私聊会话，想不了。",
        }.get(res, "好。")

    # ---------- 命令 ----------

    @filter.command("自我思考")
    async def self_think_now(self, event: AstrMessageEvent, arg: str = ""):
        """管理员手动叫她开始想；可以带一条记忆编号，如 /自我思考 #2919。"""
        if not event.is_admin():
            yield event.plain_result("这个只有管理员能叫。")
            return
        if not bool(self._cfg("self_think_enabled", False)):
            yield event.plain_result("自我思考现在是关着的。")
            return
        seed_id = 0
        raw = (arg or "").strip().lstrip("#").strip()
        if raw:
            if not raw.isdigit():
                yield event.plain_result("用法：/自我思考 或 /自我思考 #记忆编号")
                return
            seed_id = int(raw)
            try:
                t = self._db().get_trace(seed_id)
            except Exception:
                t = None
            if not t:
                yield event.plain_result(f"没找到 #{seed_id}。")
                return
        try:
            res = await self._self_thinker.force(seed_id)
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"没叫醒：{type(e).__name__}")
            return
        msg = {
            "started": f"好，去看看 #{seed_id}。" if seed_id else "好，现在开始想。",
            "resumed": f"接着想，带上 #{seed_id}。" if seed_id else "接着上次想。",
            "thinking": f"已经在想了，把 #{seed_id} 放进去。" if seed_id else "已经在想了。",
            "no_session": "没有可用的私聊会话，想不了。",
        }.get(res, "好。")
        yield event.plain_result(msg)

    @filter.command("mem")
    async def mem(self, event: AstrMessageEvent, sub: str = "", arg: str = ""):
        if not event.is_admin():
            yield event.plain_result("这个只有管理员能翻。")
            return
        try:
            store = self._db()
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"打不开记忆库：{e}")
            return
        sub = (sub or "").strip()
        arg = (arg or "").strip()

        if sub in ("dir", ""):
            d = store.directory()
            lines = [f"共 {d['total']} 条 ｜ " + " ".join(f"{k}:{v}" for k, v in d["by_kind"].items())]
            lines.append("标签：" + "｜".join(f"{g['tag']}({g['count']})" for g in d["tags"]))
            if d["unresolved"]:
                lines.append("未了的事：")
                lines += [f"  #{x['id']} {x['text']}" for x in d["unresolved"]]
            if d["most_recalled"]:
                lines.append("想起最多的：")
                lines += [f"  #{x['id']} 想起{x['activations']}次 {x['text']}"
                          for x in d["most_recalled"]]
            yield event.plain_result("\n".join(lines))

        elif sub == "show" and arg:
            t = store.get_trace(int(arg))
            if not t:
                yield event.plain_result("没有这条")
                return
            yield event.plain_result(
                f"#{t['id']} [{t['kind']}] 重要度{t['importance']:.2f} 稳定度{t['stability']}天 "
                f"R={t['r_now']} 想起{t['activations']}次 版本v{t['version_no']}\n"
                f"标签：{'/'.join(t['tags'])}\n{t['text'][:600]}"
            )

        elif sub == "history" and arg:
            t = store.get_trace(int(arg), history=True)
            if not t:
                yield event.plain_result("没有这条")
                return
            out = [f"#{t['id']} 版本链（当前 v{t['version_no']}）"]
            for v in t["chain"]:
                out.append(f"v{v['version_no']} {v['created_at'][:16]} {v['reason']} [{v['change_class']}]\n  {v['text'][:300]}")
            yield event.plain_result("\n".join(out))

        elif sub == "search" and arg:
            hits = store.search(arg)
            yield event.plain_result("\n".join(f"#{h['id']} {h['text'][:70]}" for h in hits) if hits else "没搜到")

        elif sub == "touch" and arg:
            res = store.activate(int(arg), cue_type="manual", query="cmd")
            t = store.get_trace(int(arg))
            yield event.plain_result(f"想起一次：R {res['r_before']} → 稳定度 {res['s_after']}天，现在 R={t['r_now']}")

        elif sub == "pending":
            n = store.pending_count(event.unified_msg_origin)
            yield event.plain_result(f"缓冲里攒了 {n} 行，攒到 {self._cfg('batch_size', 8)} 行会提炼。")

        elif sub == "flush":
            n = await self._flush(event.unified_msg_origin, self._scope(event), "manual")
            yield event.plain_result(f"提炼完成，落痕 {n} 条。")

        elif sub == "merge":
            import asyncio as _aio
            from . import consolidate
            apply = arg.strip().lower() in ("apply", "yes", "1")
            stats = await _aio.to_thread(consolidate.run, store, apply)
            lines = [f"聚类 {stats['clusters']} 个 · 判为重复 {stats['same']} 个"
                     + (f" · 已合并 {stats['merged']} 条 → 新痕迹 {stats['new_ids']}" if apply else "（预览，没写库）")]
            for p in stats["preview"][:6]:
                v = p["verdict"]
                lines.append(("✔ " if v["same"] else "✘ ") + "、".join(f"#{i}" for i in p["ids"]) + f"  {v['why']}")
                if v["same"] and v["merged"]:
                    lines.append("   → " + v["merged"])
            yield event.plain_result("\n".join(lines))

        elif sub == "stats":
            s = store.stats()
            yield event.plain_result(
                f"episode {s['episodes']} ｜ trace {s['traces']} ｜ 版本 {s['versions']} ｜ 回想 {s['recalled']}\n{s['by_kind']}")

        else:
            yield event.plain_result(HELP)
