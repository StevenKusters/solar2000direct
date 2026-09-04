"""Service entrypoint: start the collector and everything that feeds off it."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path

from solar2000direct import addon
from solar2000direct.api import Api
from solar2000direct.collector import Collector
from solar2000direct.config import Config
from solar2000direct.control import ControlManager
from solar2000direct.history import History
from solar2000direct.homeassistant import HomeAssistantClient
from solar2000direct.mqtt import MqttPublisher
from solar2000direct.registers import CAP_P1
from solar2000direct.state import State

_LOGGER = logging.getLogger("s2d")


async def run(config: Config) -> None:
    state = State(config.array, config.meter)
    profiles_path = str(Path(config.history.path).parent / "profiles.json")
    control = ControlManager(config.control, state, profiles_path)
    collector = Collector(config, state, on_device=control.attach)
    publisher = MqttPublisher(config, state)
    home_assistant = HomeAssistantClient(config.home_assistant, state)
    # A P1 feed is configuration, not hardware, so it is recorded here rather than
    # detected on connect. Without it the cross-check entities were created on every
    # installation and never received a value.
    if config.home_assistant.enabled and config.home_assistant.net_power_entity:
        state.site_capabilities = state.site_capabilities | {CAP_P1}
    history = History(config.history, state, sample_interval=config.polling.live_interval)
    api = Api(config, state, history, control)

    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows has no add_signal_handler
            loop.add_signal_handler(sig, stopping.set)

    services = (collector, publisher, home_assistant, history)
    tasks = [
        asyncio.create_task(collector.run(), name="collector"),
        asyncio.create_task(publisher.run(), name="mqtt"),
        asyncio.create_task(home_assistant.run(), name="home-assistant"),
        asyncio.create_task(history.run(), name="history"),
    ]
    await api.start()
    try:
        await stopping.wait()
    finally:
        await api.stop()
        _LOGGER.info("Shutting down")
        for service in services:
            service.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> int:
    # Under the Supervisor, translate add-on options and discovered services into the
    # same environment variables the container build uses, so there is one configuration
    # path rather than two that can drift apart.
    if addon.is_addon():
        logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
        _LOGGER.info("Running as a Home Assistant add-on")
        addon.apply_environment()

    try:
        config = Config.from_env()
    except ValueError as err:
        print(f"Configuration error: {err}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
