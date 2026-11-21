"""Focused history maintenance, with previews and SQLite backups, not arbitrary SQL."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace

from .audit import Audit


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def selection(key):
    identity = json.loads(key)
    if identity[1] == "feature":
        return "feature"
    if len(identity) == 6 and identity[5] is not None:
        return f"batch:{identity[3]}:{identity[5]}"
    return identity[3]


class Maintenance:
    def __init__(self, host_path, review_path, parse):
        self.host_path, self.review_path, self.parse = Path(host_path), Path(review_path), parse

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.host_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("ATTACH DATABASE ? AS review", (str(self.review_path),))
            yield db
        finally:
            db.close()

    def read(self, db):
        return {
            table: [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in (
                "bangumi",
                "torrent",
                "review.rules",
                "review.candidates",
                "review.delivered",
                "review.reservations",
            )
        }

    def scope(self, data, rid):
        host_rules = {r["id"]: r for r in data["bangumi"]}
        stored_rules = {r["id"]: json.loads(r["payload"]) for r in data["review.rules"]}
        target = host_rules.get(rid) or stored_rules.get(rid)
        if not target:
            raise ValueError("番剧规则已不存在，请刷新")
        prefix = [
            target["official_title"].casefold().strip(),
            target.get("episode_type", "episode"),
            target["season"] + (target.get("season_offset") or 0),
        ]

        def relevant(key):
            try:
                value = json.loads(key)
                return (
                    isinstance(value, list)
                    and len(value) in (4, 6)
                    and (value[:3] == prefix or value[:3] == [prefix[0], "feature", prefix[2]])
                )
            except TypeError, ValueError:
                return False

        candidates = []
        url_keys = {}
        for row in data["review.candidates"]:
            payload = json.loads(row["payload"])
            key = payload.get("episode_key")
            if relevant(key):
                candidates.append({**row, "data": payload, "key": key})
                url_keys[row["url"]] = key
        torrents = []
        for row in data["torrent"]:
            key = url_keys.get(row["url"])
            rule = host_rules.get(row["bangumi_id"])
            if rule:
                key = (
                    self.parse(SimpleNamespace(**rule), row["name"], row["url"]).get("episode_key")
                    or key
                )
            if relevant(key):
                torrents.append({**row, "key": key})
        keys = {r["key"] for r in torrents if r["downloaded"]}
        keys.update(r["key"] for r in candidates if r["status"] in ("sent", "delivered"))
        keys.update(
            r["episode_key"] for r in data["review.delivered"] if relevant(r["episode_key"])
        )
        related = (
            {rid}
            | {r["rule_id"] for r in candidates}
            | {r["bangumi_id"] for r in torrents if r["bangumi_id"]}
        )
        for key, rule in (host_rules | stored_rules).items():
            if [
                rule["official_title"].casefold().strip(),
                rule.get("episode_type", "episode"),
                rule["season"] + (rule.get("season_offset") or 0),
            ] == prefix:
                related.add(key)
        return target, candidates, torrents, keys, related

    def view(self, data, rid):
        target, candidates, torrents, keys, related = self.scope(data, rid)
        episodes = []
        by_selection = {}
        for key in keys:
            by_selection.setdefault(selection(key), set()).add(key)
        for selected, resource_keys in sorted(
            by_selection.items(), key=lambda item: (isinstance(item[0], str), item[0])
        ):
            if selected == "feature":
                label = "单片"
                episode = None
            elif isinstance(selected, str):
                _, start, end = selected.split(":")
                label = f"合集 {start}–{end} 集"
                episode = None
            else:
                label = f"第 {selected:g} 集"
                episode = selected
            names = sorted(
                {r["name"] for r in torrents if r["key"] in resource_keys and r["downloaded"]}
                | {
                    r["data"]["name"]
                    for r in candidates
                    if r["key"] in resource_keys and r["status"] in ("sent", "delivered")
                }
            )
            episodes.append(
                {
                    "episode": episode,
                    "selection": selected,
                    "label": label,
                    "names": names,
                    "host_records": sum(
                        r["key"] in resource_keys and bool(r["downloaded"]) for r in torrents
                    ),
                    "review_records": sum(
                        r["key"] in resource_keys and r["status"] in ("sent", "delivered")
                        for r in candidates
                    ),
                }
            )
        return {
            "title": target["official_title"],
            "season": target["season"] + (target.get("season_offset") or 0),
            "episodes": episodes,
            "related_rules": sorted(related),
        }

    def history(self, rid):
        with self.connect() as db:
            db.execute("BEGIN")
            return self.view(self.read(db), rid)

    def rules(self):
        with self.connect() as db:
            db.execute("BEGIN")
            data = self.read(db)
            rules = {
                r["id"]: {**json.loads(r["payload"]), "missing": True} for r in data["review.rules"]
            }
            rules.update({r["id"]: {**r, "missing": False} for r in data["bangumi"]})
            return [
                {
                    "id": r["id"],
                    "title": r["official_title"],
                    "group": r.get("group_name"),
                    "season": r["season"],
                    "missing": r["missing"],
                    "deleted": bool(r.get("deleted")),
                }
                for r in rules.values()
            ]

    def preview(self, rid, episodes):
        with self.connect() as db:
            db.execute("BEGIN")
            data = self.read(db)
            view = self.view(data, rid)
            chosen = set(episodes)
            rows = [r for r in view["episodes"] if r["selection"] in chosen]
            if not rows or len(rows) != len(chosen):
                raise ValueError("所选集数的历史已变化，请刷新")
            _, candidates, _, keys, _ = self.scope(data, rid)
            keys = {k for k in keys if selection(k) in chosen}
            if any(
                c["key"] in keys and c["status"] in ("dispatching", "retry") for c in candidates
            ):
                raise ValueError("所选集数有结果未确认的投递，请先核对下载器任务")
            return {
                "snapshot": digest(data),
                "rid": rid,
                "episodes": [row["selection"] for row in rows],
                "summary": {**view, "episodes": rows},
            }

    def backup(self):
        directory = (
            self.review_path.parent
            / "backups"
            / f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}Z-{secrets.token_hex(4)}"
        )
        directory.mkdir(parents=True, mode=0o700)
        for source, name in [(self.host_path, "ab.sqlite3"), (self.review_path, "review.sqlite3")]:
            path = directory / name
            # Separate readers see the committed state while the writer reserves both DBs.
            with (
                closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as src,
                closing(sqlite3.connect(path)) as dest,
            ):
                src.backup(dest)
                if dest.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("数据库备份校验失败，未执行清理")
            os.chmod(path, 0o600)
        Audit().emit(
            "database.backup",
            sources=[str(self.host_path), str(self.review_path)],
            target=str(directory),
        )
        return directory

    def clear(self, receipt):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                data = self.read(db)
                if digest(data) != receipt["snapshot"]:
                    raise ValueError("记录已变化，请重新预览")
                _, candidates, torrents, keys, related = self.scope(data, receipt["rid"])
                chosen = set(receipt["episodes"])
                keys = {k for k in keys if selection(k) in chosen}
                directory = self.backup()
                # Holding every sibling rule prevents a scheduled replay from immediately
                # downloading the old group again. The user explicitly selects a version.
                for rid in related:
                    db.execute(
                        "UPDATE review.rules SET state='pending',policy=NULL,revision=revision+1 WHERE id=?",
                        (rid,),
                    )
                count = 0
                for row in torrents:
                    if row["key"] in keys and row["downloaded"]:
                        db.execute("DELETE FROM torrent WHERE id=?", (row["id"],))
                        count += 1
                for row in candidates:
                    if row["key"] in keys:
                        payload = row["data"]
                        payload.pop("sent_downloader_id", None)
                        payload.pop("sent_save_path", None)
                        db.execute(
                            "UPDATE review.candidates SET status='held',error=NULL,payload=? WHERE id=?",
                            (json.dumps(payload, ensure_ascii=False), row["id"]),
                        )
                for key in keys:
                    db.execute("DELETE FROM review.delivered WHERE episode_key=?", (key,))
                    db.execute("DELETE FROM review.reservations WHERE episode_key=?", (key,))
                db.commit()
                return {
                    "cleared_episodes": receipt["episodes"],
                    "host_records": count,
                    "backup": str(directory),
                    "held_rules": sorted(related),
                }
            except BaseException:
                db.rollback()
                raise
