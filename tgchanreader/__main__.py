from __future__ import annotations

import asyncio
import logging
import os
import signal

from . import __version__
from .config import load_config
from .service import Service

log = logging.getLogger("tgchanreader")


async def main() -> None:
    cfg = load_config()
    log.info(
        "tgchanreader %s · источник: %s · LLM: %s (%s) · дайджест: %s %s",
        __version__,
        cfg.effective_source,
        cfg.llm.provider if cfg.llm.enabled else "выключен",
        cfg.llm.model,
        "по умолчанию " + ", ".join(f"{h:02d}:00" for h in cfg.default_schedule),
        cfg.timezone,
    )
    service = Service(cfg)
    try:
        await service.start()
        await _serve(service)
    finally:
        await service.close()


async def _serve(service: Service) -> None:
    cfg = service.cfg
    tasks: list[asyncio.Task] = []
    runner = None
    bot = None
    if cfg.web_enabled:
        from .web import start_web

        runner = await start_web(service)
    if cfg.bot_token:
        from .bot import TelegramBot

        bot = TelegramBot(service)
        if not cfg.admin_ids:
            log.warning("ADMIN_IDS не задан — напишите боту /start, он подскажет ваш ID")
        tasks.append(asyncio.create_task(bot.run(), name="bot"))
    else:
        log.warning("BOT_TOKEN не задан — работаем только как веб/RSS-сервис")
        if not cfg.web_enabled:
            raise SystemExit("Нет ни бота, ни веб-интерфейса: задайте BOT_TOKEN или WEB_ENABLED=true")

    service.run_background()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    waiters = [asyncio.create_task(stop.wait(), name="stop")] + tasks
    try:
        done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled() and task.exception():
                log.error("Задача %s упала: %s", task.get_name(), task.exception())
    finally:
        log.info("Остановка…")
        for task in waiters:
            task.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        if bot:
            await bot.close()
        if runner:
            await runner.cleanup()


def run() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    asyncio.run(main())


if __name__ == "__main__":
    run()
