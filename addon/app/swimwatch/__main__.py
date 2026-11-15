"""python -m swimwatch [--config config.yaml]"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import sys

import uvicorn

from .app import SwimWatch
from .config import Config
from .web import make_web


async def main(cfg: Config):
    sw = SwimWatch(cfg)
    web = make_web(sw)
    server = uvicorn.Server(uvicorn.Config(web, host=cfg["web"]["host"], port=int(cfg["web"]["port"]),
                                           log_level="warning", loop="none"))
    web_task = asyncio.create_task(server.serve())
    await sw.start()
    await web_task


def run():
    ap = argparse.ArgumentParser(prog="swimwatch")
    ap.add_argument("--config", default=None, help="optional YAML file (not needed as a Home Assistant add-on)")
    args = ap.parse_args()
    cfg = Config(args.config)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(cfg.data_dir / "swimwatch.log", maxBytes=2_000_000,
                                              backupCount=3, encoding="utf-8")
    sh = logging.StreamHandler(sys.stdout)
    for h in (fh, sh):
        h.setFormatter(fmt)
        root.addHandler(h)
    for noisy in ("httpx", "httpx2", "httpcore", "huggingface_hub", "open_clip", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not cfg["home_assistant"]["token"]:
        logging.error("no Home Assistant token: set HA_TOKEN in .env (next to the config file) or in the config file")
        sys.exit(1)
    asyncio.run(main(cfg))


if __name__ == "__main__":
    run()
