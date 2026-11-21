"""Separate durable queue. Never deletes AB history or media files."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .audit import Audit
from .policy import Policy, candidate_id, fingerprint


class Store:
    def __init__(self, path: Path, audit=None):
        self.path = path
        self.audit = audit or Audit()

    def _run(self, action, *args):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
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
        old = db.execute("SELECT fingerprint,payload FROM rules WHERE id=?", (rid,)).fetchone()
        if old and old[0] != fp:
            db.execute(
                "UPDATE rules SET state='pending',policy=NULL,revision=revision+1 WHERE id=?",
                (rid,),
            )
            previous = json.loads(old[1])
            same_show = previous.get("official_title", "").casefold().strip() == rule.get(
                "official_title", ""
            ).casefold().strip() and previous.get("episode_type") == rule.get("episode_type")
            if not same_show:
                db.execute("DELETE FROM candidates WHERE rule_id=?", (rid,))
        db.execute(
            "INSERT INTO rules(id,fingerprint,payload) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET fingerprint=excluded.fingerprint,payload=excluded.payload",
            (rid, fp, json.dumps(rule, ensure_ascii=False)),
        )

    async def register(self, rule):
        await self.call(self._rule, rule)

    @classmethod
    def _capture(cls, db, rule, torrent, parsed, first_seen=None):
        cls._rule(db, rule)
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
                time.time() if first_seen is None else first_seen,
            ),
        )
        return cid

    async def capture(self, rule, torrent, parsed, *, first_seen=None):
        def capture(db):
            cid = candidate_id(rule["id"], torrent["url"])
            previous = db.execute("SELECT 1 FROM candidates WHERE id=?", (cid,)).fetchone()
            self._capture(db, rule, torrent, parsed, first_seen)
            return cid, not previous

        cid, created = await self.call(capture)
        if created:
            self.audit.emit(
                "candidate.captured", candidate_id=cid, rule_id=rule["id"], name=torrent["name"]
            )
        return cid

    async def move_candidate(self, candidate, rule, parsed):
        def move(db):
            # Uncertain dispatch must stay attached to the original rule.
            if db.execute("SELECT 1 FROM reservations WHERE url=?", (candidate["url"],)).fetchone():
                raise ValueError("候选有待确认投递，不能自动调整关联")
            cid = self._capture(db, rule, candidate, parsed)
            db.execute(
                "UPDATE candidates SET first_seen=MIN(first_seen,?) WHERE id=?",
                (candidate["first_seen"], cid),
            )
            db.execute("DELETE FROM candidates WHERE id=? AND status='held'", (candidate["id"],))

        await self.call(move)

    async def import_history(self, rules, entries):
        """One atomic commit avoids hundreds of NAS fsyncs during plugin setup."""

        def merge(db):
            for rule in rules:
                self._rule(db, rule)
            for rule, torrent, parsed, downloaded in entries:
                cid = self._capture(db, rule, torrent, parsed)
                if downloaded:
                    db.execute(
                        "UPDATE candidates SET status='delivered',error=NULL WHERE id=?", (cid,)
                    )
                    if parsed.get("episode_key"):
                        db.execute(
                            "INSERT OR IGNORE INTO delivered VALUES(?,?)",
                            (parsed["episode_key"], torrent["url"]),
                        )

        await self.call(merge)

    async def refresh_metadata(self, updates):
        def update(db):
            remapped = {}
            for cid, parsed in updates:
                row = db.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
                if not row:
                    continue
                payload = {**json.loads(row["payload"]), **parsed}
                db.execute(
                    "UPDATE candidates SET payload=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), cid),
                )
                key = parsed.get("episode_key")
                if key:
                    remapped.setdefault(row["url"], set()).add(key)
            # Dedup records retain the source URL. Rewrite only identities whose
            # source is still known; unrelated or unparseable history is preserved.
            delivered = list(db.execute("SELECT * FROM delivered"))
            for row in delivered:
                keys = remapped.get(row["url"])
                if keys and row["episode_key"] not in keys:
                    db.execute(
                        "DELETE FROM delivered WHERE episode_key=? AND url=?",
                        (row["episode_key"], row["url"]),
                    )
                    for key in keys:
                        db.execute("INSERT OR IGNORE INTO delivered VALUES(?,?)", (key, row["url"]))
            # Preserve uncertain-delivery reservations while moving their identity.
            for row in list(db.execute("SELECT * FROM reservations")):
                keys = remapped.get(row["url"])
                if keys and len(keys) == 1 and row["episode_key"] not in keys:
                    key = next(iter(keys))
                    existing = db.execute(
                        "SELECT url FROM reservations WHERE episode_key=?", (key,)
                    ).fetchone()
                    if existing is None or existing[0] == row["url"]:
                        db.execute(
                            "INSERT OR IGNORE INTO reservations VALUES(?,?)", (key, row["url"])
                        )
                        db.execute(
                            "DELETE FROM reservations WHERE episode_key=?", (row["episode_key"],)
                        )
            for row in db.execute(
                "SELECT payload,url,status FROM candidates WHERE status IN ('sent','delivered')"
            ):
                key = json.loads(row["payload"]).get("episode_key")
                if key:
                    db.execute("INSERT OR IGNORE INTO delivered VALUES(?,?)", (key, row["url"]))

        await self.call(update)

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

    async def rules(self, rid=None):
        def read(db):
            query = """
                SELECT r.*, (SELECT COUNT(*) FROM candidates WHERE rule_id=r.id) AS candidate_count
                FROM rules r
            """
            rows = (
                db.execute(query + " WHERE r.id=?", (rid,))
                if rid is not None
                else db.execute(query + " ORDER BY r.id DESC")
            )
            return [
                {
                    **json.loads(r["payload"]),
                    "state": r["state"],
                    "revision": r["revision"],
                    "policy": json.loads(r["policy"]) if r["policy"] else None,
                    "count": r["candidate_count"],
                }
                for r in rows
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

    async def decision(self, candidate, override=None, *, exclusion=None, scan_revisions=True):
        def read(db):
            row = db.execute("SELECT * FROM rules WHERE id=?", (candidate["rule_id"],)).fetchone()
            if not row or row["fingerprint"] != candidate.get("rule_fingerprint"):
                return "规则身份已变化，请重新审核"
            if candidate["status"] in {"sent", "delivered"}:
                return "已投递"
            if exclusion and (reason := exclusion(candidate["name"])):
                return reason
            if (
                candidate.get("episode_key")
                and db.execute(
                    "SELECT 1 FROM delivered WHERE episode_key=?", (candidate["episode_key"],)
                ).fetchone()
            ):
                return "同版本已有下载记录"
            reservation = db.execute(
                "SELECT url FROM reservations WHERE episode_key=?", (candidate.get("episode_key"),)
            ).fetchone()
            if reservation and reservation[0] != candidate["url"]:
                return "同版本已有待确认投递；只重试原候选"
            if override is None and (not row or row["state"] != "approved"):
                return "等待版本审核"
            policy = override or Policy(**json.loads(row["policy"]))
            reason = policy.reason(candidate)
            if reason:
                return reason
            if reservation or not scan_revisions:
                return None  # An uncertain send must retry the exact reserved URL.
            for other_row in db.execute(
                "SELECT * FROM candidates WHERE rule_id=?", (candidate["rule_id"],)
            ):
                other = self._unpack(other_row)
                if (
                    other.get("episode_key") == candidate.get("episode_key")
                    and other.get("revision", 1) > candidate.get("revision", 1)
                    and not (exclusion and exclusion(other["name"]))
                    and not policy.reason(other)
                ):
                    return f"同版本已有 v{other['revision']} 修订，跳过旧版"
            return None

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
