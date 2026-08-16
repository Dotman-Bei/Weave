"""Episodic layer -- "what happened". Immutable record of every conversation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..util import count_tokens, new_id, now_iso


@dataclass
class Utterance:
    text: str
    id: str = field(default_factory=lambda: new_id("utt"))
    timestamp: str = field(default_factory=now_iso)
    utterance_number: int = 0

    def to_props(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "timestamp": self.timestamp,
            "utterance_number": self.utterance_number,
            "token_count": count_tokens(self.text),
        }


@dataclass
class Turn:
    speaker: str
    text: str
    id: str = field(default_factory=lambda: new_id("turn"))
    turn_number: int = 0
    timestamp: str = field(default_factory=now_iso)
    session_id: str = ""

    @property
    def token_count(self) -> int:
        return count_tokens(self.text)

    def to_props(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "turn_number": self.turn_number,
            "timestamp": self.timestamp,
            "speaker": self.speaker,
            "text": self.text,
            "token_count": self.token_count,
            "session_id": self.session_id,
        }


@dataclass
class Session:
    id: str = field(default_factory=lambda: new_id("sess"))
    user_id: str = "default"
    turns: list[Turn] = field(default_factory=list)
    start_time: str = field(default_factory=now_iso)
    end_time: str = field(default_factory=now_iso)
    session_summary: str = ""
    session_number: int = 0

    @property
    def total_turns(self) -> int:
        return len(self.turns)

    @property
    def total_tokens(self) -> int:
        return sum(turn.token_count for turn in self.turns)

    def to_props(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "total_turns": self.total_turns,
            "total_tokens": self.total_tokens,
            "session_summary": self.session_summary,
            "session_number": self.session_number,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Session":
        """Build a Session from the /ingest request shape."""
        from ..util import to_iso

        session_id = payload.get("session_id") or new_id("sess")
        start = to_iso(payload.get("timestamp") or payload.get("start_time")) or now_iso()
        session = cls(
            id=session_id,
            user_id=payload.get("user_id") or "default",
            start_time=start,
            end_time=to_iso(payload.get("end_time")) or start,
            session_summary=payload.get("session_summary", ""),
            session_number=int(payload.get("session_number") or 0),
        )
        for index, raw in enumerate(payload.get("turns", [])):
            if isinstance(raw, str):
                raw = {"speaker": "user", "text": raw}
            session.turns.append(
                Turn(
                    speaker=(raw.get("speaker") or raw.get("role") or "user").lower(),
                    text=raw.get("text") or raw.get("content") or "",
                    turn_number=index,
                    timestamp=to_iso(raw.get("timestamp")) or start,
                    session_id=session_id,
                )
            )
        if session.turns:
            session.end_time = session.turns[-1].timestamp
        return session
