from __future__ import annotations

from ..config import Config
from .base import ChannelInfo, Source, SourceError
from .web import WebSource

__all__ = ["ChannelInfo", "Source", "SourceError", "WebSource", "build_source"]


def build_source(cfg: Config) -> Source:
    if cfg.effective_source == "telethon":
        from .telethon_src import CombinedSource, TelethonSource

        return CombinedSource(
            TelethonSource(cfg.tg_api_id, cfg.tg_api_hash, cfg.tg_session),
            WebSource(),
        )
    return WebSource()
