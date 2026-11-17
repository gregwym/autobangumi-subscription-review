"""Separate durable queue. Never deletes AB history or media files."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

from .policy import Policy, candidate_id, fingerprint


class Store:
    def __init__(self, path: Path):
        self.path = path

    def _run(self, action, *args):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            return action(db, *args)

    async def call(self, action, *args):
        return await asyncio.to_thread(self._run, action, *args)

    async def init(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def create(db):
            db.executescript("""
                CREATE TABLE IF NOT EXISTS rules (
                    id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    policy TEXT, revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT PRIMARY KEY, rule_id INTEGER NOT NULL REFERENCES rules(id),
                    url TEXT NOT NULL, payload TEXT NOT NULL, first_seen REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'held', error TEXT
                );
                CREATE INDEX IF NOT EXISTS candidate_rule ON candidates(rule_id);
                CREATE TABLE IF NOT EXISTS delivered (
                    episode_key TEXT PRIMARY KEY, url TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reservations (
                    episode_key TEXT PRIMARY KEY, url TEXT NOT NULL
                );
            """)

        await self.call(create)

    @staticmethod
    def _rule(db, rule):
        rid, fp = rule["id"], fingerprint(rule)
        old = db.execute("SELECT fingerprint FROM rules WHERE id=?", (rid,)).fetchone()
        if old and old[0] != fp:
            db.execute(
                "UPDATE rules SET state='pending',policy=NULL,revision=revision+1 WHERE id=?",
                (rid,),
            )
            db.execute("DELETE FROM candidates WHERE rule_id=?", (rid,))
        db.execute(
            "INSERT INTO rules(id,fingerprint,payload) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET fingerprint=excluded.fingerprint,payload=excluded.payload",
            (rid, fp, json.dumps(rule, ensure_ascii=False)),
        )

    async def register(self, rule):
        await self.call(self._rule, rule)

    async def capture(self, rule, torrent, parsed):
        def add(db):
            self._rule(db, rule)
            cid = candidate_id(rule["id"], torrent["url"])
            data = {
                **torrent,
                **parsed,
                "id": cid,
                "rule_id": rule["id"],
                "rule_fingerprint": fingerprint(rule),
            }
            db.execute(
                "INSERT INTO candidates(id,rule_id,url,payload,first_seen) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (
                    cid,
                    rule["id"],
                    torrent["url"],
                    json.dumps(data, ensure_ascii=False),
                    time.time(),
                ),
            )
            return cid

        return await self.call(add)

    @staticmethod
    def _unpack(row):
        return {
            **json.loads(row["payload"]),
            "first_seen": row["first_seen"],
            "status": row["status"],
            "error": row["error"],
        }

    async def candidate(self, cid):
        def read(db):
            row = db.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
            return self._unpack(row) if row else None

        return await self.call(read)

    async def rules(self):
        def read(db):
            return [
                {
                    **json.loads(r["payload"]),
                    "state": r["state"],
                    "revision": r["revision"],
                    "policy": json.loads(r["policy"]) if r["policy"] else None,
                    "count": db.execute(
                        "SELECT COUNT(*) FROM candidates WHERE rule_id=?", (r["id"],)
                    ).fetchone()[0],
                }
                for r in db.execute("SELECT * FROM rules ORDER BY id DESC")
            ]

        return await self.call(read)

    async def candidates(self, rid=None):
        def read(db):
            rows = (
                db.execute(
                    "SELECT * FROM candidates WHERE rule_id=? ORDER BY first_seen,id", (rid,)
                )
                if rid is not None
                else db.execute("SELECT * FROM candidates ORDER BY first_seen,id")
            )
            return [self._unpack(r) for r in rows]

        return await self.call(read)

    async def set_policy(self, rid, policy, revision):
        policy.validate()

        def update(db):
            cur = db.execute(
                "UPDATE rules SET state='approved',policy=?,revision=revision+1 WHERE id=? AND revision=?",
                (json.dumps(policy.dump()), rid, revision),
            )
            if cur.rowcount != 1:
                raise ValueError("规则已变化，请刷新后重新预览")

        await self.call(update)

    async def hold(self, rid):
        await self.call(
            lambda db: (
                db.execute(
                    "UPDATE rules SET state='pending',revision=revision+1 WHERE id=?", (rid,)
                ).rowcount
            )
        )

    async def decision(self, candidate, override=None):
        def read(db):
            row = db.execute("SELECT * FROM rules WHERE id=?", (candidate["rule_id"],)).fetchone()
            if not row or row["fingerprint"] != candidate.get("rule_fingerprint"):
                return "规则身份已变化，请重新审核"
            if candidate["status"] in {"sent", "delivered"}:
                return "已投递"
            if (
                candidate.get("episode_key")
                and db.execute(
                    "SELECT 1 FROM delivered WHERE episode_key=?", (candidate["episode_key"],)
                ).fetchone()
            ):
                return "该集已有下载记录"
            reservation = db.execute(
                "SELECT url FROM reservations WHERE episode_key=?", (candidate.get("episode_key"),)
            ).fetchone()
            if reservation and reservation[0] != candidate["url"]:
                return "该集已有待确认投递；只重试原候选"
            if override is None and (not row or row["state"] != "approved"):
                return "等待版本审核"
            policy = override or Policy(**json.loads(row["policy"]))
            return policy.reason(candidate)

        return await self.call(read)

    async def reserve(self, candidate):
        def reserve(db):
            db.execute(
                "INSERT OR IGNORE INTO reservations VALUES(?,?)",
                (candidate["episode_key"], candidate["url"]),
            )
            url = db.execute(
                "SELECT url FROM reservations WHERE episode_key=?", (candidate["episode_key"],)
            ).fetchone()[0]
            if url != candidate["url"]:
                raise ValueError("该集已由另一个候选占用")
            db.execute("UPDATE candidates SET status='dispatching' WHERE id=?", (candidate["id"],))

        await self.call(reserve)

    async def mark(self, cid, status, error=None):
        await self.call(
            lambda db: (
                db.execute(
                    "UPDATE candidates SET status=?,error=? WHERE id=?", (status, error, cid)
                ).rowcount
            )
        )

    async def delivered(self, candidate, *, instance_id=None, save_path=None):
        def update(db):
            payload = dict(candidate)
            if instance_id is not None:
                payload["sent_downloader_id"] = instance_id
                payload["sent_save_path"] = save_path
            db.execute(
                "UPDATE candidates SET status='sent',error=NULL,payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), candidate["id"]),
            )
            if candidate.get("episode_key"):
                db.execute(
                    "INSERT OR IGNORE INTO delivered VALUES(?,?)",
                    (candidate["episode_key"], candidate["url"]),
                )

        await self.call(update)

    async def seed(self, rule, torrent, parsed):
        cid = await self.capture(rule, torrent, parsed)
        await self.delivered(await self.candidate(cid))
        await self.mark(cid, "delivered")
