"""Drydock core — workspace model, registry, and orchestration primitives."""

from dataclasses import dataclass, field


@dataclass
class WsError(Exception):
    """An error that includes what to do about it.

    Every error carries a human/LLM-readable `fix` field so the caller
    never has to guess the corrective action.
    """

    message: str
    fix: str | None = None
    context: dict = field(default_factory=dict)
    code: str | None = None

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict:
        if self.code:
            d: dict = {"error": self.code, "message": self.message}
        else:
            d = {"error": self.message}
        if self.fix:
            d["fix"] = self.fix
        if self.context:
            d["context"] = self.context
        return d
