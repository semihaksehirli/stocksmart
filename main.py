"""Run StockSmart: REST API at /api and the web front end at /.

    python -m app.seed      # first time only: demo data
    python main.py          # http://localhost:7860  (API docs: /api/docs)

Connected POS systems are checked for new sales every POS_SYNC_MINUTES (default 5; 0 turns it off).
"""
import logging
import os
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import app as api_app
from app.db import init_db
from app.pos.sync import sync_all

FRONTEND = Path(__file__).parent / "frontend"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

init_db()
root = FastAPI(title="StockSmart", docs_url=None, redoc_url=None)
root.mount("/api", api_app)
root.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")


def _pos_poller(minutes: float) -> None:
    stop = threading.Event()
    while not stop.wait(minutes * 60):
        try:
            sync_all()
        except Exception:  # never let the poller die
            logging.getLogger("stocksmart.pos").exception("POS auto sync failed")


if __name__ == "__main__":
    minutes = float(os.getenv("POS_SYNC_MINUTES", "5"))
    if minutes > 0:
        threading.Thread(target=_pos_poller, args=(minutes,), daemon=True, name="pos-poller").start()
    uvicorn.run(root, host="0.0.0.0", port=int(os.getenv("PORT", "7860")))
