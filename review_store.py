# -*- coding: utf-8 -*-
"""放行内容复审库。

违规记录只留下「被拦下」的消息，**漏判**在那边永远看不到；这个库反过来，
把审核通过的消息留一份，供人工回看、标注、必要时补处置。

为什么单独用 SQLite 而不是塞进 state.json：

- 放行量远大于拦截量（正常群一天几百条），JSON 会越滚越大、每次保存全量重写；
- 复审需要按群 / 时间 / 来源 / 关键词 / 复审状态组合筛选和分页，SQL 天然合适；
- 存的是群聊原文，属于敏感数据，单独一个文件便于整体清空或删除。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Iterable

#: 四种复审结论
REVIEW_STATES = ("pending", "clean", "violation", "ignored")

#: 放行原因 -> 界面文案
SOURCE_LABELS = {
    "llm": "模型判无违规",
    "rule": "规则未命中",
    "rule_only": "仅规则初筛（未开模型）",
    "vision_error": "图片转述失败",
    "llm_error": "模型调用失败",
    "disabled": "审核未开启",
    "misjudge_skip": "人工标记误判",
}


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(str(source or ""), str(source or "未知"))


class ReviewStore:
    """放行消息的存储与复审状态维护。"""

    def __init__(self, db_path: str, limit: int = 2000) -> None:
        self.db_path = db_path
        self.limit = max(100, int(limit))
        self._lock = threading.Lock()
        self._writes_since_prune = 0
        self._db = sqlite3.connect(db_path, check_same_thread=False, timeout=5)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
        except Exception:  # pragma: no cover - 老版本 sqlite 忽略即可
            pass
        self._init_db()

    # ---------------------------------------------------------------- 建表
    def _init_db(self) -> None:
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS passed_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          REAL NOT NULL,
                day         TEXT NOT NULL DEFAULT '',
                clock       TEXT NOT NULL DEFAULT '',
                group_id    TEXT NOT NULL DEFAULT '',
                group_name  TEXT NOT NULL DEFAULT '',
                user_id     TEXT NOT NULL DEFAULT '',
                user_name   TEXT NOT NULL DEFAULT '',
                text        TEXT NOT NULL DEFAULT '',
                image       TEXT NOT NULL DEFAULT '',
                has_image   INTEGER NOT NULL DEFAULT 0,
                source      TEXT NOT NULL DEFAULT 'rule',
                risk        INTEGER NOT NULL DEFAULT 0,
                msg_id      TEXT NOT NULL DEFAULT '',
                review      TEXT NOT NULL DEFAULT 'pending',
                note        TEXT NOT NULL DEFAULT '',
                reviewer    TEXT NOT NULL DEFAULT '',
                reviewed_at REAL NOT NULL DEFAULT 0
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_passed_review ON passed_messages(review, id)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_passed_group ON passed_messages(group_id, id)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_passed_day ON passed_messages(day, id)"
        )
        self._db.commit()

    # ---------------------------------------------------------------- 写入
    def add(
        self,
        *,
        group_id: str,
        group_name: str = "",
        user_id: str = "",
        user_name: str = "",
        text: str = "",
        source: str = "rule",
        risk: int = 0,
        msg_id: str = "",
        image: str = "",
        has_image: bool = False,
        ts: float | None = None,
    ) -> int:
        """记一条放行消息，返回自增 id；失败返回 0（复审不影响正常聊天）。"""
        moment = float(ts if ts is not None else time.time())
        day = time.strftime("%Y-%m-%d", time.localtime(moment))
        clock = time.strftime("%m-%d %H:%M:%S", time.localtime(moment))
        try:
            with self._lock:
                cur = self._db.execute(
                    "INSERT INTO passed_messages (ts, day, clock, group_id, group_name,"
                    " user_id, user_name, text, image, has_image, source, risk, msg_id)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        moment,
                        day,
                        clock,
                        str(group_id or ""),
                        str(group_name or ""),
                        str(user_id or ""),
                        str(user_name or ""),
                        str(text or "")[:400],
                        str(image or ""),
                        1 if has_image or image else 0,
                        str(source or "rule"),
                        1 if int(risk or 0) > 0 else 0,
                        str(msg_id or ""),
                    ),
                )
                self._db.commit()
                rowid = int(cur.lastrowid or 0)
                self._writes_since_prune += 1
                if self._writes_since_prune >= 100:
                    self._writes_since_prune = 0
                    self._prune_locked()
                return rowid
        except Exception:
            return 0

    def _prune_locked(self) -> None:
        try:
            self._db.execute(
                "DELETE FROM passed_messages WHERE id NOT IN ("
                " SELECT id FROM passed_messages ORDER BY id DESC LIMIT ?)",
                (self.limit,),
            )
            self._db.commit()
        except Exception:
            pass

    def prune(self) -> None:
        with self._lock:
            self._prune_locked()

    # ---------------------------------------------------------------- 查询
    def list_items(
        self,
        *,
        page: int = 1,
        size: int = 20,
        keyword: str = "",
        group: str = "",
        review: str = "",
        source: str = "",
        day: str = "",
        risky_only: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []

        if keyword:
            like = f"%{keyword}%"
            where.append(
                "(text LIKE ? OR user_name LIKE ? OR user_id LIKE ? OR group_name LIKE ?)"
            )
            params.extend([like, like, like, like])
        if group:
            where.append("group_id = ?")
            params.append(group)
        if review:
            where.append("review = ?")
            params.append(review)
        if source:
            where.append("source = ?")
            params.append(source)
        if day:
            where.append("day = ?")
            params.append(day)
        if risky_only:
            where.append("risk = 1")

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = int(
                self._db.execute(
                    f"SELECT COUNT(*) FROM passed_messages{clause}", tuple(params)
                ).fetchone()[0]
            )
            rows = self._db.execute(
                f"SELECT * FROM passed_messages{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                tuple(params) + (max(1, int(size)), max(0, (max(1, int(page)) - 1) * int(size))),
            ).fetchall()
        return [dict(row) for row in rows], total

    def get(self, item_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM passed_messages WHERE id = ?", (int(item_id),)
            ).fetchone()
        return dict(row) if row else None

    def mark_image(self, item_id: int, image: str) -> None:
        """给老记录补上回填的图片文件名。"""
        try:
            with self._lock:
                self._db.execute(
                    "UPDATE passed_messages SET image = ?, has_image = 1 WHERE id = ?",
                    (str(image or ""), int(item_id)),
                )
                self._db.commit()
        except Exception:
            pass

    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT review, COUNT(*) AS n FROM passed_messages GROUP BY review"
            ).fetchall()
            total = int(
                self._db.execute("SELECT COUNT(*) FROM passed_messages").fetchone()[0]
            )
            risky = int(
                self._db.execute(
                    "SELECT COUNT(*) FROM passed_messages WHERE risk = 1 AND review = 'pending'"
                ).fetchone()[0]
            )
        data = {state: 0 for state in REVIEW_STATES}
        for row in rows:
            data[str(row["review"])] = int(row["n"] or 0)
        data["total"] = total
        data["risky_pending"] = risky
        return data

    def groups(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT group_id, MAX(group_name) AS group_name, COUNT(*) AS n"
                " FROM passed_messages GROUP BY group_id ORDER BY n DESC LIMIT 200"
            ).fetchall()
        return [dict(row) for row in rows]

    def days(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT day, COUNT(*) AS n FROM passed_messages"
                " GROUP BY day ORDER BY day DESC LIMIT 30"
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------------------------------------------------------------- 标注
    def mark(
        self,
        ids: Iterable[int],
        review: str,
        note: str | None = None,
        reviewer: str = "",
    ) -> int:
        """更新复审结论；note 传 None 表示保留原备注。"""
        review = str(review or "")
        if review not in REVIEW_STATES:
            raise ValueError(f"不支持的复审结论：{review}")
        clean_ids = [int(i) for i in ids if str(i).strip()]
        if not clean_ids:
            return 0
        placeholders = ",".join("?" for _ in clean_ids)
        now = time.time()
        with self._lock:
            if note is None:
                cur = self._db.execute(
                    f"UPDATE passed_messages SET review = ?, reviewer = ?, reviewed_at = ?"
                    f" WHERE id IN ({placeholders})",
                    (review, reviewer, now, *clean_ids),
                )
            else:
                cur = self._db.execute(
                    f"UPDATE passed_messages SET review = ?, note = ?, reviewer = ?,"
                    f" reviewed_at = ? WHERE id IN ({placeholders})",
                    (review, str(note)[:200], reviewer, now, *clean_ids),
                )
            self._db.commit()
            return int(cur.rowcount or 0)

    def clear(self) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM passed_messages")
            self._db.commit()
            return int(cur.rowcount or 0)

    def close(self) -> None:
        try:
            with self._lock:
                self._db.commit()
                self._db.close()
        except Exception:
            pass
