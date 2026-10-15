"""Small transport envelope, independent of numerical dependencies."""
import json
from dataclasses import dataclass
from uuid import UUID


class PoisonMessage(Exception):
    pass


@dataclass(frozen=True)
class Envelope:
    event_id: str
    job_id: str

    def encode(self) -> bytes:
        return json.dumps({"schema": 1, "kind": "analysis.ready", "event_id": self.event_id, "job_id": self.job_id}).encode()

    @classmethod
    def decode(cls, body: bytes) -> "Envelope":
        try:
            if len(body) > 1024:
                raise ValueError()
            value = json.loads(body)
            if value.get("schema") != 1 or value.get("kind") != "analysis.ready":
                raise ValueError()
            return cls(str(UUID(value["event_id"])), str(UUID(value["job_id"])))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise PoisonMessage("INVALID_ENVELOPE") from None
