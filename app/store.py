"""SQLite 存储层。标准库 sqlite3，同步调用放进线程池，避免阻塞事件循环。"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sqlite3

# 必须和 app/context.py 的 PROACTIVE_MARK 一模一样。
# 这里不能 import 那个常量：context 要 import store，反向 import 会成环。
# tests/smoke.py 里有一条断言专门盯着这两个值不许分家。
_PROACTIVE_MARK = "【他没说话】"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT    NOT NULL,
  role        TEXT    NOT NULL,          -- user / assistant
  content     TEXT    NOT NULL,          -- 实际进上下文的那份文本（含动态包装），用于原样回放以命中缓存
  display     TEXT,                      -- 给人看的干净文本
  created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS summaries (
  session_id        TEXT PRIMARY KEY,
  summary           TEXT NOT NULL,
  covered_until_id  INTEGER NOT NULL DEFAULT 0,
  updated_at        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS profile (
  session_id  TEXT NOT NULL,
  k           TEXT NOT NULL,
  v           TEXT,
  updated_at  REAL,
  PRIMARY KEY (session_id, k)
);

CREATE TABLE IF NOT EXISTS facts (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id   TEXT NOT NULL,
  content      TEXT NOT NULL,
  importance   REAL NOT NULL DEFAULT 0.5,
  tokens       TEXT,                      -- 检索词元 JSON（本地算，不花 token）
  source_msg_id INTEGER,
  created_at   REAL NOT NULL,
  last_access  REAL,
  access_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_facts_session ON facts(session_id);

CREATE TABLE IF NOT EXISTS kv (
  k          TEXT PRIMARY KEY,
  v          TEXT,
  updated_at REAL NOT NULL
);

-- 自动白名单：前 N 个私聊进来的人。存库里而不是写回 config.yaml，
-- 免得程序擅自去改用户的配置文件。
CREATE TABLE IF NOT EXISTS allowlist (
  uid       INTEGER PRIMARY KEY,
  nickname  TEXT,
  added_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            REAL NOT NULL,
  session_id    TEXT,
  model         TEXT,
  purpose       TEXT NOT NULL DEFAULT 'chat',
  cache_hit     INTEGER NOT NULL DEFAULT 0,
  cache_miss    INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cost_yuan     REAL NOT NULL DEFAULT 0,
  latency_ms    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);

-- B 站视频推送。一个 bvid 只允许出现一次：推过的不再推，AI 毙掉的也不再重判，
-- 否则每天都要为同一批垃圾内容重复付筛选费。
CREATE TABLE IF NOT EXISTS video_push (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  bvid       TEXT    NOT NULL,
  keyword    TEXT    NOT NULL,
  title      TEXT,
  author     TEXT,
  url        TEXT,
  duration   INTEGER NOT NULL DEFAULT 0,
  play       INTEGER NOT NULL DEFAULT 0,
  pubdate    INTEGER NOT NULL DEFAULT 0,
  score      INTEGER NOT NULL DEFAULT 0,
  reason     TEXT,
  task_id    TEXT,
  note_path  TEXT,
  status     TEXT    NOT NULL DEFAULT 'pending',   -- pending/generating/ready/sent/failed/rejected
  attempts   INTEGER NOT NULL DEFAULT 0,
  error      TEXT,
  sent_to    TEXT,                                 -- JSON 数组：已经发给了谁
  created_at REAL    NOT NULL,
  updated_at REAL    NOT NULL,
  sent_at    REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vpush_bvid ON video_push(bvid);
CREATE INDEX IF NOT EXISTS idx_vpush_status ON video_push(status, id);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = asyncio.Lock()
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """给老库补列。SQLite 的 ALTER TABLE ADD COLUMN 是安全的幂等操作。"""
        want = {
            "facts": [("tokens", "TEXT"), ("source_msg_id", "INTEGER")],
            "messages": [],
        }
        for table, cols in want.items():
            have = {r["name"] for r in
                    self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in cols:
                if name not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    async def _run(self, fn, *args):
        """把同步 sqlite 调用丢进线程池。"""
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    # ---------------- messages ----------------

    def _add_message(self, session_id: str, role: str, content: str, display: Optional[str]) -> int:
        cur = self._conn.execute(
            "INSERT INTO messages(session_id, role, content, display, created_at) VALUES (?,?,?,?,?)",
            (session_id, role, content, display, time.time()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    async def add_message(self, session_id: str, role: str, content: str,
                          display: Optional[str] = None) -> int:
        return await self._run(self._add_message, session_id, role, content, display)

    def _history(self, session_id: str, after_id: int) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, role, content, display, created_at FROM messages "
            "WHERE session_id=? AND id>? ORDER BY id ASC",
            (session_id, after_id),
        ).fetchall()
        return [dict(r) for r in rows]

    async def history(self, session_id: str, after_id: int = 0) -> List[Dict[str, Any]]:
        return await self._run(self._history, session_id, after_id)

    # ---------------- summary ----------------

    def _get_summary(self, session_id: str) -> Tuple[str, int]:
        row = self._conn.execute(
            "SELECT summary, covered_until_id FROM summaries WHERE session_id=?", (session_id,)
        ).fetchone()
        if not row:
            return "", 0
        return row["summary"], int(row["covered_until_id"])

    async def get_summary(self, session_id: str) -> Tuple[str, int]:
        return await self._run(self._get_summary, session_id)

    def _set_summary(self, session_id: str, summary: str, covered_until_id: int) -> None:
        self._conn.execute(
            "INSERT INTO summaries(session_id, summary, covered_until_id, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET summary=excluded.summary, "
            "covered_until_id=excluded.covered_until_id, updated_at=excluded.updated_at",
            (session_id, summary, covered_until_id, time.time()),
        )
        self._conn.commit()

    async def set_summary(self, session_id: str, summary: str, covered_until_id: int) -> None:
        await self._run(self._set_summary, session_id, summary, covered_until_id)

    # ---------------- profile ----------------

    def _get_profile(self, session_id: str) -> Dict[str, str]:
        rows = self._conn.execute(
            "SELECT k, v FROM profile WHERE session_id=? ORDER BY k", (session_id,)
        ).fetchall()
        return {r["k"]: r["v"] for r in rows}

    async def get_profile(self, session_id: str) -> Dict[str, str]:
        return await self._run(self._get_profile, session_id)

    def _set_profile(self, session_id: str, k: str, v: str) -> None:
        self._conn.execute(
            "INSERT INTO profile(session_id, k, v, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(session_id, k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
            (session_id, k, v, time.time()),
        )
        self._conn.commit()

    async def set_profile(self, session_id: str, k: str, v: str) -> None:
        await self._run(self._set_profile, session_id, k, v)

    # ---------------- usage ----------------

    def _add_usage(self, session_id, model, purpose, cache_hit, cache_miss,
                   output_tokens, cost, latency_ms) -> None:
        self._conn.execute(
            "INSERT INTO usage(ts, session_id, model, purpose, cache_hit, cache_miss, "
            "output_tokens, cost_yuan, latency_ms) VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), session_id, model, purpose, cache_hit, cache_miss,
             output_tokens, cost, latency_ms),
        )
        self._conn.commit()

    async def add_usage(self, **kw) -> None:
        # 注意不能写成 self._run(self._add_usage, **kw) —— kwargs 会落在 _run 上而不是 _add_usage 上
        await self._run(lambda: self._add_usage(**kw))

    # ---------------- 成本聚合（预算守卫用） ----------------

    def _spent_since(self, ts: float) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost_yuan),0) AS s FROM usage WHERE ts>=?", (ts,)
        ).fetchone()
        return float(row["s"] or 0.0)

    async def spent_since(self, ts: float) -> float:
        return await self._run(self._spent_since, ts)

    # ---------------- 长期记忆 facts（P3） ----------------

    def _add_fact(self, session_id: str, content: str, importance: float,
                  tokens: str, source_msg_id: Optional[int]) -> int:
        cur = self._conn.execute(
            "INSERT INTO facts(session_id, content, importance, tokens, source_msg_id, "
            "created_at, last_access, access_count) VALUES (?,?,?,?,?,?,?,0)",
            (session_id, content, importance, tokens, source_msg_id, time.time(), time.time()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    async def add_fact(self, session_id: str, content: str, importance: float = 0.5,
                       tokens: str = "[]", source_msg_id: Optional[int] = None) -> int:
        return await self._run(self._add_fact, session_id, content, importance,
                               tokens, source_msg_id)

    def _facts(self, session_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, content, importance, tokens, created_at, last_access, access_count "
            "FROM facts WHERE session_id=? ORDER BY id ASC", (session_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    async def facts(self, session_id: str) -> List[Dict[str, Any]]:
        return await self._run(self._facts, session_id)

    def _update_fact(self, fact_id: int, content: str, importance: float, tokens: str) -> None:
        self._conn.execute(
            "UPDATE facts SET content=?, importance=?, tokens=?, last_access=? WHERE id=?",
            (content, importance, tokens, time.time(), fact_id),
        )
        self._conn.commit()

    async def update_fact(self, fact_id: int, content: str, importance: float,
                          tokens: str) -> None:
        await self._run(self._update_fact, fact_id, content, importance, tokens)

    def _touch_facts(self, ids: List[int]) -> None:
        if not ids:
            return
        now = time.time()
        self._conn.executemany(
            "UPDATE facts SET last_access=?, access_count=access_count+1 WHERE id=?",
            [(now, i) for i in ids],
        )
        self._conn.commit()

    async def touch_facts(self, ids: List[int]) -> None:
        await self._run(self._touch_facts, list(ids))

    # ---------------- 会话与时间（主动搭话用） ----------------

    def _sessions(self, private_only: bool = True) -> List[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT session_id FROM messages ORDER BY session_id"
        ).fetchall()
        out = [r["session_id"] for r in rows]
        return [s for s in out if s.startswith("private:")] if private_only else out

    async def sessions(self, private_only: bool = True) -> List[str]:
        return await self._run(self._sessions, private_only)

    def _last_message_at(self, session_id: str, role: Optional[str] = None) -> float:
        sql = "SELECT MAX(created_at) AS t FROM messages WHERE session_id=?"
        args: List[Any] = [session_id]
        if role:
            sql += " AND role=?"
            args.append(role)
        row = self._conn.execute(sql, args).fetchone()
        return float(row["t"] or 0.0)

    async def last_message_at(self, session_id: str, role: Optional[str] = None) -> float:
        return await self._run(self._last_message_at, session_id, role)

    def _count_turns(self, session_id: str, after_id: int = 0) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id=? AND role='user' AND id>?",
            (session_id, after_id),
        ).fetchone()
        return int(row["n"] or 0)

    async def count_turns(self, session_id: str, after_id: int = 0) -> int:
        return await self._run(self._count_turns, session_id, after_id)

    def _last_message(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT id, role, content, display, created_at FROM messages "
            "WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    async def last_message(self, session_id: str) -> Optional[Dict[str, Any]]:
        """最后一条消息（含 id/role），主动搭话要用它判断"是不是轮到我开口"。

        规则：**他回你之前，你最多主动找他一次。** 连着追问是明显的机器行为。
        """
        return await self._run(self._last_message, session_id)

    def _last_user_id(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(id) AS i FROM messages WHERE session_id=? AND role='user'",
            (session_id,),
        ).fetchone()
        return int(row["i"] or 0)

    async def last_user_id(self, session_id: str) -> int:
        return await self._run(self._last_user_id, session_id)

    def _last_real_user_id(self, session_id: str) -> int:
        """最后一条**真的是他说的话**的消息 id。

        为什么要单独一个方法：主动搭话也要往历史里落一条 role='user' 的消息
        （否则下一轮前缀对不上、缓存全废），那条的开头是 PROACTIVE_MARK。
        如果拿 last_user_id 去判「他回过我没有」，我们自己发的那条会把水位顶上去，
        于是「他没回就不再追」这条护栏永远失效 —— 实测被自己顶掉过，一天追了 17 次。
        所以这里把带标记的那类消息排除掉。
        """
        row = self._conn.execute(
            "SELECT MAX(id) AS i FROM messages "
            "WHERE session_id=? AND role='user' "
            "AND (content IS NULL OR instr(content, ?) = 0)",
            (session_id, _PROACTIVE_MARK),
        ).fetchone()
        return int(row["i"] or 0)

    async def last_real_user_id(self, session_id: str) -> int:
        return await self._run(self._last_real_user_id, session_id)

    # ---------------- 运行时键值（计数、水位线） ----------------

    def _kv_get(self, k: str) -> Optional[str]:
        row = self._conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return None if row is None else row["v"]

    async def kv_get(self, k: str, default: Optional[str] = None) -> Optional[str]:
        v = await self._run(self._kv_get, k)
        return default if v is None else v

    def _kv_set(self, k: str, v: str) -> None:
        self._conn.execute(
            "INSERT INTO kv(k, v, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
            (k, v, time.time()),
        )
        self._conn.commit()

    async def kv_set(self, k: str, v: Any) -> None:
        await self._run(self._kv_set, k, str(v))

    async def kv_get_int(self, k: str, default: int = 0) -> int:
        v = await self.kv_get(k)
        try:
            return int(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    # ---------------- 自动白名单 ----------------

    def _allowlist(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT uid, nickname, added_at FROM allowlist ORDER BY added_at"
        ).fetchall()
        return [dict(r) for r in rows]

    async def allowlist(self) -> List[Dict[str, Any]]:
        return await self._run(self._allowlist)

    def _allowlist_add(self, uid: int, nickname: str) -> None:
        self._conn.execute(
            "INSERT INTO allowlist(uid, nickname, added_at) VALUES (?,?,?) "
            "ON CONFLICT(uid) DO UPDATE SET nickname=excluded.nickname",
            (uid, nickname, time.time()),
        )
        self._conn.commit()

    async def allowlist_add(self, uid: int, nickname: str = "") -> None:
        await self._run(self._allowlist_add, uid, nickname)

    def _allowlist_has(self, uid: int) -> bool:
        row = self._conn.execute("SELECT 1 FROM allowlist WHERE uid=?", (uid,)).fetchone()
        return row is not None

    async def allowlist_has(self, uid: int) -> bool:
        return await self._run(self._allowlist_has, uid)

    async def allowlist_count(self) -> int:
        return len(await self.allowlist())


    # ---------------- 视频推送 ----------------
    # 这一组刻意用同步方法：一天只跑几次，每次几条，阻塞几毫秒，
    # 换来的好处是调度逻辑（_due/_cycle）里不用到处 await，读起来清楚。

    _VPUSH_COLS = (
        "bvid", "keyword", "title", "author", "url", "duration", "play",
        "pubdate", "score", "reason", "task_id", "note_path", "status",
        "attempts", "error", "sent_to", "sent_at",
    )

    def vpush_add(self, **kw) -> int:
        """新候选入库。bvid 已存在返回 0（静默跳过，说明推过了）。"""
        now = time.time()
        data = {k: kw.get(k) for k in self._VPUSH_COLS if k in kw}
        data.setdefault("status", "pending")
        data["created_at"] = now
        data["updated_at"] = now
        cols = ",".join(data)
        marks = ",".join("?" for _ in data)
        cur = self._conn.execute(
            f"INSERT OR IGNORE INTO video_push({cols}) VALUES ({marks})",
            tuple(data.values()),
        )
        self._conn.commit()
        return int(cur.lastrowid) if cur.rowcount else 0

    def vpush_update(self, vid: int, **kw) -> None:
        data = {k: v for k, v in kw.items() if k in self._VPUSH_COLS}
        if "sent_to" in data and isinstance(data["sent_to"], (list, tuple)):
            data["sent_to"] = json.dumps(list(data["sent_to"]))
        if not data:
            return
        data["updated_at"] = time.time()
        sets = ",".join(f"{k}=?" for k in data)
        self._conn.execute(f"UPDATE video_push SET {sets} WHERE id=?",
                           tuple(data.values()) + (int(vid),))
        self._conn.commit()

    def vpush_row(self, vid: int) -> Optional[Dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM video_push WHERE id=?", (int(vid),)).fetchone()
        return dict(row) if row else None

    def vpush_rows(self, statuses: Optional[tuple] = None,
                   limit: int = 50) -> List[Dict[str, Any]]:
        if statuses:
            marks = ",".join("?" for _ in statuses)
            rows = self._conn.execute(
                f"SELECT * FROM video_push WHERE status IN ({marks}) ORDER BY id ASC LIMIT ?",
                tuple(statuses) + (int(limit),),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM video_push ORDER BY id DESC LIMIT ?", (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def vpush_known(self, bvids) -> set:
        bvids = [b for b in bvids if b]
        if not bvids:
            return set()
        marks = ",".join("?" for _ in bvids)
        rows = self._conn.execute(
            f"SELECT bvid FROM video_push WHERE bvid IN ({marks})", tuple(bvids)
        ).fetchall()
        return {r["bvid"] for r in rows}

    def vpush_count_since(self, ts: float, statuses: tuple = ()) -> int:
        if statuses:
            marks = ",".join("?" for _ in statuses)
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM video_push WHERE created_at>=? AND status IN ({marks})",
                (float(ts),) + tuple(statuses),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM video_push WHERE created_at>=?", (float(ts),)
            ).fetchone()
        return int(row["n"] or 0)

    def vpush_mark_rejected(self, items) -> None:
        """把 AI 看过但没选中的候选记下来，避免下次重复送审。"""
        if not items:
            return
        now = time.time()
        self._conn.executemany(
            "INSERT OR IGNORE INTO video_push"
            "(bvid,keyword,title,author,url,duration,play,pubdate,status,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,'rejected',?,?)",
            [tuple(x) + (now, now) for x in items],
        )
        self._conn.commit()

    def vpush_prune(self, days: int = 45) -> int:
        """清掉旧的 rejected 记录，控制表大小。"""
        cut = time.time() - max(1, int(days)) * 86400
        cur = self._conn.execute(
            "DELETE FROM video_push WHERE status='rejected' AND created_at<?", (cut,)
        )
        self._conn.commit()
        return int(cur.rowcount or 0)

    # ---------------- 只读查询（工具脚本用） ----------------

    def query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """同步只读查询，返回 dict 列表。给 tools/ 下的报表脚本用。"""
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]
