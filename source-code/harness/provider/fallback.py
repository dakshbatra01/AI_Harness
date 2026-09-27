"""Bounded failover among credentials for the same model."""
from __future__ import annotations

from harness.provider.base import ContextOverflow, Provider, ProviderError, ToolsUnsupported


class FallbackProvider(Provider):
    name = "fallback"

    def __init__(self, model: str, cfg: dict, clients: list[Provider]) -> None:
        super().__init__(model, cfg)
        self.clients = clients
        self.current = 0
        self.rejected: set[int] = set()

    def chat(self, system: str, messages: list[dict], tools: list[dict] | None):
        last_error: ProviderError | None = None
        for offset in range(len(self.clients)):
            index = (self.current + offset) % len(self.clients)
            if index in self.rejected:
                continue
            try:
                turn = self.clients[index].chat(system, messages, tools)
            except (ContextOverflow, ToolsUnsupported):
                raise
            except ProviderError as error:
                last_error = error
                if error.status in (401, 403):
                    self.rejected.add(index)
                continue
            self.current = index
            return turn
        if len(self.rejected) == len(self.clients):
            raise ProviderError("all configured model credentials were rejected", fatal=True, status=401) from None
        raise ProviderError("all configured model endpoints are unavailable", status=getattr(last_error, "status", None)) from None
