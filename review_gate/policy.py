"""Host-independent policy. Literal terms avoid regex ambiguity and ReDoS."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field


@dataclass
class Policy:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    mode: str = "future"
    from_episode: float = 1
    cutoff: float = 0
    version_id: str | None = None

    def validate(self):
        if self.mode not in {"future", "backlog"}:
            raise ValueError("mode must be future or backlog")
        if not self.include or len(self.include) > 32 or len(self.exclude) > 32:
            raise ValueError("至少填写一个必须包含的版本条件，最多 32 项")
        for term in self.include + self.exclude:
            if not isinstance(term, str) or not term.strip() or len(term) > 200:
                raise ValueError("关键词必须为 1–200 字符的非空文本")
        if not 0 <= self.from_episode <= 100000:
            raise ValueError("起始集数不合法")
        return self

    def reason(self, candidate: dict) -> str | None:
        if self.version_id:
            from .versions import variants

            matches = variants([candidate])
            if not matches or matches[0]["id"] != self.version_id:
                return "不是所选下载版本"
        name = candidate["name"].casefold()
        if not all(t.casefold() in name for t in self.include):
            return "缺少必须包含的关键词"
        if any(t.casefold() in name for t in self.exclude):
            return "命中排除关键词"
        if not candidate.get("episode_key"):
            return "不能可靠识别单集；保持等待"
        if candidate["episode"] < self.from_episode:
            return "早于起始集数"
        if self.mode == "future" and candidate["first_seen"] <= self.cutoff:
            return "批准前已出现；仅追后续发布"
        return None

    def dump(self):
        return asdict(self)


def fingerprint(rule: dict) -> str:
    # Rule IDs can be reused after a host DB restore. Never inherit approval then.
    fields = (
        "official_title",
        "title_raw",
        "season",
        "episode_type",
        "group_name",
        "episode_offset",
        "season_offset",
    )
    return hashlib.sha256(
        json.dumps([rule.get(k) for k in fields], ensure_ascii=False).encode()
    ).hexdigest()


def candidate_id(rule_id: int, url: str) -> str:
    return hashlib.sha256(f"{rule_id}\0{url}".encode()).hexdigest()
