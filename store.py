"""记忆核心：存储层（M1）

设计依据见工作区《记忆插件设计-v0.1.md》。

要点：
- episode（原文）只追加，永不改写
- trace 是「痕迹的头」，看得到当前版本；trace_version 是版本链，只追加
- 目录（directory）只给轮廓，不给内容
- 深读（get_trace(history=True)）才展开版本链
- 遗忘用稳定度 S：R = exp(-Δt / S)，天花板随 importance 浮动（见 forget())
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1
KINDS = ("fact", "observation", "speculation", "self", "unresolved", "promise", "agreement")
RESIDENT_MAX_ITEMS = 12
RESIDENT_MAX_CHARS = 1200
RESIDENT_MAX_ONE = 500


def _series_scope_compatible(trace_scope: str, series_scope: str) -> bool:
    """系列场景和记忆场景是否相容。

    旧记忆的私聊 scope 只写 `private`，系列可能是 `private:<qq>`；
    这两个是同一个私聊，不能判成“场景对不上”。群聊仍要求同一个群号。
    """
    ts = (trace_scope or "").strip()
    ss = (series_scope or "").strip()
    if ss == "global":
        return True
    if ts == ss:
        return True
    if ts == "private" and ss.startswith("private:"):
        return True
    if ss == "private" and ts.startswith("private:"):
        return True
    if ss == "group" and ts.startswith("group:"):
        return True
    return False

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS episode (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scope        TEXT NOT NULL,
    session_id   TEXT,
    started_at   TEXT,
    ended_at     TEXT,
    participants TEXT,
    transcript   TEXT,
    source       TEXT DEFAULT 'live',
    source_ref   TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trace (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    head_version_id  INTEGER,
    episode_id       INTEGER,
    kind             TEXT NOT NULL,
    importance       REAL DEFAULT 0.5,
    stability        REAL DEFAULT 7.0,
    last_active_at   TEXT,
    last_mentioned_at TEXT,
    activation_count INTEGER DEFAULT 0,
    versions_count   INTEGER DEFAULT 0,
    avoided          INTEGER DEFAULT 0,
    avoid_reason     TEXT,
    status           TEXT DEFAULT 'active',
    tags             TEXT,
    decayed_at       TEXT,
    about_trace_id   INTEGER,
    merged_into      INTEGER,
    state            TEXT DEFAULT 'open',
    due_at           TEXT,
    due_time         TEXT,
    recur            TEXT DEFAULT '',
    review_at        TEXT,
    review_asked_at  TEXT,
    reminded_at      TEXT,
    split_from       INTEGER,
    source           TEXT DEFAULT 'live',
    scope            TEXT DEFAULT '',
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trace_version (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id       INTEGER NOT NULL,
    version_no     INTEGER NOT NULL,
    text           TEXT NOT NULL,
    certainty      REAL,
    anchor_quote   TEXT,
    anchor_time    TEXT,
    event_time     TEXT,
    topics         TEXT,
    felt           TEXT,
    reaction       TEXT,
    inference      TEXT,
    inference_certainty REAL,
    encoded_state  TEXT,
    reason         TEXT NOT NULL DEFAULT 'initial',
    caused_by_type TEXT,
    caused_by_id   TEXT,
    change_class   TEXT DEFAULT 'minor',
    folded         INTEGER DEFAULT 0,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trace_note (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id   INTEGER NOT NULL,
    version_id INTEGER,
    kind       TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notice (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id   INTEGER NOT NULL,
    kind       TEXT,
    day        TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notice ON notice(trace_id, day);

CREATE TABLE IF NOT EXISTS kv (
    k          TEXT PRIMARY KEY,
    v          TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS reminder (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id     INTEGER NOT NULL,
    session_id   TEXT,
    text         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    delivered_at TEXT,
    resolved     INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_reminder_pending ON reminder(delivered_at, resolved);

CREATE TABLE IF NOT EXISTS reflection (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    session_id  TEXT DEFAULT '',
    saw_ids     TEXT,
    decision    TEXT,
    reason      TEXT,
    said        TEXT,
    source_trace INTEGER,
    trigger     TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS core_memory (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    text       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resident_note (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    text       TEXT NOT NULL,
    scope      TEXT NOT NULL DEFAULT 'global',
    status     TEXT NOT NULL DEFAULT 'active',
    retired_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_resident_scope
    ON resident_note(status, scope, id);

CREATE TABLE IF NOT EXISTS memory_series (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    name       TEXT NOT NULL,
    scope      TEXT NOT NULL DEFAULT 'private',
    note       TEXT DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'active',
    retired_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_series_scope
    ON memory_series(status, scope, id);

CREATE TABLE IF NOT EXISTS series_member (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id INTEGER NOT NULL,
    trace_id  INTEGER NOT NULL,
    added_at  TEXT NOT NULL,
    status    TEXT NOT NULL DEFAULT 'active',
    UNIQUE(series_id, trace_id)
);
CREATE INDEX IF NOT EXISTS idx_series_member
    ON series_member(series_id, status);
CREATE INDEX IF NOT EXISTS idx_series_trace
    ON series_member(trace_id, status);

CREATE TABLE IF NOT EXISTS wander_trigger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    due_at     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    trace_id   INTEGER DEFAULT 0,
    session_id TEXT DEFAULT '',
    state      TEXT DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS self_snapshot (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    about      TEXT NOT NULL,
    content    TEXT NOT NULL,
    supersedes INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recall_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_ids  TEXT,
    query      TEXT,
    cue_type   TEXT,
    outcome    TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trace_episode ON trace(episode_id);
CREATE INDEX IF NOT EXISTS idx_version_trace ON trace_version(trace_id, version_no);
CREATE INDEX IF NOT EXISTS idx_episode_scope ON episode(scope, started_at);

CREATE VIRTUAL TABLE IF NOT EXISTS trace_fts USING fts5(
    text, trace_id UNINDEXED, tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS trace_vec (
    trace_id   INTEGER PRIMARY KEY,
    vec        BLOB NOT NULL,
    dim        INTEGER NOT NULL,
    model      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trace_chunk (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id   INTEGER NOT NULL,
    chunk_no   INTEGER NOT NULL,
    text       TEXT NOT NULL,
    vec        BLOB,
    created_at TEXT NOT NULL,
    UNIQUE(trace_id, chunk_no)
);

CREATE INDEX IF NOT EXISTS idx_chunk_trace ON trace_chunk(trace_id);

CREATE TABLE IF NOT EXISTS entity (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    norm       TEXT NOT NULL UNIQUE,
    type       TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mention (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id   INTEGER NOT NULL,
    entity_id  INTEGER NOT NULL,
    weight     REAL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    UNIQUE(trace_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_mention_entity ON mention(entity_id);
CREATE INDEX IF NOT EXISTS idx_mention_trace ON mention(trace_id);

CREATE TABLE IF NOT EXISTS raw_message (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    scope      TEXT,
    role       TEXT NOT NULL,
    text       TEXT NOT NULL,
    sender     TEXT,
    created_at TEXT NOT NULL,
    source     TEXT DEFAULT 'live'
);

CREATE TABLE IF NOT EXISTS correction (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    text       TEXT NOT NULL,
    applied    INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_time ON raw_message(created_at);
CREATE INDEX IF NOT EXISTS idx_raw_session ON raw_message(session_id, created_at);

CREATE TABLE IF NOT EXISTS pending_message (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    scope      TEXT,
    role       TEXT NOT NULL,
    text       TEXT NOT NULL,
    sender     TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    consumed   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pending ON pending_message(session_id, consumed);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def days_between(a: str | None, b: str | None) -> float:
    da, db = parse_iso(a), parse_iso(b)
    if not da or not db:
        return 0.0
    return abs((db - da).total_seconds()) / 86400.0


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate_columns()
        cur = self.conn.execute("SELECT version FROM schema_version LIMIT 1")
        row = cur.fetchone()
        if row is None:
            self.conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        self.conn.commit()

    def _migrate_columns(self):
        """给老库补新列（SQLite 没有 ADD COLUMN IF NOT EXISTS）。"""
        try:
            pcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(pending_message)")}
            if pcols and "sender" not in pcols:
                self.conn.execute("ALTER TABLE pending_message ADD COLUMN sender TEXT DEFAULT ''")
                self.conn.commit()
        except Exception:
            pass
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(trace)")}
        if "decayed_at" not in cols:
            self.conn.execute("ALTER TABLE trace ADD COLUMN decayed_at TEXT")
        if "about_trace_id" not in cols:
            self.conn.execute("ALTER TABLE trace ADD COLUMN about_trace_id INTEGER")
        if "merged_into" not in cols:
            self.conn.execute("ALTER TABLE trace ADD COLUMN merged_into INTEGER")
        if "state" not in cols:
            self.conn.execute("ALTER TABLE trace ADD COLUMN state TEXT DEFAULT 'open'")
        if "split_from" not in cols:
            self.conn.execute("ALTER TABLE trace ADD COLUMN split_from INTEGER")
        if "due_at" not in cols:
            self.conn.execute("ALTER TABLE trace ADD COLUMN due_at TEXT")
        if "reminded_at" not in cols:
            self.conn.execute("ALTER TABLE trace ADD COLUMN reminded_at TEXT")
        if "due_time" not in cols:
            self.conn.execute("ALTER TABLE trace ADD COLUMN due_time TEXT")
        if "recur" not in cols:
            self.conn.execute("ALTER TABLE trace ADD COLUMN recur TEXT DEFAULT ''")
        if "scope" not in cols:
            self.conn.execute("ALTER TABLE trace ADD COLUMN scope TEXT DEFAULT ''")
        # 老记录按所属 episode 补场合（group:群号 / private）
        self.conn.execute(
            "UPDATE trace SET scope = COALESCE("
            "(SELECT e.scope FROM episode e WHERE e.id=trace.episode_id), '') "
            "WHERE COALESCE(scope,'')=''")
        if "last_mentioned_at" not in cols:
            self.conn.execute("ALTER TABLE trace ADD COLUMN last_mentioned_at TEXT")
        if "review_at" not in cols:
            self.conn.execute("ALTER TABLE trace ADD COLUMN review_at TEXT")
        if "review_asked_at" not in cols:
            self.conn.execute("ALTER TABLE trace ADD COLUMN review_asked_at TEXT")
        # 老记录：用最后一版的时间当“最后一次被提起”
        self.conn.execute(
            "UPDATE trace SET last_mentioned_at = COALESCE("
            "(SELECT MAX(v.created_at) FROM trace_version v WHERE v.trace_id=trace.id), created_at) "
            "WHERE COALESCE(last_mentioned_at,'')=''")
        rcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(reflection)")}
        if "trigger" not in rcols:
            self.conn.execute("ALTER TABLE reflection ADD COLUMN trigger TEXT DEFAULT ''")
        vcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(trace_version)")}
        if "folded" not in vcols:
            self.conn.execute("ALTER TABLE trace_version ADD COLUMN folded INTEGER DEFAULT 0")
        if "event_time" not in vcols:
            self.conn.execute("ALTER TABLE trace_version ADD COLUMN event_time TEXT")
        if "topics" not in vcols:
            self.conn.execute("ALTER TABLE trace_version ADD COLUMN topics TEXT")
        if "felt" not in vcols:
            self.conn.execute("ALTER TABLE trace_version ADD COLUMN felt TEXT")
        if "reaction" not in vcols:
            self.conn.execute("ALTER TABLE trace_version ADD COLUMN reaction TEXT")
        if "inference" not in vcols:
            self.conn.execute("ALTER TABLE trace_version ADD COLUMN inference TEXT")
        if "inference_certainty" not in vcols:
            self.conn.execute("ALTER TABLE trace_version ADD COLUMN inference_certainty REAL")
        # 老记录补上衰减起点，否则第一次衰减会从 NULL 算
        self.conn.execute(
            "UPDATE trace SET decayed_at = COALESCE(last_active_at, created_at) WHERE decayed_at IS NULL")
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---------- 写入 ----------

    def add_episode(self, scope: str, *, session_id: str = "", started_at: str = "",
                    ended_at: str = "", participants: Any = None, transcript: str = "",
                    source: str = "live", source_ref: str = "") -> int:
        cur = self.conn.execute(
            """INSERT INTO episode(scope, session_id, started_at, ended_at,
                                   participants, transcript, source, source_ref, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (scope, session_id, started_at or now_iso(), ended_at, 
             json.dumps(participants or [], ensure_ascii=False) if participants is not None else None,
             transcript, source, source_ref, now_iso()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    @staticmethod
    def index_text(text: str, topics) -> str:
        """进 FTS 的文本：正文 + 主题词（正文本身不动）。"""
        t = text or ""
        try:
            if isinstance(topics, str):
                import json as _json
                topics = _json.loads(topics) if topics.strip().startswith("[") else [topics]
            words = [str(x).strip() for x in (topics or []) if str(x).strip()]
        except Exception:
            words = []
        if not words:
            return t
        return t + "。主题：" + "、".join(words)

    def add_trace(self, text: str, kind: str = "observation", *, importance: float = 0.5,
                  episode_id: int | None = None, anchor_quote: str = "",
                  anchor_time: str = "", encoded_state: Any = None,
                  event_time: str = "", due_at: str = "", due_time: str = "",
                  recur: str = "", topics: Any = None,
                  certainty: float | None = None, tags: Any = None,
                  felt: str = "", reaction: str = "", inference: str = "",
                  inference_certainty: float | None = None,
                  source: str = "live", scope: str = "", reason: str = "initial",
                  created_at: str | None = None, about_trace_id: int | None = None) -> int:
        if kind not in KINDS:
            raise ValueError(f"kind 必须是 {KINDS} 之一，收到 {kind!r}")
        if recur and due_time and not due_at:
            due_at = self._next_due(due_time)   # 循环约定：按时刻算下一次
        ts = created_at or now_iso()
        cur = self.conn.execute(
            """INSERT INTO trace(episode_id, kind, importance, stability, last_active_at,
                                 last_mentioned_at, activation_count, versions_count, tags,
                                 source, scope, created_at, decayed_at, about_trace_id)
               VALUES (?,?,?,?,?,?,0,1,?,?,?,?,?,?)""",
            (episode_id, kind, float(importance), 7.0, ts, ts,
             json.dumps(tags, ensure_ascii=False) if tags is not None else None, source,
             scope or "", ts, ts, about_trace_id),
        )
        trace_id = int(cur.lastrowid)
        ver = self.conn.execute(
            """INSERT INTO trace_version(trace_id, version_no, text, certainty, anchor_quote,
                                         anchor_time, event_time, topics, felt, reaction,
                                         inference, inference_certainty, encoded_state, reason,
                                         change_class, created_at)
               VALUES (?,1,?,?,?,?,?,?,?,?,?,?,?,?,'minor',?)""",
            (trace_id, text, certainty, anchor_quote or None, anchor_time or None,
             event_time or None,
             json.dumps(topics, ensure_ascii=False) if topics else None,
             felt or None, reaction or None, inference or None, inference_certainty,
             json.dumps(encoded_state, ensure_ascii=False) if encoded_state is not None else None,
             reason, ts),
        )
        self.conn.execute("UPDATE trace SET head_version_id=? WHERE id=?", (int(ver.lastrowid), trace_id))
        # 又提起这件事：从沉默/旧事里捞回来
        self.conn.execute(
            "UPDATE trace SET state='open', review_asked_at=NULL WHERE id=? AND COALESCE(state,'') IN ('stale','old','dormant')",
            (trace_id,),
        )
        # 又提起一次：重新算“三天没动静”
        self.conn.execute("UPDATE trace SET review_asked_at=NULL WHERE id=?", (trace_id,))
        self.conn.execute("INSERT INTO trace_fts(text, trace_id) VALUES (?,?)",
                          (self.index_text(text, topics), trace_id))
        if due_at:
            self.conn.execute("UPDATE trace SET due_at=? WHERE id=?", (due_at, trace_id))
        if due_time or recur:
            self.conn.execute("UPDATE trace SET due_time=?, recur=? WHERE id=?",
                              (due_time or None, recur or "", trace_id))
        self.conn.commit()
        return trace_id

    @staticmethod
    def _delta(text: str) -> str:
        """从「v1；因此X」里取出 X。"""
        t = (text or "").strip()
        if "；因此" in t:
            return t.split("；因此", 1)[1].strip()
        return t

    def add_resolution(self, fact_id: int, delta: str) -> int:
        """给事实追加/合并「因此…」了结版本。

        语义相同的多条 resolution 不再各占一版：旧的折叠（folded=1，数据保留），
        改成一条把所有这些「因此」合并起来的新版本。
        """
        rows = self.conn.execute(
            """SELECT id, text FROM trace_version WHERE trace_id=? AND reason='resolution'
               ORDER BY version_no""", (fact_id,)).fetchall()
        base = self.conn.execute(
            "SELECT text FROM trace_version WHERE trace_id=? ORDER BY version_no LIMIT 1",
            (fact_id,)).fetchone()
        base_text = (base["text"] if base else "").rstrip("。")
        deltas = []
        for r in rows:
            d = self._delta(r["text"])
            if d and d not in deltas:
                deltas.append(d)
        d = (delta or "").strip()
        if d and d not in deltas:
            deltas.append(d)
        for r in rows:
            self.conn.execute("UPDATE trace_version SET folded=1 WHERE id=?", (r["id"],))
        self.conn.commit()
        text = base_text + ("；因此" + "、".join(deltas) if deltas else "")
        return self.add_version(fact_id, text, reason="resolution",
                                caused_by_type="correction", change_class="minor")

    def add_version(self, trace_id: int, text: str, *, reason: str = "reconsolidation",
                    caused_by_type: str = "", caused_by_id: str = "",
                    change_class: str = "minor", certainty: float | None = None,
                    anchor_quote: str = "", anchor_time: str = "",
                    event_time: str = "", due_at: str = "", due_time: str = "",
                    recur: str = "", topics: Any = None,
                    felt: str = "", reaction: str = "", inference: str = "",
                    inference_certainty: float | None = None,
                    encoded_state: Any = None) -> int:
        """追加新版本并把 head 指过去。旧版本永不删除。"""
        if caused_by_type not in ("recall", "episode", "correction", "integration", "self_observation", ""):
            raise ValueError(f"不认识的 caused_by_type: {caused_by_type!r}")
        row = self.conn.execute("SELECT versions_count, head_version_id FROM trace WHERE id=?", (trace_id,)).fetchone()
        if row is None:
            raise KeyError(f"trace {trace_id} 不存在")
        version_no = int(row["versions_count"]) + 1
        ts = now_iso()
        cur = self.conn.execute(
            """INSERT INTO trace_version(trace_id, version_no, text, certainty, anchor_quote,
                                         anchor_time, event_time, topics, felt, reaction,
                                         inference, inference_certainty, encoded_state, reason,
                                         caused_by_type, caused_by_id, change_class, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trace_id, version_no, text, certainty, anchor_quote or None, anchor_time or None,
             event_time or None,
             json.dumps(topics, ensure_ascii=False) if topics else None,
             felt or None, reaction or None, inference or None, inference_certainty,
             json.dumps(encoded_state, ensure_ascii=False) if encoded_state is not None else None,
             reason, caused_by_type or None, caused_by_id or None, change_class, ts),
        )
        new_id = int(cur.lastrowid)
        self.conn.execute("UPDATE trace SET head_version_id=?, versions_count=? WHERE id=?",
                          (new_id, version_no, trace_id))
        self.conn.execute("UPDATE trace SET last_mentioned_at=? WHERE id=?", (ts, trace_id))
        # 又提起这件事：从沉默/旧事里捞回来
        self.conn.execute(
            "UPDATE trace SET state='open', review_asked_at=NULL WHERE id=? AND COALESCE(state,'') IN ('stale','old','dormant')",
            (trace_id,),
        )
        # 又提起一次：重新算“三天没动静”
        self.conn.execute("UPDATE trace SET review_asked_at=NULL WHERE id=?", (trace_id,))
        self.conn.execute("DELETE FROM trace_fts WHERE trace_id=?", (trace_id,))
        # 主题词默认继承上一版（改口不该丢掉「关于什么」）
        if topics is None:
            prev = self.conn.execute(
                "SELECT topics FROM trace_version WHERE trace_id=? AND id<? "
                "ORDER BY version_no DESC LIMIT 1", (trace_id, new_id)).fetchone()
            if prev and prev["topics"]:
                topics = prev["topics"]
                self.conn.execute("UPDATE trace_version SET topics=? WHERE id=?",
                                  (topics, new_id))
        # 旧的关键词索引要清掉，否则同一痕迹会在 FTS 里留下历史文本
        self.conn.execute("DELETE FROM trace_fts WHERE trace_id=?", (trace_id,))
        self.conn.execute("INSERT INTO trace_fts(text, trace_id) VALUES (?,?)",
                          (self.index_text(text, topics), trace_id))
        # 正文变了，旧向量作废，等 ensure_vectors 重算
        self.conn.execute("DELETE FROM trace_vec WHERE trace_id=?", (trace_id,))
        if due_at or due_time or recur:
            self.conn.execute(
                "UPDATE trace SET due_at=COALESCE(NULLIF(?,''),due_at), "
                "due_time=COALESCE(NULLIF(?,''),due_time), recur=COALESCE(NULLIF(?,''),recur) "
                "WHERE id=?", (due_at, due_time, recur, trace_id))
        self.conn.commit()
        return new_id

    def add_note(self, trace_id: int, text: str, kind: str = "annotation",
                 version_id: int | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO trace_note(trace_id, version_id, kind, text, created_at) VALUES (?,?,?,?,?)",
            (trace_id, version_id, kind, text, now_iso()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def archive(self, trace_id: int, merged_into: int | None = None) -> None:
        """退出日常检索；正文与版本链全部保留。"""
        self.conn.execute(
            "UPDATE trace SET status='archived', merged_into=? WHERE id=?", (merged_into, trace_id))
        self.conn.commit()

    def settle(self, trace_id: int, state: str = "settled", note: str = "") -> None:
        """给约定/未了的事收尾：settled（办完了/说清了）或 void（作废了）。"""
        if state not in ("open", "settled", "void"):
            raise ValueError("state 只能是 open/settled/void")
        self.conn.execute("UPDATE trace SET state=? WHERE id=?", (state, trace_id))
        self.conn.execute(
            "UPDATE reminder SET resolved=1 WHERE trace_id=? AND delivered_at IS NULL",
            (int(trace_id),))
        if note:
            self.add_note(trace_id, note, kind="annotation")
        self.conn.commit()

    def mark_avoided(self, trace_id: int, reason: str = ""):
        self.conn.execute("UPDATE trace SET avoided=1, avoid_reason=?, status='avoided' WHERE id=?",
                          (reason, trace_id))
        self.conn.commit()

    def activate(self, trace_id: int, cue_type: str = "recall", query: str = "") -> dict:
        """激活一次：重置时钟、按 R 给稳定度增益、记一条 recall_log。"""
        row = self.trace_row(trace_id)
        if row is None:
            raise KeyError(trace_id)
        ts = now_iso()
        r_before = self.retrievability(row, at=ts)
        s = float(row["stability"] or 7.0)
        gain = 3.0 * (1.0 - r_before)
        s = min(self.ceiling(row), s + gain)
        self.conn.execute(
            "UPDATE trace SET stability=?, last_active_at=?, activation_count=activation_count+1 WHERE id=?",
            (s, ts, trace_id),
        )
        self.conn.execute(
            "INSERT INTO recall_log(trace_ids, query, cue_type, outcome, created_at) VALUES (?,?,?,?,?)",
            (json.dumps([trace_id]), query, cue_type, "hit", ts),
        )
        self.conn.commit()
        self._maybe_meta(trace_id)
        return {"trace_id": trace_id, "r_before": round(r_before, 3), "s_after": round(s, 2)}

    META_STEPS = (3, 5, 7, 10, 15, 20, 30, 50)

    def _maybe_meta(self, trace_id: int) -> int | None:
        """「我想过很多次」本身记成一条 self 痕迹；只记一层，不递归。"""
        row = self.conn.execute(
            "SELECT * FROM trace WHERE id=? AND about_trace_id IS NULL", (trace_id,)
        ).fetchone()
        if row is None:
            return None
        n = int(row["activation_count"] or 0)
        if n not in self.META_STEPS:
            return None
        head = self.conn.execute("SELECT text, anchor_time FROM trace_version WHERE id=?",
                                 (row["head_version_id"],)).fetchone()
        if not head:
            return None
        span = round(days_between(row["created_at"], now_iso()))
        text = f"关于「{head['text'][:24]}」，我想过 {n} 次，跨了 {span} 天。"
        existing = self.conn.execute(
            "SELECT id FROM trace WHERE about_trace_id=? AND kind='self' LIMIT 1", (trace_id,)
        ).fetchone()
        if existing:
            tid = int(existing["id"])
            self.add_version(tid, text, reason="self_observation", caused_by_type="self_observation")
            return tid
        return self.add_trace(
            text, "self", importance=min(0.8, 0.3 + 0.01 * n), anchor_time=now_iso(),
            tags=["回想"], source="meta", reason="self_observation",
            about_trace_id=trace_id,
        )

    # ---------- 遗忘模型 ----------

    @staticmethod
    def ceiling(row: sqlite3.Row) -> float:
        importance = float(row["importance"] or 0.5)
        base = 30.0 + 150.0 * max(0.0, min(1.0, importance))
        # 每 3 次激活，天花板 ×1.05（最多翻到 365 天）
        bumps = int((row["activation_count"] or 0) // 3)
        grown = base * (1.05 ** min(bumps, 20))
        return min(365.0, grown)

    @staticmethod
    def retrievability(row: sqlite3.Row, at: str | None = None) -> float:
        s = float(row["stability"] or 7.0)
        if s <= 0:
            return 0.0
        dt = days_between(row["last_active_at"] or row["created_at"], at or now_iso())
        return math.exp(-dt / s)

    def apply_idle_decay(self, at: str | None = None, factor: float = 0.85) -> int:
        """闲置萎缩：自上次衰减以来每错过一个 S 周期，S × factor。

        用 decayed_at 记账，所以一天跑几次也不会重复扣。
        """
        at = at or now_iso()
        changed = 0
        for row in self.conn.execute(
                "SELECT id, stability, last_active_at, created_at, decayed_at FROM trace"):
            s = float(row["stability"] or 7.0)
            anchor = row["decayed_at"] or row["last_active_at"] or row["created_at"]
            if not anchor:
                continue
            # 只从「上次衰减」或「上次被想起」里更晚的那个算起
            latest = max(anchor, row["last_active_at"] or anchor)
            dt = days_between(latest, at)
            missed = int(dt // max(s, 1e-6))
            if missed > 0:
                new_s = max(1.0, s * (factor ** missed))
                self.conn.execute("UPDATE trace SET stability=?, decayed_at=? WHERE id=?",
                                  (new_s, at, row["id"]))
                changed += 1
        self.conn.commit()
        return changed

    # ---------- 循环约定 ----------

    @staticmethod
    def _next_due(due_time: str, after: str = "") -> str:
        """按 HH:MM 算下一次（今天还没到就今天，否则明天）。"""
        from datetime import datetime, timedelta
        try:
            hh, mm = [int(x) for x in str(due_time).split(":", 1)]
        except Exception:
            return ""
        now = datetime.now().astimezone()
        base = now
        if after:
            try:
                base = datetime.fromisoformat(after)
                if base.tzinfo is None:
                    base = base.replace(tzinfo=now.tzinfo)
            except Exception:
                base = now
        cand = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        while cand <= now:
            cand = cand + timedelta(days=1)
        return cand.isoformat(timespec="seconds")

    def advance_recurring(self, trace_id: int) -> str:
        """循环约定：把 due_at 推到下一次，并清掉已提醒标记。"""
        row = self.conn.execute(
            "SELECT due_time, recur, due_at FROM trace WHERE id=?", (int(trace_id),)).fetchone()
        if not row or not row["recur"]:
            return ""
        nxt = self._next_due(row["due_time"] or "", row["due_at"] or "")
        if nxt:
            self.conn.execute("UPDATE trace SET due_at=?, reminded_at=NULL WHERE id=?",
                              (nxt, int(trace_id)))
            self.conn.commit()
        return nxt

    def advance_past_recurring(self, limit: int = 20) -> list[int]:
        """兜底：循环约定已经提醒过、但 due_at 还停在过去的，推到下一次。"""
        rows = self.conn.execute(
            """SELECT id FROM trace
               WHERE COALESCE(recur,'')!='' AND status='active' AND avoided=0
                 AND COALESCE(state,'open')='open'
                 AND due_at IS NOT NULL AND due_at!=''
                 AND reminded_at IS NOT NULL
                 AND julianday(due_at) <= julianday('now')
               LIMIT ?""", (int(limit),)).fetchall()
        out = []
        for r in rows:
            if self.advance_recurring(int(r["id"])):
                out.append(int(r["id"]))
        return out

    def cancel_agreement(self, trace_id: int, note: str = "") -> bool:
        """她把一个约定取消（循环的也一起停）。"""
        row = self.trace_row(int(trace_id))
        if not row:
            return False
        self.conn.execute(
            "UPDATE trace SET state='void', recur='', reminded_at=NULL WHERE id=?",
            (int(trace_id),))
        self.conn.execute(
            "UPDATE reminder SET resolved=1 WHERE trace_id=? AND delivered_at IS NULL",
            (int(trace_id),))
        self.conn.commit()
        if note:
            self.add_note(int(trace_id), note, kind="annotation")
        return True

    # ---------- 通知限速 ----------

    def notice_count(self, trace_id: int, kind: str = "", day: str = "") -> int:
        import time as _t
        d = day or _t.strftime("%Y-%m-%d")
        if kind:
            return int(self.conn.execute(
                "SELECT COUNT(*) FROM notice WHERE trace_id=? AND kind=? AND day=?",
                (int(trace_id), kind, d)).fetchone()[0])
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM notice WHERE trace_id=? AND day=?",
            (int(trace_id), d)).fetchone()[0])

    def notice_add(self, trace_id: int, kind: str = "") -> None:
        import time as _t
        self.conn.execute(
            "INSERT INTO notice(trace_id, kind, day, created_at) VALUES (?,?,?,?)",
            (int(trace_id), kind[:16], _t.strftime("%Y-%m-%d"), now_iso()))
        self.conn.commit()

    def pending_reminder_count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM reminder WHERE delivered_at IS NULL AND resolved=0").fetchone()[0])

    def notice_purge(self, days: int = 180) -> int:
        """清掉很旧的通知计数。"""
        cur = self.conn.execute(
            "DELETE FROM notice WHERE julianday('now') - julianday(created_at) > ?",
            (float(days),))
        self.conn.commit()
        return int(cur.rowcount or 0)

    # ---------- 沉默档 ----------

    def stale_candidates(self, days: int = 7, limit: int = 50) -> list[int]:
        """挂太久没动的事：open/missed 且最后时间已超过 N 天。"""
        rows = self.conn.execute(
            """SELECT t.id FROM trace t
               WHERE t.kind IN ('promise','unresolved','agreement')
                 AND t.status='active' AND t.avoided=0
                 AND COALESCE(t.state,'open') IN ('open','missed')
                 AND COALESCE(t.recur,'')=''
                 AND (COALESCE(t.due_at,'')='' OR julianday(t.due_at) <= julianday('now'))
                 AND NOT (t.kind='promise' AND t.reminded_at IS NULL AND COALESCE(t.due_at,'')!=''
                          AND julianday(t.due_at) <= julianday('now'))
                 AND julianday(COALESCE(NULLIF(t.last_mentioned_at,''), NULLIF(t.due_at,''), t.created_at)) <= julianday('now') - ?
               LIMIT ?""", (float(days), int(limit))).fetchall()
        return [int(r["id"]) for r in rows]

    def old_candidates(self, days: int = 30, limit: int = 50) -> list[int]:
        """沉默很久的：已经 stale，且最后触碰超过 N 天。"""
        rows = self.conn.execute(
            """SELECT t.id FROM trace t
               WHERE t.kind IN ('promise','unresolved','agreement')
                 AND t.status='active' AND t.avoided=0
                 AND COALESCE(t.state,'open')='stale'
                 AND COALESCE(t.recur,'')=''
                 AND (COALESCE(t.due_at,'')='' OR julianday(t.due_at) <= julianday('now'))
                 AND julianday(COALESCE(NULLIF(t.last_mentioned_at,''), t.created_at)) <= julianday('now') - ?
               LIMIT ?""", (float(days), int(limit))).fetchall()
        return [int(r["id"]) for r in rows]

    def mark_stale(self, ids: list[int], state: str = "stale") -> int:
        if not ids:
            return 0
        q = ",".join("?" * len(ids))
        vals = [int(i) for i in ids]
        cur = self.conn.execute(
            f"UPDATE trace SET state=? WHERE id IN ({q})", [state] + vals)
        # 转沉默后，别再递它还没投出去的通知
        self.conn.execute(
            f"UPDATE reminder SET resolved=1 WHERE delivered_at IS NULL AND trace_id IN ({q})", vals)
        self.conn.commit()
        for tid in vals[:2]:
            try:
                self.enqueue_trigger("stale", int(tid), 60)
            except Exception:
                pass
        return int(cur.rowcount or 0)

    # ---------- 原话 / 时效 ----------

    def episode_transcript(self, trace_id: int) -> str:
        r = self.conn.execute(
            "SELECT e.transcript FROM trace t JOIN episode e ON e.id=t.episode_id WHERE t.id=?",
            (int(trace_id),)).fetchone()
        return (r["transcript"] if r and r["transcript"] else "") or ""

    def review_candidates(self, after_days: float = 3, asked_days: float = 7,
                          limit: int = 3, session: str = "") -> list[dict]:
        from datetime import timedelta
        now = datetime.now().astimezone()
        cutoff = (now - timedelta(days=float(after_days))).isoformat(timespec="seconds")
        asked_cut = (now - timedelta(days=float(asked_days))).isoformat(timespec="seconds")
        rows = self.conn.execute(
            """SELECT t.id, t.kind, t.review_at, t.review_asked_at, v.text
               FROM trace t JOIN trace_version v ON v.id=t.head_version_id
               LEFT JOIN episode e ON e.id=t.episode_id
               WHERE t.kind IN ('unresolved','promise') AND t.status='active' AND t.avoided=0
                 AND (?='' OR COALESCE(e.session_id,'')=?)
                 AND COALESCE(t.state,'open') IN ('open','missed')
                 AND COALESCE(t.due_at,'')=''
                 AND COALESCE(t.last_mentioned_at, t.created_at) <= ?
                 AND (t.review_asked_at IS NULL OR t.review_asked_at <= ?)
               ORDER BY COALESCE(t.last_mentioned_at, t.created_at) LIMIT ?""",
            (session or "", session or "", cutoff, asked_cut, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def mark_review_asked(self, trace_id: int) -> None:
        self.conn.execute(
            "UPDATE trace SET review_asked_at=? WHERE id=?", (now_iso(), int(trace_id)))
        self.conn.commit()

    def dormant_candidates(self, days: float = 7, limit: int = 50) -> list[int]:
        from datetime import timedelta
        cutoff = (datetime.now().astimezone() - timedelta(days=float(days))).isoformat(
            timespec="seconds")
        rows = self.conn.execute(
            """SELECT id FROM trace
               WHERE kind IN ('unresolved','promise') AND status='active' AND avoided=0
                 AND COALESCE(state,'open') IN ('open','missed')
                 AND review_asked_at IS NOT NULL AND review_asked_at <= ?
               LIMIT ?""", (cutoff, int(limit))).fetchall()
        return [int(r["id"]) for r in rows]

    def mark_dormant(self, ids: list[int]) -> int:
        if not ids:
            return 0
        q = ",".join("?" * len(ids))
        cur = self.conn.execute(
            f"UPDATE trace SET state='dormant' WHERE id IN ({q})", [int(i) for i in ids])
        self.conn.commit()
        return int(cur.rowcount or 0)

    # ---------- 走神 / 反思 ----------

    def add_reflection(self, session_id: str, saw_ids, decision: str,
                       reason: str = "", said: str = "", source_trace=None,
                       trigger: str = "") -> int:
        cur = self.conn.execute(
            """INSERT INTO reflection(created_at, session_id, saw_ids, decision, reason, said,
                                      source_trace, trigger)
               VALUES (?,?,?,?,?,?,?,?)""",
            (now_iso(), session_id or "",
             json.dumps(saw_ids or [], ensure_ascii=False),
             decision, (reason or "")[:500], (said or "")[:4000],
             int(source_trace) if source_trace else None, trigger or ""))
        self.conn.commit()
        return int(cur.lastrowid)

    def reflections_recent(self, limit: int = 5, session_id: str = "") -> list[dict]:
        if session_id:
            rows = self.conn.execute(
                "SELECT * FROM reflection WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, int(limit))).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM reflection ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def thoughts_today(self) -> int:
        r = self.conn.execute(
            "SELECT COUNT(*) FROM reflection WHERE date(created_at)=date('now','localtime')").fetchone()
        return int(r[0] if r else 0)

    def reflections_today_for(self, session: str, decision: str = "speak") -> int:
        r = self.conn.execute(
            """SELECT COUNT(*) FROM reflection
               WHERE decision=? AND session_id=? AND date(created_at)=date('now','localtime')""",
            (decision, str(session or ""))).fetchone()
        return int(r[0] if r else 0)

    def reflections_today(self, decision: str = "speak") -> int:
        r = self.conn.execute(
            """SELECT COUNT(*) FROM reflection
               WHERE decision=? AND date(created_at)=date('now','localtime')""",
            (decision,)).fetchone()
        return int(r[0] if r else 0)

    def add_core(self, text: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO core_memory(created_at, text) VALUES (?,?)",
            (now_iso(), (text or "").strip()[:2000]))
        self.conn.commit()
        return int(cur.lastrowid)

    def core_entries(self, limit: int = 5) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM core_memory ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    # ---------- 她自己放的常驻区 ----------

    def resident_add(self, text: str, scope: str = "global") -> int:
        """她自己往常驻区放一条。满了不自动清，直接报错让她先撤。"""
        text = (text or "").strip()
        if not text:
            raise ValueError("内容是空的")
        if len(text) > RESIDENT_MAX_ONE:
            raise ValueError(f"一条最多 {RESIDENT_MAX_ONE} 字，这条 {len(text)} 字")
        scope = (scope or "global").strip() or "global"
        rows = self.conn.execute(
            "SELECT text FROM resident_note WHERE status='active'").fetchall()
        if len(rows) >= RESIDENT_MAX_ITEMS:
            raise ValueError(
                f"常驻区满了（{RESIDENT_MAX_ITEMS} 条），先 resident_retire 一条再放")
        total = sum(len(r["text"] or "") for r in rows)
        if total + len(text) > RESIDENT_MAX_CHARS:
            raise ValueError(
                f"常驻区字数满了（{RESIDENT_MAX_CHARS} 字），先撤一条再放")
        cur = self.conn.execute(
            "INSERT INTO resident_note(created_at, text, scope, status) VALUES (?,?,?, 'active')",
            (now_iso(), text, scope))
        self.conn.commit()
        return int(cur.lastrowid)

    def resident_active(self, scopes: list[str] | None = None) -> list[dict]:
        if scopes:
            marks = ",".join("?" for _ in scopes)
            rows = self.conn.execute(
                f"SELECT * FROM resident_note WHERE status='active' AND scope IN ({marks}) "
                "ORDER BY id", tuple(scopes)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM resident_note WHERE status='active' ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def resident_retire(self, note_id: int) -> bool:
        cur = self.conn.execute(
            "UPDATE resident_note SET status='retired', retired_at=? "
            "WHERE id=? AND status='active'", (now_iso(), int(note_id)))
        self.conn.commit()
        return cur.rowcount > 0

    # ---------- 她自己分的系列 ----------

    def series_create(self, name: str, scope: str, note: str = "") -> int:
        name = (name or "").strip()
        if not name:
            raise ValueError("系列名是空的")
        if len(name) > 60:
            raise ValueError("系列名最多 60 字")
        scope = (scope or "private").strip() or "private"
        cur = self.conn.execute(
            "INSERT INTO memory_series(created_at, name, scope, note, status) "
            "VALUES (?,?,?,?, 'active')",
            (now_iso(), name, scope, (note or "").strip()[:200]))
        self.conn.commit()
        return int(cur.lastrowid)

    def series_set_scope(self, series_id: int, scope: str) -> bool:
        cur = self.conn.execute(
            "UPDATE memory_series SET scope=? WHERE id=? AND status='active'",
            ((scope or "").strip() or "private", int(series_id)))
        self.conn.commit()
        return cur.rowcount > 0

    def series_add(self, series_id: int, trace_id: int) -> bool:
        s = self.conn.execute(
            "SELECT * FROM memory_series WHERE id=? AND status='active'",
            (int(series_id),)).fetchone()
        if not s:
            raise ValueError(f"系列 #{int(series_id)} 不存在或已撤")
        t = self.trace_row(int(trace_id))
        if not t:
            raise ValueError(f"记忆 #{int(trace_id)} 不存在")
        if t["status"] != "active":
            raise ValueError(f"记忆 #{int(trace_id)} 不是 active")
        if not _series_scope_compatible(t["scope"] or "", s["scope"] or ""):
            raise ValueError(
                f"记忆 #{int(trace_id)} 在 {t['scope'] or '未知场景'}，"
                f"系列是 {s['scope']}，不能跨场景放")
        ex = self.conn.execute(
            "SELECT id FROM series_member WHERE series_id=? AND trace_id=? AND status='active'",
            (int(series_id), int(trace_id))).fetchone()
        if ex:
            return False
        self.conn.execute(
            "INSERT INTO series_member(series_id, trace_id, added_at, status) "
            "VALUES (?,?,?, 'active') "
            "ON CONFLICT(series_id, trace_id) DO UPDATE SET status='active', added_at=excluded.added_at",
            (int(series_id), int(trace_id), now_iso()))
        self.conn.commit()
        return True

    def series_remove(self, series_id: int, trace_id: int) -> bool:
        cur = self.conn.execute(
            "UPDATE series_member SET status='removed' "
            "WHERE series_id=? AND trace_id=? AND status='active'",
            (int(series_id), int(trace_id)))
        self.conn.commit()
        return cur.rowcount > 0

    def series_retire(self, series_id: int) -> bool:
        cur = self.conn.execute(
            "UPDATE memory_series SET status='retired', retired_at=? "
            "WHERE id=? AND status='active'", (now_iso(), int(series_id)))
        self.conn.commit()
        return cur.rowcount > 0

    def series_list(self, scopes: list[str] | None = None) -> list[dict]:
        if scopes:
            marks = ",".join("?" for _ in scopes)
            rows = self.conn.execute(
                f"""SELECT s.*, COUNT(m.id) AS n, MAX(m.added_at) AS last_added
                    FROM memory_series s
                    LEFT JOIN series_member m ON m.series_id=s.id AND m.status='active'
                    WHERE s.status='active' AND s.scope IN ({marks})
                    GROUP BY s.id ORDER BY s.id DESC""", tuple(scopes)).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT s.*, COUNT(m.id) AS n, MAX(m.added_at) AS last_added
                   FROM memory_series s
                   LEFT JOIN series_member m ON m.series_id=s.id AND m.status='active'
                   WHERE s.status='active'
                   GROUP BY s.id ORDER BY s.id DESC""").fetchall()
        return [dict(r) for r in rows]

    def series_show(self, series_id: int) -> dict | None:
        s = self.conn.execute(
            "SELECT * FROM memory_series WHERE id=?", (int(series_id),)).fetchone()
        if not s:
            return None
        rows = self.conn.execute(
            """SELECT t.id, t.kind, t.created_at, t.scope,
                      v.text, v.anchor_time, v.event_time
               FROM series_member m
               JOIN trace t ON t.id=m.trace_id
               JOIN trace_version v ON v.id=t.head_version_id
               WHERE m.series_id=? AND m.status='active'
               ORDER BY COALESCE(v.anchor_time, t.created_at, v.event_time)""",
            (int(series_id),)).fetchall()
        out = dict(s)
        out["members"] = [dict(r) for r in rows]
        return out

    def series_for_traces(self, trace_ids: list[int]) -> dict[int, dict]:
        ids = sorted({int(x) for x in trace_ids if x})
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"""SELECT m.trace_id, s.id, s.name, s.scope
                FROM series_member m
                JOIN memory_series s ON s.id=m.series_id
                WHERE m.trace_id IN ({marks}) AND m.status='active' AND s.status='active'
                ORDER BY s.id""", ids).fetchall()
        out: dict[int, dict] = {}
        for r in rows:
            tid = int(r["trace_id"])
            if tid not in out:  # 检索折叠时先按最早建的系列归组
                out[tid] = {"id": int(r["id"]), "name": r["name"] or "",
                            "scope": r["scope"] or ""}
        return out

    def last_user_message_at(self, session_id: str) -> str:
        r = self.conn.execute(
            """SELECT MAX(created_at) FROM raw_message
               WHERE session_id=? AND role='user'""", (session_id,)).fetchone()
        return (r[0] if r and r[0] else "") or ""

    def enqueue_trigger(self, kind: str, trace_id: int = 0,
                          due_minutes: float = 5, session: str = "") -> int:
        from datetime import timedelta
        tid = int(trace_id or 0)
        r = self.conn.execute(
            "SELECT id FROM wander_trigger WHERE kind=? AND trace_id=? AND state='pending' LIMIT 1",
            (kind, tid)).fetchone()
        if r:
            return int(r["id"])
        due = (datetime.now().astimezone() + timedelta(minutes=float(due_minutes))).isoformat(
            timespec="seconds")
        cur = self.conn.execute(
            "INSERT INTO wander_trigger(created_at, due_at, kind, trace_id, session_id, state) "
            "VALUES (?,?,?,?,?,'pending')",
            (now_iso(), due, kind, tid, session or ""))
        self.conn.commit()
        return int(cur.lastrowid)

    def due_triggers(self, limit: int = 2) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM wander_trigger WHERE state='pending' AND due_at<=? "
            "ORDER BY due_at LIMIT ?", (now_iso(), int(limit))).fetchall()
        return [dict(r) for r in rows]

    def finish_trigger(self, trigger_id: int, state: str = "done") -> None:
        self.conn.execute("UPDATE wander_trigger SET state=? WHERE id=?",
                          (state, int(trigger_id)))
        self.conn.commit()

    def recent_reflected_traces(self, days: float = 7) -> set[int]:
        from datetime import timedelta
        cutoff = (datetime.now().astimezone() - timedelta(days=float(days))).isoformat(
            timespec="seconds")
        out: set[int] = set()
        for r in self.conn.execute(
                "SELECT source_trace, saw_ids FROM reflection WHERE created_at>=? "
                "AND decision IN ('speak','silent')", (cutoff,)):
            if r["source_trace"]:
                out.add(int(r["source_trace"]))
            try:
                for x in json.loads(r["saw_ids"] or "[]"):
                    out.add(int(x))
            except Exception:
                pass
        return out

    def random_memory_for_wander(self, exclude: set | None = None, scope: str = "") -> dict | None:
        import random as _random
        rows = self.conn.execute(
            """SELECT t.id, t.kind, t.scope, COALESCE(t.state,'open') AS st, v.text
               FROM trace t JOIN trace_version v ON v.id=t.head_version_id
               WHERE t.status='active' AND t.avoided=0
                 AND COALESCE(t.state,'open') NOT IN ('settled','void')
                 AND t.kind IN ('fact','observation','self','unresolved','promise','agreement')
               ORDER BY t.id DESC LIMIT 400""").fetchall()
        if not rows:
            return None
        weights = {"self": 3, "unresolved": 3, "promise": 2, "agreement": 2,
                   "observation": 2, "fact": 1}
        skip = {int(x) for x in (exclude or set())}
        rows = [r for r in rows if int(r["id"]) not in skip]
        if scope:
            rows = [r for r in rows if (r["scope"] or "") == scope]
        if not rows:
            return None
        picked = _random.choices(list(rows), weights=[weights.get(r["kind"], 1) for r in rows], k=1)[0]
        return dict(picked)

    def trace_brief(self, trace_id: int) -> dict | None:
        r = self.conn.execute(
            """SELECT t.id, t.kind, t.scope, COALESCE(t.state,'open') AS st, v.text
               FROM trace t JOIN trace_version v ON v.id=t.head_version_id
               WHERE t.id=?""", (int(trace_id),)).fetchone()
        return dict(r) if r else None

    # ---------- 每日任务状态 ----------

    def kv_get(self, key: str) -> str:
        r = self.conn.execute("SELECT v FROM kv WHERE k=?", (str(key),)).fetchone()
        return (r["v"] if r else "") or ""

    def kv_set(self, key: str, value: str) -> None:
        self.conn.execute(
            """INSERT INTO kv(k, v, updated_at) VALUES (?,?,?)
               ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at""",
            (str(key), str(value), now_iso()))
        self.conn.commit()

    # ---------- 按天回想 ----------

    def day_chain(self, day: str, max_episodes: int = 12, per_episode: int = 3,
                  exclude_recent_min: int = 30) -> dict:
        """某一天的经历链：每段经历的时间范围 + 几条代表痕迹。

        最近 exclude_recent_min 分钟内的经历会被跳过——那段还在上下文里，重复讲没意义。
        """
        rows = self.conn.execute(
            """SELECT id, started_at, ended_at FROM episode
               WHERE started_at LIKE ? ORDER BY started_at LIMIT ?""",
            (day + "%", int(max_episodes) + 6)).fetchall()
        if exclude_recent_min:
            from datetime import datetime, timedelta
            cutoff = datetime.now().astimezone() - timedelta(minutes=int(exclude_recent_min))
            kept = []
            for e in rows:
                try:
                    end = datetime.fromisoformat(str(e["ended_at"] or e["started_at"]))
                    if end.tzinfo is None:
                        end = end.replace(tzinfo=cutoff.tzinfo)
                    if end >= cutoff:
                        continue
                except Exception:
                    pass
                kept.append(e)
            rows = kept[:int(max_episodes)]
        episodes = []
        for e in rows:
            items = self.conn.execute(
                """SELECT t.kind, v.text FROM trace t
                   JOIN trace_version v ON v.id=t.head_version_id
                   WHERE t.episode_id=? AND t.status='active' AND t.avoided=0
                   ORDER BY t.id""", (int(e["id"]),)).fetchall()
            pref = {"observation": 0, "fact": 1, "promise": 2, "agreement": 3,
                    "unresolved": 4, "self": 5, "speculation": 6}
            picked = sorted(items, key=lambda r: pref.get(r["kind"], 9))[:per_episode]
            episodes.append({
                "start": str(e["started_at"] or "")[11:16],
                "end": str(e["ended_at"] or "")[11:16],
                "total": len(items),
                "items": [{"kind": r["kind"], "text": r["text"]} for r in picked],
            })
        open_items = [dict(r) for r in self.conn.execute(
            """SELECT t.id, t.kind, v.text, t.due_at FROM trace t
               JOIN trace_version v ON v.id=t.head_version_id
               WHERE t.kind IN ('promise','unresolved','agreement') AND t.status='active'
                 AND COALESCE(t.state,'open') IN ('open','missed')
                 AND (t.created_at LIKE ? OR t.due_at LIKE ?)
               ORDER BY t.id LIMIT 8""", (day + "%", day + "%"))]
        return {"episodes": episodes, "open_items": open_items}

    # ---------- 约定到期 ----------

    def due_promises(self, window_min: int = 5, catch_up: bool = False) -> list[dict]:
        """到点且还没提醒过的约定。catch_up=True 时不管时间窗，补停机期间错过的。"""
        sql = """SELECT t.id, t.due_at, t.recur, v.text, e.session_id
                 FROM trace t
                 JOIN trace_version v ON v.id=t.head_version_id
                 LEFT JOIN episode e ON e.id=t.episode_id
                 WHERE t.kind='promise' AND t.status='active' AND t.avoided=0
                   AND COALESCE(t.state,'open')='open'
                   AND t.due_at IS NOT NULL AND t.due_at!=''
                   AND t.reminded_at IS NULL
                   AND julianday(t.due_at) <= julianday('now')"""
        if catch_up:
            return [dict(r) for r in self.conn.execute(sql + " ORDER BY t.due_at LIMIT 3")]
        sql += " AND julianday(t.due_at) >= julianday('now') - (?/1440.0)"
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY t.due_at",
                                                   (int(window_min),))]

    def add_reminder(self, trace_id: int, session_id: str, text: str) -> bool:
        self.conn.execute(
            "INSERT INTO reminder(trace_id, session_id, text, created_at) VALUES (?,?,?,?)",
            (int(trace_id), session_id or "", text[:300], now_iso()))
        self.conn.execute("UPDATE trace SET reminded_at=? WHERE id=?", (now_iso(), int(trace_id)))
        self.conn.commit()
        self.notice_add(int(trace_id), "due")
        return True

    def pending_reminders(self, session_id: str, limit: int = 2) -> list[dict]:
        rows = self.conn.execute(
            """SELECT r.id, r.trace_id, r.text FROM reminder r
               JOIN trace t ON t.id=r.trace_id
               WHERE r.delivered_at IS NULL AND r.resolved=0
                 AND t.status='active' AND COALESCE(t.state,'open') IN ('open','missed')
                 AND (r.session_id=? OR r.session_id='' OR r.session_id IS NULL)
               ORDER BY r.id LIMIT ?""", (session_id, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def pending_recurring_reminders(self, limit: int = 2) -> list[dict]:
        """循环约定的提醒：到点就可以发，不等 30 分钟。"""
        rows = self.conn.execute(
            """SELECT r.id, r.trace_id, r.session_id, r.text, r.created_at, t.recur
               FROM reminder r JOIN trace t ON t.id=r.trace_id
               WHERE r.delivered_at IS NULL AND r.resolved=0
                 AND t.status='active' AND COALESCE(t.state,'open')='open'
                 AND COALESCE(t.recur,'')!=''
               ORDER BY r.id LIMIT ?""", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def pending_reminders_older(self, minutes: float, limit: int = 2) -> list[dict]:
        """排队超过 N 分钟还没投递出去的提醒。"""
        rows = self.conn.execute(
            """SELECT r.id, r.trace_id, r.session_id, r.text, r.created_at
               FROM reminder r JOIN trace t ON t.id=r.trace_id
               WHERE r.delivered_at IS NULL AND r.resolved=0
                 AND t.status='active' AND COALESCE(t.state,'open') IN ('open','missed')
                 AND julianday(r.created_at) <= julianday('now') - (?/1440.0)
               ORDER BY r.id LIMIT ?""", (float(minutes), int(limit))).fetchall()
        return [dict(r) for r in rows]

    def last_activity(self, session_id: str) -> str:
        """这个会话最后一次有动静是什么时候（原话优先，其次缓冲）。"""
        for tbl, cond in (("raw_message", "session_id=?"), ("pending_message", "session_id=?")):
            try:
                r = self.conn.execute(
                    f"SELECT MAX(created_at) m FROM {tbl} WHERE {cond}", (session_id,)).fetchone()
                if r and r["m"]:
                    return r["m"]
            except Exception:
                continue
        return ""

    def recent_replies(self, session_id: str, limit: int = 3) -> list[str]:
        rows = self.conn.execute(
            """SELECT text FROM raw_message WHERE session_id=? AND role='assistant'
               ORDER BY id DESC LIMIT ?""", (session_id, int(limit))).fetchall()
        return [str(r["text"] or "")[:120] for r in reversed(rows)]

    def mark_reminder_delivered(self, ids: list[int]) -> None:
        if not ids:
            return
        q = ",".join("?" * len(ids))
        self.conn.execute(f"UPDATE reminder SET delivered_at=? WHERE id IN ({q})",
                          [now_iso(), *ids])
        self.conn.commit()

    def miss_overdue_reminders(self, grace_min: int = 30) -> list[int]:
        """提醒过、过了宽限期她还没处理 → 约定状态改为 missed（未了）。"""
        rows = self.conn.execute(
            """SELECT r.id, r.trace_id, v.text FROM reminder r
               JOIN trace t ON t.id=r.trace_id
               JOIN trace_version v ON v.id=t.head_version_id
               WHERE r.resolved=0
                 AND COALESCE(t.state,'open')='open'
                 AND COALESCE(t.recur,'')=''
                 AND julianday(COALESCE(r.delivered_at, r.created_at)) <= julianday('now') - (?/1440.0)""",
            (int(grace_min),)).fetchall()
        ids, tids = [], []
        for r in rows:
            tid = int(r["trace_id"])
            ids.append(int(r["id"]))
            tids.append(tid)
            self.conn.execute("UPDATE trace SET state='missed' WHERE id=?", (tid,))
            # 转成「过点」通知：下一轮再递一次，让她知道自己错过了
            base = str(r["text"] or "")
            for _p in ("你答应过：", "过点了："):
                if base.startswith(_p):
                    base = base[len(_p):]
            base = base.replace("（说的时间到了）", "").strip("。 ")
            note = f"这件事过了时候：「{base[:60]}」——还挂着，你心里有数"
            self.conn.execute(
                "UPDATE reminder SET text=?, delivered_at=NULL, created_at=? WHERE id=?",
                (note[:300], now_iso(), int(r["id"])))
        self.conn.commit()
        return tids

    # ---------- 主题（她自己起） ----------

    def recalled_without_topics(self, limit: int = 5, days: float = 1.0) -> list[dict]:
        """最近被想起过、但还没有主题的记忆。"""
        import json as _json
        from datetime import datetime, timedelta
        cutoff = (datetime.now().astimezone() - timedelta(days=float(days))).isoformat()
        tids = set()
        for r in self.conn.execute(
                """SELECT trace_ids FROM recall_log WHERE created_at >= ?""", (cutoff,)):
            try:
                for x in _json.loads(r["trace_ids"] or "[]"):
                    tids.add(int(x))
            except Exception:
                continue
        out = []
        for tid in sorted(tids):
            row = self.conn.execute(
                """SELECT t.id, t.kind, v.text FROM trace t
                   JOIN trace_version v ON v.id=t.head_version_id
                   WHERE t.id=? AND t.status='active'
                     AND (v.topics IS NULL OR v.topics='' OR v.topics='[]')""", (tid,)).fetchone()
            if row:
                out.append(dict(row))
            if len(out) >= limit:
                break
        return out

    def set_topics(self, trace_id: int, topics: list[str]) -> bool:
        """给她起的主题落库：更新版本、重建关键词索引。"""
        import json as _json
        row = self.conn.execute("SELECT head_version_id FROM trace WHERE id=?", (trace_id,)).fetchone()
        if not row:
            return False
        self.conn.execute("UPDATE trace_version SET topics=? WHERE id=?",
                          (_json.dumps(topics, ensure_ascii=False), row["head_version_id"]))
        self.conn.commit()
        self.reindex_trace(trace_id)
        return True

    # ---------- 把一条长记忆拆成几段（原文不改字） ----------

    def split_trace(self, trace_id: int, pieces: list[str], source: str = "split") -> list[int]:
        """按给定的原文片段建新痕迹，原条归档并留下拆分关系。"""
        row = self.trace_row(trace_id)
        if row is None or len(pieces) < 2:
            return []
        head = self.conn.execute("SELECT * FROM trace_version WHERE id=?",
                                 (row["head_version_id"],)).fetchone()
        new_ids = []
        for i, piece in enumerate(pieces):
            nid = self.add_trace(
                piece, row["kind"], importance=float(row["importance"] or 0.5),
                episode_id=row["episode_id"], anchor_quote=head["anchor_quote"] if head else "",
                anchor_time=(head["anchor_time"] if head else "") or "",
                event_time=(head["event_time"] if head and "event_time" in head.keys() else "") or "",
                tags=[], source=source, reason="split",
                created_at=row["created_at"],
            )
            self.conn.execute(
                "UPDATE trace SET split_from=?, stability=?, last_active_at=?, activation_count=? WHERE id=?",
                (int(trace_id), float(row["stability"] or 7.0),
                 row["last_active_at"] or row["created_at"], int(row["activation_count"] or 0), nid))
            self.add_note(nid, f"拆自 #{trace_id}（第 {i+1} 段，原文未改）", kind="annotation")
            new_ids.append(nid)
        self.conn.execute("UPDATE trace SET status='archived', merged_into=NULL WHERE id=?", (int(trace_id),))
        self.add_note(trace_id, "已拆成 " + "、".join(f"#{i}" for i in new_ids), kind="annotation")
        self.conn.commit()
        return new_ids

    # ---------- 长文本切块（只索引，不改正文） ----------

    CHUNK_MIN_LEN = 150

    @staticmethod
    def split_sentences(text: str) -> list[str]:
        import re as _re
        parts = _re.split(r"(?<=[。！？；\n])", text or "")
        return [p.strip() for p in parts if len(p.strip()) >= 8]

    def rebuild_chunks(self, trace_id: int, text: str) -> int:
        """长文本切成句子存块（向量由调用方补）。短文本清空块。"""
        self.conn.execute("DELETE FROM trace_chunk WHERE trace_id=?", (trace_id,))
        if len(text or "") <= self.CHUNK_MIN_LEN:
            self.conn.commit()
            return 0
        parts = self.split_sentences(text)
        if len(parts) < 2:
            self.conn.commit()
            return 0
        for i, p in enumerate(parts):
            self.conn.execute(
                "INSERT INTO trace_chunk(trace_id, chunk_no, text, created_at) VALUES (?,?,?,?)",
                (trace_id, i, p[:500], now_iso()))
        self.conn.commit()
        return len(parts)

    def chunks_without_vec(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT id, trace_id, text FROM trace_chunk WHERE vec IS NULL LIMIT ?", (int(limit),))]

    def set_chunk_vec(self, chunk_id: int, vec_blob: bytes) -> None:
        self.conn.execute("UPDATE trace_chunk SET vec=? WHERE id=?", (vec_blob, int(chunk_id)))
        self.conn.commit()

    def all_chunk_vecs(self) -> dict[int, list]:
        out: dict[int, list] = {}
        for r in self.conn.execute("SELECT trace_id, vec FROM trace_chunk WHERE vec IS NOT NULL"):
            out.setdefault(int(r["trace_id"]), []).append(r["vec"])
        return out

    # ---------- 实体（轻量：只有名字和提及，不做图） ----------

    @staticmethod
    def norm_entity(name: str) -> str:
        t = (name or "").strip().lower()
        for ch in "「」『』“”\"'（）：:，,。.、 ":
            t = t.replace(ch, "")
        return t

    def upsert_entity(self, name: str, type_: str = "") -> int | None:
        norm = self.norm_entity(name)
        if len(norm) < 2 or len(norm) > 24:
            return None
        row = self.conn.execute("SELECT id FROM entity WHERE norm=?", (norm,)).fetchone()
        if row:
            return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO entity(name, norm, type, created_at) VALUES (?,?,?,?)",
            (name.strip()[:32], norm, type_ or "", now_iso()))
        return int(cur.lastrowid)

    def add_mention(self, trace_id: int, entity_id: int, weight: float = 1.0) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO mention(trace_id, entity_id, weight, created_at)
               VALUES (?,?,?,?)""", (trace_id, entity_id, float(weight), now_iso()))

    def entity_traces(self, query: str, limit: int = 60) -> list[dict]:
        """查询里出现了哪些实体名 → 这些实体关联的痕迹（含权重）。"""
        q = self.norm_entity(query or "")
        if len(q) < 2:
            return []
        rows = self.conn.execute(
            """SELECT e.norm, e.name, m.trace_id, m.weight FROM entity e
               JOIN mention m ON m.entity_id = e.id
               WHERE instr(?, e.norm) > 0
               ORDER BY LENGTH(e.norm) DESC LIMIT ?""", (q, int(limit))).fetchall()
        return [dict(r) for r in rows]

    # ---------- 原始对话归档（只追加，不进检索） ----------

    def add_raw(self, session_id: str, role: str, text: str,
                scope: str = "", sender: str = "", source: str = "live") -> None:
        text = (text or "").strip()
        if not text:
            return
        self.conn.execute(
            """INSERT INTO raw_message(session_id, scope, role, text, sender, created_at, source)
               VALUES (?,?,?,?,?,?,?)""",
            (session_id, scope, role, text[:4000], sender, now_iso(), source))
        self.conn.commit()

    def raw_recent(self, limit: int = 50, session_id: str = "") -> list[dict]:
        if session_id:
            rows = self.conn.execute(
                """SELECT * FROM raw_message WHERE session_id=? ORDER BY id DESC LIMIT ?""",
                (session_id, int(limit))).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM raw_message ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def raw_search(self, q: str, limit: int = 50) -> list[dict]:
        q = (q or "").strip()
        if not q:
            return []
        rows = self.conn.execute(
            """SELECT * FROM raw_message WHERE text LIKE ? ORDER BY id DESC LIMIT ?""",
            (f"%{q}%", int(limit))).fetchall()
        return [dict(r) for r in rows]

    def reindex_trace(self, trace_id: int) -> None:
        """按当前 head 版本的正文+主题，重建这条的 FTS。"""
        row = self.conn.execute("SELECT head_version_id FROM trace WHERE id=?", (trace_id,)).fetchone()
        if not row:
            return
        v = self.conn.execute("SELECT text, topics FROM trace_version WHERE id=?",
                              (row["head_version_id"],)).fetchone()
        if not v:
            return
        self.conn.execute("DELETE FROM trace_fts WHERE trace_id=?", (trace_id,))
        self.conn.execute("INSERT INTO trace_fts(text, trace_id) VALUES (?,?)",
                          (self.index_text(v["text"], v["topics"]), trace_id))
        self.conn.commit()

    def raw_purge(self, days: int) -> int:
        """删除超过 N 天的原话；days<=0 表示永久保留。"""
        if int(days) <= 0:
            return 0
        cur = self.conn.execute(
            """DELETE FROM raw_message
               WHERE julianday('now') - julianday(created_at) > ?""", (int(days),))
        self.conn.commit()
        return cur.rowcount

    # ---------- 更正（上下文覆盖） ----------

    def add_correction(self, session_id: str, text: str) -> None:
        self.conn.execute(
            "INSERT INTO correction(session_id, text, created_at) VALUES (?,?,?)",
            (session_id, text, now_iso()))
        self.conn.commit()

    def recent_corrections(self, session_id: str, hours: int = 24, limit: int = 8) -> list[str]:
        """取最近 N 小时的更正；不标记已用，所以会持续在场直到过期。

        hours <= 0 时退化成「只用一次」的旧行为。
        """
        if int(hours) <= 0:
            return self.take_corrections(session_id)
        rows = self.conn.execute(
            """SELECT text FROM correction
               WHERE session_id=?
                 AND julianday(created_at) >= julianday('now') - (? / 24.0)
               ORDER BY id DESC LIMIT ?""",
            (session_id, float(hours), int(limit))).fetchall()
        return [r["text"] for r in reversed(rows)]

    def take_corrections(self, session_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT id, text FROM correction WHERE session_id=? AND applied=0 ORDER BY id",
            (session_id,)).fetchall()
        if not rows:
            return []
        self.conn.execute("UPDATE correction SET applied=1 WHERE session_id=? AND applied=0", (session_id,))
        self.conn.commit()
        return [r["text"] for r in rows]

    # ---------- 待处理消息（M2） ----------

    def add_pending(self, session_id: str, role: str, text: str, scope: str = "",
                    sender: str = "") -> None:
        text = (text or "").strip()
        if not text:
            return
        self.conn.execute(
            "INSERT INTO pending_message(session_id, scope, role, text, sender, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (session_id, scope, role, text, (sender or "")[:24], now_iso()),
        )
        self.conn.commit()

    def pending_count(self, session_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM pending_message WHERE session_id=? AND consumed=0", (session_id,)
        ).fetchone()[0]

    def last_pending_at(self, session_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT created_at FROM pending_message WHERE session_id=? AND consumed=0 ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return row["created_at"] if row else None

    def stale_sessions(self, minutes: float, limit: int = 20) -> list[tuple[str, str]]:
        """列出缓冲里最老一行已经超过 N 分钟的会话（session, scope）。"""
        # created_at 是带时区的 ISO 串（2026-11-10T20:39:15+08:00），
        # 不能直接和 datetime('now') 的字符串比大小，用 julianday 统一换算
        rows = self.conn.execute(
            """SELECT session_id, MAX(scope) scope, MIN(created_at) oldest
               FROM pending_message WHERE consumed=0
               GROUP BY session_id
               HAVING julianday(oldest) <= julianday('now') - (? / 1440.0)
               ORDER BY oldest LIMIT ?""",
            (float(minutes), int(limit))).fetchall()
        return [(r["session_id"], r["scope"] or "") for r in rows]

    def take_pending(self, session_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM pending_message WHERE session_id=? AND consumed=0 ORDER BY id", (session_id,)
        ).fetchall()
        if not rows:
            return []
        self.conn.execute("UPDATE pending_message SET consumed=1 WHERE session_id=? AND consumed=0", (session_id,))
        self.conn.commit()
        return [{"id": int(r["id"]), "role": r["role"], "text": r["text"],
                 "created_at": r["created_at"], "scope": r["scope"],
                 "sender": (r["sender"] if "sender" in r.keys() else "") or ""} for r in rows]

    def restore_pending(self, ids: list[int]) -> int:
        """提炼失败时把这几行还回缓冲，别丢对话。"""
        if not ids:
            return 0
        q = ",".join("?" * len(ids))
        cur = self.conn.execute(f"UPDATE pending_message SET consumed=0 WHERE id IN ({q})",
                                [int(i) for i in ids])
        self.conn.commit()
        return int(cur.rowcount or 0)

    def find_duplicate(self, text: str) -> int | None:
        text = (text or "").strip()
        if len(text) < 6:
            return None
        row = self.conn.execute(
            """SELECT t.id FROM trace t JOIN trace_version v ON v.id=t.head_version_id
               WHERE v.text = ? LIMIT 1""",
            (text,),
        ).fetchone()
        return int(row["id"]) if row else None

    # ---------- 读取 ----------

    def trace_row(self, trace_id: int):
        return self.conn.execute("SELECT * FROM trace WHERE id=?", (trace_id,)).fetchone()

    def get_trace(self, trace_id: int, history: bool = False) -> dict | None:
        row = self.trace_row(trace_id)
        if row is None:
            return None
        head = self.conn.execute("SELECT * FROM trace_version WHERE id=?", (row["head_version_id"],)).fetchone()
        out = {
            "id": row["id"],
            "kind": row["kind"],
            "importance": row["importance"],
            "stability": round(float(row["stability"]), 2),
            "r_now": round(self.retrievability(row), 3),
            "activations": row["activation_count"],
            "versions": row["versions_count"],
            "status": row["status"],
            "avoided": bool(row["avoided"]),
            "tags": json.loads(row["tags"]) if row["tags"] else [],
            "created_at": row["created_at"],
            "text": head["text"] if head else "",
            "version_no": head["version_no"] if head else 0,
            "anchor_quote": head["anchor_quote"] if head else None,
            "anchor_time": head["anchor_time"] if head else None,
            "event_time": head["event_time"] if head and "event_time" in head.keys() else None,
            "felt": head["felt"] if head and "felt" in head.keys() else None,
            "reaction": head["reaction"] if head and "reaction" in head.keys() else None,
            "inference": head["inference"] if head and "inference" in head.keys() else None,
            "inference_certainty": head["inference_certainty"]
                if head and "inference_certainty" in head.keys() else None,
        }
        if history:
            out["chain"] = [
                {
                    "version_no": v["version_no"],
                    "text": v["text"],
                    "reason": v["reason"],
                    "caused_by_type": v["caused_by_type"],
                    "change_class": v["change_class"],
                    "created_at": v["created_at"],
                    "folded": bool(v["folded"]) if "folded" in v.keys() else False,
                    "event_time": v["event_time"] if "event_time" in v.keys() else None,
                }
                for v in self.conn.execute(
                    "SELECT * FROM trace_version WHERE trace_id=? ORDER BY version_no", (trace_id,)
                )
            ]
            out["notes"] = [
                {"kind": n["kind"], "text": n["text"], "created_at": n["created_at"]}
                for n in self.conn.execute(
                    "SELECT * FROM trace_note WHERE trace_id=? ORDER BY id", (trace_id,)
                )
            ]
        return out

    def directory(self, limit_groups: int = 12) -> dict:
        """只给轮廓：按标签聚合，外加未了的事、想起次数多的。"""
        total = self.conn.execute("SELECT COUNT(*) FROM trace WHERE status='active'").fetchone()[0]
        by_kind = {r["kind"]: r["n"] for r in self.conn.execute(
            "SELECT kind, COUNT(*) n FROM trace GROUP BY kind")}
        groups: dict[str, dict] = {}
        for row in self.conn.execute("SELECT id, tags, kind FROM trace WHERE status='active'"):
            tags = json.loads(row["tags"]) if row["tags"] else []
            if not tags:
                tags = ["未分类"]
            for t in tags:
                g = groups.setdefault(t, {"tag": t, "count": 0})
                g["count"] += 1
        top = sorted(groups.values(), key=lambda g: -g["count"])[:limit_groups]

        recalled = [
            {
                "id": r["id"],
                "kind": r["kind"],
                "activations": r["activation_count"],
                "days_span": round(days_between(r["created_at"], r["last_active_at"])),
                "text": (self.conn.execute(
                    "SELECT text FROM trace_version WHERE id=?", (r["head_version_id"],)).fetchone() or [""])[0][:40],
            }
            for r in self.conn.execute(
                "SELECT * FROM trace WHERE activation_count >= 3 ORDER BY activation_count DESC LIMIT 5")
        ]
        unresolved = [
            {"id": r["id"], "text": (self.conn.execute(
                "SELECT text FROM trace_version WHERE id=?", (r["head_version_id"],)).fetchone() or [""])[0][:60]}
            for r in self.conn.execute(
                """SELECT * FROM trace WHERE kind IN ('unresolved','promise') AND status='active'
                   AND COALESCE(state,'open')='open' LIMIT 8""")
        ]
        return {"total": total, "by_kind": by_kind, "tags": top,
                "most_recalled": recalled, "unresolved": unresolved}

    def search(self, query: str, limit: int = 10) -> list[dict]:
        q = (query or "").strip()
        if not q:
            return []
        # trigram 需要至少 3 个字；更短的查询退回子串匹配
        if len(q) >= 3:
            rows = self.conn.execute(
                """SELECT t.id, t.kind, v.text, bm25(trace_fts) AS score
                   FROM trace_fts
                   JOIN trace t ON t.id = trace_fts.trace_id
                   JOIN trace_version v ON v.id = t.head_version_id
                   WHERE trace_fts MATCH ?
                   ORDER BY score LIMIT ?""",
                (q, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT t.id, t.kind, v.text, 0.0 AS score
                   FROM trace t JOIN trace_version v ON v.id = t.head_version_id
                   WHERE v.text LIKE ? ORDER BY t.id DESC LIMIT ?""",
                (f"%{q}%", limit),
            ).fetchall()
        return [{"id": r["id"], "kind": r["kind"], "text": r["text"], "score": round(r["score"], 3)} for r in rows]

    def stats(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]
        return {
            "episodes": q("SELECT COUNT(*) FROM episode"),
            "traces": q("SELECT COUNT(*) FROM trace"),
            "versions": q("SELECT COUNT(*) FROM trace_version"),
            "notes": q("SELECT COUNT(*) FROM trace_note"),
            "recalled": q("SELECT COUNT(*) FROM recall_log"),
            "by_kind": {r["kind"]: r["n"] for r in self.conn.execute(
                "SELECT kind, COUNT(*) n FROM trace GROUP BY kind")},
        }
