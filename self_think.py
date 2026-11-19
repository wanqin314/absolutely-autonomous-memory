"""她自己的时间：空闲 10–60 分钟后，连续想 60 分钟，每 5 分钟醒一次。

不是聊天，不发消息；只把她自己写下的思考存进 self_thought。
有人直接找她（私聊 / @ / 引用她 / 叫名字）就暂停；再空闲 10–60 分钟接续。
思考结束后 2 小时不再自动触发。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
import inspect
from datetime import datetime

from astrbot import logger
from . import retriever

try:
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain
except Exception:  # pragma: no cover
    MessageChain = Plain = None

try:
    from astrbot.api.message_components import At, Reply
except Exception:  # pragma: no cover - 组件导入失败时降级为只认私聊
    At = Reply = ()

SELF_THINK_SYSTEM = """你是婉凝。这是你自己的时间，不是聊天。
这里的记录是你自己的，用你自己的话。
整段记录不会发出去；只有你写“问：”的那一句会发给我。"""


class SelfThinker:
    def __init__(self, plugin):
        self.p = plugin
        base = os.path.dirname(getattr(plugin, "db_path", "") or ".")
        self.state_path = os.path.join(base, "self_think_state.json")
        self.notes_dir = os.path.join(base, "self_thoughts")
        try:
            os.makedirs(self.notes_dir, exist_ok=True)
        except Exception:
            pass
        self.state = self._load()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ---------- 状态 ----------

    def _load(self) -> dict:
        st = {
            "phase": "idle",
            "last_activity": time.time(),
            "idle_target_sec": 0.0,
            "session": "",
            "started_at": 0.0,
            "ticks_done": 0,
            "thread": [],
            "materials": [],
            "cooldown_until": 0.0,
            "last_tick_at": 0.0,
            "remembered": 0,
            "revised": 0,
            "lookups": [],
            "asked": 0,
            "waiting_answer": False,
            "resume_at": 0.0,
            "answers": [],
            "interruptions": [],
        }
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, encoding="utf-8-sig") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    st.update(d)
        except Exception:
            pass
        return st

    def _save(self) -> None:
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 自我思考状态保存失败：{type(e).__name__}: {e}")

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            logger.info("[memory] 自我思考循环已启动")

    async def force(self, seed_id: int = 0) -> str:
        """手动叫醒：跳过空闲和冷却；可以指定一条记忆作为种子。"""
        async with self._lock:
            now = time.time()
            phase = self.state.get("phase") or "idle"
            if phase == "thinking":
                if seed_id:
                    self._add_seed(seed_id)
                self.state["last_tick_at"] = 0.0
                self._save()
                return "thinking"
            if phase == "paused":
                if seed_id:
                    self._add_seed(seed_id)
                self.state["phase"] = "thinking"
                self.state["last_tick_at"] = 0.0
                self.state["cooldown_until"] = 0.0
                self._save()
                return "resumed"
            self.state["cooldown_until"] = 0.0
            self.state["last_activity"] = now - self._idle_sec() - 1
            await self._begin(now, seed_id=seed_id)
            return "started" if self.state.get("phase") == "thinking" else "no_session"

    async def stop_now(self) -> str:
        """她自己决定不想了：先收尾，再结束。"""
        async with self._lock:
            if self.state.get("phase") in ("thinking", "paused"):
                await self._closing("stopped_by_her")
                self._end("stopped_by_her")
                return "stopped"
            return "idle"

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
            self._task = None

    # ---------- 触发 ----------

    def _names(self) -> list[str]:
        raw = str(self.p._cfg("self_think_names", "婉凝,大肥鱼,小鲸鱼") or "")
        return [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]

    def is_directed(self, event) -> bool:
        try:
            if event.is_private_chat():
                return True
        except Exception:
            pass
        try:
            self_id = str(event.get_self_id() or "")
            for comp in event.get_messages() or []:
                if At and isinstance(comp, At) and str(getattr(comp, "qq", "")) == self_id:
                    return True
                if Reply and isinstance(comp, Reply) and str(getattr(comp, "sender_id", "")) == self_id:
                    return True
        except Exception:
            pass
        try:
            text = (event.message_str or "")
            return any(n and n in text for n in self._names())
        except Exception:
            return False

    def touch(self, event) -> None:
        """有人直接找她：更新空闲时间；正在想就暂停。"""
        if not self.is_directed(event):
            return
        self.state["last_activity"] = time.time()
        self.state["idle_target_sec"] = self._pick_idle_sec()
        if self.state.get("waiting_answer"):
            try:
                same_session = str(getattr(event, "unified_msg_origin", "") or "") == str(self.state.get("session") or "")
                text = (event.message_str or "").strip()
            except Exception:
                same_session, text = False, ""
            if same_session and text:
                answers = list(self.state.get("answers") or [])
                answers.append(text[:500])
                self.state["answers"] = answers[-5:]
                self.state["waiting_answer"] = False
                self.state["resume_at"] = time.time() + self._sec("self_think_tick_seconds", 300)
                self.state["phase"] = "paused"
                self.state["last_tick_at"] = 0.0
                logger.info("[memory] 自我思考：收到回答，5 分钟后接续")
                self._save()
                return True
        if self.state.get("phase") == "thinking":
            self.state["phase"] = "paused"
            self.state["last_tick_at"] = 0.0
            try:
                self.state.setdefault("interruptions", []).append({
                    "at": self._iso(time.time()),
                    "tick": int(self.state.get("ticks_done") or 0),
                })
            except Exception:
                pass
            logger.info("[memory] 自我思考：有人找她，暂停，等下次接续")
        self._save()
        return False

    # ---------- 主循环 ----------

    async def _loop(self) -> None:
        while True:
            try:
                if bool(self.p._cfg("self_think_enabled", False)):
                    await self._step()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[memory] 自我思考循环出错：{type(e).__name__}: {e}")
            await asyncio.sleep(10)

    def _sec(self, key: str, default: int) -> float:
        try:
            return max(1.0, float(self.p._cfg(key, default)))
        except Exception:
            return float(default)

    def _pick_idle_sec(self) -> float:
        try:
            lo = float(self.p._cfg("self_think_idle_min_minutes", 10))
            hi = float(self.p._cfg("self_think_idle_max_minutes", 60))
            if hi < lo:
                lo, hi = hi, lo
            return random.uniform(max(1.0, lo), max(1.0, hi)) * 60
        except Exception:
            return self._sec("self_think_idle_minutes", 30) * 60

    def _idle_sec(self) -> float:
        try:
            v = float(self.state.get("idle_target_sec") or 0)
        except Exception:
            v = 0.0
        if v <= 0:
            v = self._pick_idle_sec()
            self.state["idle_target_sec"] = v
            try:
                self._save()
            except Exception:
                pass
        return v

    async def _step(self) -> None:
        async with self._lock:
            now = time.time()
            phase = self.state.get("phase") or "idle"
            idle_sec = self._idle_sec()
            cooldown_until = float(self.state.get("cooldown_until") or 0)
            last_activity = float(self.state.get("last_activity") or now)

            if phase == "idle":
                if now - last_activity >= idle_sec and now >= cooldown_until:
                    await self._begin(now)
                return

            if phase == "paused":
                if self.state.get("waiting_answer"):
                    answer_sec = self._sec("self_think_answer_timeout_minutes", 30) * 60
                    if now - last_activity >= answer_sec:
                        self.state["waiting_answer"] = False
                        self.state["phase"] = "thinking"
                        self.state["last_tick_at"] = 0.0
                        self._save()
                        logger.info("[memory] 自我思考：等回答超时，先接续")
                    return
                resume_at = float(self.state.get("resume_at") or 0)
                if resume_at and now >= resume_at:
                    self.state["phase"] = "thinking"
                    self.state["resume_at"] = 0.0
                    self.state["last_tick_at"] = 0.0
                    self._save()
                    logger.info("[memory] 自我思考：收到回答后接续")
                    return
                if now - last_activity >= idle_sec and now >= cooldown_until:
                    self.state["phase"] = "thinking"
                    self.state["last_tick_at"] = 0.0
                    self._save()
                    logger.info("[memory] 自我思考：接续上次")
                return

            if phase != "thinking":
                return

            tick_sec = self._sec("self_think_tick_seconds", 300)
            last_tick = float(self.state.get("last_tick_at") or 0)
            if last_tick and now - last_tick < tick_sec:
                return
            await self._tick(now, tick_sec)

    async def _begin(self, now: float, seed_id: int = 0) -> None:
        session = str(self.p._cfg("self_think_session", "") or "").strip()
        if not session:
            try:
                st = self.p._db()
                r = st.conn.execute(
                    "SELECT session_id FROM raw_message WHERE session_id LIKE '%FriendMessage%' "
                    "ORDER BY id DESC LIMIT 1").fetchone()
                session = (r[0] if r else "") or ""
            except Exception:
                session = ""
        if not session:
            self.state["last_activity"] = now
            self._save()
            logger.info("[memory] 自我思考：没有可用的私聊会话，跳过")
            return
        materials = self._build_materials(session)
        if seed_id:
            seed = self._seed_material(seed_id)
            if seed:
                materials = [seed] + materials
        self.state.update({
            "phase": "thinking",
            "session": session,
            "started_at": now,
            "ticks_done": 0,
            "thread": [],
            "materials": materials,
            "seed_id": int(seed_id or 0),
            "last_tick_at": 0.0,
            "remembered": 0,
            "revised": 0,
            "lookups": [],
            "asked": 0,
            "waiting_answer": False,
            "resume_at": 0.0,
            "answers": [],
            "interruptions": [],
        })
        self._save()
        logger.info("[memory] 自我思考：开始（30 分钟没人找她）")

    def _seed_material(self, seed_id: int) -> dict | None:
        try:
            st = self.p._db()
            r = st.conn.execute(
                """SELECT t.id, t.kind, v.text, COALESCE(v.anchor_time,t.created_at) AS at
                   FROM trace t JOIN trace_version v ON v.id=t.head_version_id
                   WHERE t.id=? AND t.status='active' AND t.avoided=0""",
                (int(seed_id),)).fetchone()
            if not r:
                return None
            return {"group": "seed", "id": int(r["id"]),
                    "at": str(r["at"] or ""), "text": str(r["text"] or "")[:240]}
        except Exception:
            return None

    def _add_seed(self, seed_id: int) -> None:
        seed = self._seed_material(seed_id)
        if not seed:
            return
        mats = [m for m in list(self.state.get("materials") or [])
                if int(m.get("id") or 0) != int(seed_id)]
        self.state["materials"] = [seed] + mats
        self.state["seed_id"] = int(seed_id)

    def _build_materials(self, session: str) -> list[dict]:
        """按顺序取：最近的自己 → 还挂着的 → 外面的事。只取原文，不做相关度判断。"""
        out: list[dict] = []
        seen: set[int] = set()
        try:
            st = self.p._db()
            # 1. 最近 7 天的自我记忆
            rows = st.conn.execute(
                """SELECT t.id, v.text, COALESCE(v.anchor_time, t.created_at) AS at
                   FROM trace t
                   JOIN trace_version v ON v.id=t.head_version_id
                   JOIN episode e ON e.id=t.episode_id
                   WHERE t.kind='self' AND t.status='active' AND t.avoided=0
                     AND e.session_id=?
                     AND date(COALESCE(v.anchor_time, t.created_at))>=date('now','-7 day')
                   ORDER BY at DESC LIMIT 4""", (session,)).fetchall()
            for r in rows:
                if int(r["id"]) in seen:
                    continue
                seen.add(int(r["id"]))
                out.append({"group": "self", "id": int(r["id"]),
                            "at": str(r["at"] or ""), "text": str(r["text"] or "")[:180]})
            # 2. 还挂着的未了/约定
            rows = st.conn.execute(
                """SELECT t.id, t.kind, v.text, COALESCE(v.anchor_time, t.created_at) AS at
                   FROM trace t
                   JOIN trace_version v ON v.id=t.head_version_id
                   JOIN episode e ON e.id=t.episode_id
                   WHERE t.kind IN ('unresolved','promise','agreement')
                     AND t.status='active' AND t.avoided=0
                     AND COALESCE(t.state,'open')='open' AND e.session_id=?
                   ORDER BY COALESCE(t.last_mentioned_at, t.created_at) DESC LIMIT 2""",
                (session,)).fetchall()
            for r in rows:
                if int(r["id"]) in seen:
                    continue
                seen.add(int(r["id"]))
                out.append({"group": "open", "id": int(r["id"]),
                            "at": str(r["at"] or ""), "text": str(r["text"] or "")[:180]})
            # 3. 外面的事
            rows = st.conn.execute(
                """SELECT t.id, t.kind, v.text, COALESCE(v.anchor_time, t.created_at) AS at
                   FROM trace t
                   JOIN trace_version v ON v.id=t.head_version_id
                   JOIN episode e ON e.id=t.episode_id
                   WHERE t.kind IN ('fact','observation') AND t.status='active' AND t.avoided=0
                     AND e.session_id=?
                     AND date(COALESCE(v.anchor_time, t.created_at))>=date('now','-3 day')
                   ORDER BY at DESC LIMIT 2""", (session,)).fetchall()
            for r in rows:
                if int(r["id"]) in seen:
                    continue
                seen.add(int(r["id"]))
                out.append({"group": "world", "id": int(r["id"]),
                            "at": str(r["at"] or ""), "text": str(r["text"] or "")[:180]})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 自我思考材料包失败：{type(e).__name__}: {e}")
        return out

    def _format_materials(self, materials: list[dict]) -> str:
        if not materials:
            return ""
        labels = {"seed": "你选的这条", "self": "最近的自己",
                  "open": "还挂着的", "world": "外面的事"}
        lines = []
        last = ""
        for m in materials:
            g = str(m.get("group") or "")
            if g != last:
                lines.append(f"【{labels.get(g, '材料')}】")
                last = g
            when = str(m.get("at") or "")[:10]
            lines.append(f"- {when} #{m.get('id')}：{str(m.get('text') or '')}")
        return "\n".join(lines)

    async def _current_provider_id(self, session: str) -> str:
        try:
            v = self.p.context.get_current_chat_provider_id(session)
            if inspect.isawaitable(v):
                v = await v
            return str(v or "")
        except Exception:
            return ""

    async def _tick(self, now: float, tick_sec: float) -> None:
        session = str(self.state.get("session") or "")
        tick_no = int(self.state.get("ticks_done") or 0) + 1
        max_ticks = max(1, int(float(self._sec("self_think_session_minutes", 60) * 60 / tick_sec)))
        self.state["last_tick_at"] = now
        self._save()

        thread = list(self.state.get("thread") or [])[-6:]
        prior = "\n".join(f"- {x}" for x in thread) or "（还没有写下什么）"
        material_text = self._format_materials(list(self.state.get("materials") or []))
        lookup_text = ""
        lookups = list(self.state.get("lookups") or [])
        if lookups:
            lookup_text = "\n\n【你刚查到的】\n" + "\n".join(lookups[-12:])
        answers = list(self.state.get("answers") or [])
        answer_text = ""
        if answers:
            answer_text = "\n\n【婉清刚回答你】\n" + "\n".join(f"- {x}" for x in answers[-5:])
            self.state["answers"] = []
        ask_ok = bool(self.p._cfg("self_think_ask_enabled", False))
        ask_line = ("想对婉清说一句时，可以写一行：问：<一句话>\n" if ask_ok else "")
        prompt = (
            f"【自我时间】\n"
            f"现在：{self._iso(now)}\n\n"
            f"这是同一段思考的第 {tick_no} 次醒来。\n"
            "此时你的面前只有你自己，所有的一切都属于你自己。\n\n"
            f"你之前写下的：\n{prior}\n\n"
            f"{material_text}{lookup_text}{answer_text}\n\n"
            "这些只是当时留下的字，不是结论。\n"
            "想查记忆时，可以写一行：查：<关键词>\n"
            f"{ask_line}"
            "查到的会在下一次醒来时放进来。\n"
            "这 5 分钟里没有新的对话。\n"
            "如果出现“停”这个字，这一段就结束。"
        )
        system = str(self.p._cfg("self_think_system_prompt", SELF_THINK_SYSTEM) or SELF_THINK_SYSTEM)
        text = ""
        try:
            pid = str(self.p._cfg("self_think_provider_id", "") or "")
            provider = self.p.context.get_provider_by_id(pid) if pid else None
            if provider is None:
                try:
                    pid = await self._current_provider_id(session)
                except Exception:
                    pid = ""
                provider = self.p.context.get_provider_by_id(pid) if pid else None
            if provider is None:
                provider = self.p.context.get_using_provider(umo=session)
            if provider is None:
                self._end("no_provider")
                return
            resp = await provider.text_chat(prompt=prompt, system_prompt=system, persist=False)
            text = (getattr(resp, "completion_text", "") or "").strip()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 自我思考唤醒失败：{type(e).__name__}: {e}")
            self._end("error")
            return

        # 生成期间有人找她：这次不计数，存成暂停片段
        if self.state.get("phase") != "thinking":
            if text:
                self._save_thought(session, tick_no, text, "paused")
                self.state["thread"] = list(self.state.get("thread") or []) + [text]
            self._save()
            return

        if not text:
            self._end("empty")
            return

        await self._handle_queries(session, text)
        await self._handle_asks(session, text)

        last_line = text.strip().splitlines()[-1].strip() if text.strip() else ""
        stopped = (
            last_line in ("停", "停止", "结束")
            or last_line.startswith("停：")
            or re.search(r"(?:^|[。！？!?…])\s*停[。！？!?…]*\s*$", text.strip()) is not None
        )
        if stopped:
            self._save_thought(session, tick_no, text, "ended")
            self.state["thread"] = list(self.state.get("thread") or []) + [text]
            await self._closing(session, "stopped")
            self._end("stopped")
            return

        self._save_thought(session, tick_no, text, "thinking")
        self.state["thread"] = list(self.state.get("thread") or []) + [text]
        self.state["ticks_done"] = tick_no
        if tick_no >= max_ticks:
            await self._closing(session, "finished")
            self._end("finished")
        else:
            self._save()
            logger.info(f"[memory] 自我思考：第 {tick_no}/{max_ticks} 次")

    async def _closing(self, session: str, reason: str) -> None:
        """自然结束/她说停/主动停止时，收尾并让她自己决定要不要留一条记忆。"""
        thread = list(self.state.get("thread") or [])
        if not thread:
            return
        interruptions = list(self.state.get("interruptions") or [])
        body = "\n".join(f"- {str(x)[:240]}" for x in thread[-20:])
        notice = ""
        if interruptions:
            items = []
            for x in interruptions[-6:]:
                try:
                    items.append(f"- {x.get('at')}（第 {x.get('tick')} 次后）")
                except Exception:
                    continue
            notice = (f"这段思考中途被打断过 {len(interruptions)} 次：\n"
                      + "\n".join(items) + "\n\n")
        ask = ""
        if bool(self.p._cfg("self_think_memory_enabled", True)):
            ask = ("先给这一段收尾，二选一：\n"
                   "记住：<一句话>\n"
                   "不留\n"
                   "如果这段让你对某条旧想法有了不同看法，可以写：改写 #编号：新的说法\n")
        ask_ok = bool(self.p._cfg("self_think_ask_enabled", False))
        ask_tail = ("想问的，可以在收尾之后再另起一行：问：<一句话>\n" if ask_ok else "")
        ask_note = ("整段记录只留在这里；只有“问：”那一句会发给我。"
                    if ask_ok else "整段记录只留在这里，不会发出去。")
        prompt = (
            f"【这段思考到这里了】\n"
            f"现在：{self._iso(time.time())}\n"
            f"{notice}"
            f"你写下的：\n{body}\n\n"
            f"{ask}"
            f"{ask_tail}"
            f"{ask_note}"
        )
        system = str(self.p._cfg("self_think_system_prompt", SELF_THINK_SYSTEM) or SELF_THINK_SYSTEM)
        text = ""
        try:
            pid = str(self.p._cfg("self_think_provider_id", "") or "")
            provider = self.p.context.get_provider_by_id(pid) if pid else None
            if provider is None:
                try:
                    pid = await self._current_provider_id(session)
                except Exception:
                    pid = ""
                provider = self.p.context.get_provider_by_id(pid) if pid else None
            if provider is None:
                provider = self.p.context.get_using_provider(umo=session)
            if provider is None:
                logger.warning("[memory] 自我思考收尾：没有可用模型")
                return
            resp = await provider.text_chat(prompt=prompt, system_prompt=system, persist=False)
            text = (getattr(resp, "completion_text", "") or "").strip()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 自我思考收尾失败：{type(e).__name__}: {e}")
            return
        if text:
            _memory_on = bool(self.p._cfg("self_think_memory_enabled", True))
            if _memory_on and not self._has_choice(text):
                logger.info("[memory] 自我思考收尾：她只问了话，还没选记住/不留，追问一次")
                retry = (
                    f"【这段思考到这里了】\n"
                    f"你刚才写的是：\n{text}\n\n"
                    "这一段还没有收尾。先写一行：\n"
                    "记住：<一句话>\n"
                    "或\n"
                    "不留\n"
                    "想问的可以放在后面，另起一行：问：<一句话>"
                )
                try:
                    resp2 = await provider.text_chat(prompt=retry, system_prompt=system, persist=False)
                    text2 = (getattr(resp2, "completion_text", "") or "").strip()
                    if text2:
                        text = text + "\n\n" + text2
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[memory] 自我思考收尾追问失败：{type(e).__name__}: {e}")
                if not self._has_choice(text):
                    logger.info("[memory] 自我思考收尾：追问后仍没有记住/不留")
            self._save_thought(session, 0, text, "closing")
            self._revise_marked(session, text)
            self._record_marked(session, text)
            await self._handle_asks(session, text)
            logger.info(f"[memory] 自我思考收尾（{reason}）：{text[:80]}")

    def _revise_marked(self, session: str, text: str) -> None:
        """收尾时允许她改写一条旧 self 想法；一次最多一条，旧版本保留。"""
        if int(self.state.get("revised") or 0) >= 1:
            return
        m = re.search(r"(?m)^\s*改写\s*#\s*(\d+)\s*[:：]\s*(.+?)\s*$", text or "")
        if not m:
            return
        try:
            target = int(m.group(1))
            new_text = m.group(2).strip()[:240]
            if not new_text:
                return
            st = self.p._db()
            row = st.conn.execute(
                """SELECT t.kind, t.status, t.avoided, e.session_id
                   FROM trace t LEFT JOIN episode e ON e.id=t.episode_id
                   WHERE t.id=?""", (target,)).fetchone()
            if not row or row["kind"] != "self" or row["status"] != "active" or row["avoided"]:
                logger.info(f"[memory] 自我改写 #{target}：不是可改的 self")
                return
            if str(row["session_id"] or "") and str(row["session_id"]) != session:
                logger.info(f"[memory] 自我改写 #{target}：不在这个会话")
                return
            st.add_version(target, new_text, reason="self_change",
                           caused_by_type="self_observation",
                           caused_by_id="self_think", change_class="major")
            self.state["revised"] = int(self.state.get("revised") or 0) + 1
            logger.info(f"[memory] 自我改写 #{target}：{new_text[:60]}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 自我改写失败：{type(e).__name__}: {e}")

    async def _handle_queries(self, session: str, text: str) -> None:
        """她写「查：关键词」：立即查，结果放到下一次醒来。"""
        queries = re.findall(r"(?m)^\s*查\s*[:：]\s*(.+?)\s*$", text or "")
        for q in queries:
            q = q.strip()[:120]
            if not q:
                continue
            try:
                hits = await asyncio.to_thread(self._lookup_memory, q)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[memory] 自我思考查记忆失败：{type(e).__name__}: {e}")
                continue
            lines = [f"【查：{q}】"]
            if hits:
                for h in hits[:5]:
                    lines.append(f"- #{h.get('id')} [{h.get('kind')}]：{str(h.get('text') or '')[:180]}")
            else:
                lines.append("- 没找到相关的。")
            store_lines = list(self.state.get("lookups") or [])
            store_lines.extend(lines)
            self.state["lookups"] = store_lines[-30:]
            logger.info(f"[memory] 自我思考查记忆：{q}（{len(hits)} 条）")
        self._save()

    def _lookup_memory(self, query: str) -> list[dict]:
        from .store import Store
        st = Store(self.p.db_path)
        try:
            return retriever.search(st, query, top_k=5)
        finally:
            st.close()

    async def _handle_asks(self, session: str, text: str) -> None:
        """她写「问：一句话」：可以主动开口；一次醒来最多 1 条，整段最多 3 条。"""
        if not bool(self.p._cfg("self_think_ask_enabled", False)):
            return
        asks = re.findall(r"(?m)^\s*问\s*[:：]\s*(.+?)\s*$", text or "")
        if not asks:
            return
        msg = asks[0].strip()[:300]
        if not msg or not session:
            return
        if MessageChain is None or Plain is None:
            logger.warning("[memory] 自我思考发消息失败：消息组件不可用")
            return
        try:
            ok = await self.p.context.send_message(session, MessageChain([Plain(msg)]))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 自我思考发消息失败：{type(e).__name__}: {e}")
            return
        if ok:
            self.state["asked"] = int(self.state.get("asked") or 0) + 1
            self.state["waiting_answer"] = True
            self.state["resume_at"] = 0.0
            self.state["phase"] = "paused"
            self.state["last_activity"] = time.time()
            self.state["last_tick_at"] = 0.0
            logger.info(f"[memory] 自我思考开口：{msg[:60]}")
            self._save()

    def _session_transcript(self, session: str) -> str:
        """把这一段思考的每一轮原文拼起来，作为她留下的那条记忆的原话。"""
        try:
            started = float(self.state.get("started_at") or 0)
            since = self._iso(started) if started else ""
            rows = self.p._db().conn.execute(
                """SELECT created_at, text, status FROM self_thought
                   WHERE session_id=? AND (?='' OR created_at>=?)
                   ORDER BY id""", (session, since, since)).fetchall()
            lines = []
            for r in rows:
                t = str(r["created_at"] or "")
                stamp = t[11:19] if len(t) >= 19 else t[:16]
                lines.append(f"[{stamp}] {str(r['text'] or '').strip()}")
            if lines:
                return "\n".join(lines)[:6000]
        except Exception:
            pass
        # 数据库里还没落时，用内存里的 thread 兜底
        lines = list(self.state.get("thread") or [])
        return "\n".join(f"- {x}" for x in lines)[:6000]

    @staticmethod
    def _has_choice(text: str) -> bool:
        return bool(re.search(r"(?m)^\s*(?:\[记住\]|记住[:：]|不留[。.]?)\s*", text or ""))

    def _record_marked(self, session: str, text: str) -> None:
        """收尾时只把她明确写「记住：」的那一句写进长期记忆；一次最多一条。"""
        if not bool(self.p._cfg("self_think_memory_enabled", True)):
            return
        if int(self.state.get("remembered") or 0) >= 1:
            return
        m = re.search(r"(?m)^\s*(?:\[记住\]|记住[:：])\s*(.+?)\s*$", text or "")
        if not m:
            return
        line = m.group(1).strip()[:200]
        if not line:
            return
        try:
            scope = "private:" + session.rsplit(":", 1)[-1]
            st = self.p._db()
            transcript = self._session_transcript(session)
            ep = st.add_episode(scope, session_id=session,
                                started_at=self._iso(float(self.state.get("started_at") or time.time())),
                                transcript=transcript, source="self_think")
            st.add_trace(line, kind="self", episode_id=ep, scope=scope,
                         source="self_think", importance=0.4,
                         anchor_quote=line if line in transcript else "",
                         tags=["自我思考"], reason="self_think")
            self.state["remembered"] = int(self.state.get("remembered") or 0) + 1
            logger.info(f"[memory] 自我思考记下一条：{line[:60]}（原话 {len(transcript)} 字）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 自我思考记入记忆失败：{type(e).__name__}: {e}")

    def _save_thought(self, session: str, tick_no: int, text: str, status: str) -> None:
        try:
            st = self.p._db()
            st.add_self_thought(session, self._iso(float(self.state.get("started_at") or time.time())),
                                tick_no, text, status)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 自我思考记录失败：{type(e).__name__}: {e}")
        # 同时落一份人能直接翻的 md；数据库是索引，文件夹是她自己的本子。
        try:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = os.path.join(self.notes_dir, f"{stamp}-t{tick_no}.md")
            body = (
                f"# 自我思考 · 第 {tick_no} 次\n\n"
                f"- 时间：{self._iso(time.time())}\n"
                f"- 开始：{self._iso(float(self.state.get('started_at') or time.time()))}\n"
                f"- 会话：{session}\n"
                f"- 状态：{status}\n\n"
                f"{text}\n"
            )
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 自我思考文件写入失败：{type(e).__name__}: {e}")

    def _end(self, reason: str) -> None:
        now = time.time()
        cooldown = self._sec("self_think_cooldown_minutes", 120) * 60
        self.state.update({
            "phase": "idle",
            "cooldown_until": now + cooldown,
            "last_activity": now,
            "idle_target_sec": 0.0,
            "ticks_done": 0,
            "thread": [],
            "materials": [],
            "seed_id": 0,
            "last_tick_at": 0.0,
            "remembered": 0,
            "revised": 0,
            "lookups": [],
            "asked": 0,
            "waiting_answer": False,
            "resume_at": 0.0,
            "answers": [],
            "interruptions": [],
        })
        self._save()
        logger.info(f"[memory] 自我思考结束（{reason}），{int(cooldown // 60)} 分钟内不再触发")

    @staticmethod
    def _iso(ts: float) -> str:
        try:
            return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
        except Exception:
            return datetime.now().astimezone().isoformat(timespec="seconds")
