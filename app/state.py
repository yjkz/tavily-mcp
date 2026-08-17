"""Shared application state wired during startup."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .db import Database
from .pool import KeyPool
from .tavily import TavilyClient


@dataclass
class AppState:
    config: Config
    db: Database
    pool: KeyPool
    tavily: TavilyClient


_state: AppState | None = None


def set_state(state: AppState) -> None:
    global _state
    _state = state


def get_state() -> AppState:
    if _state is None:
        raise RuntimeError("AppState not initialized")
    return _state
