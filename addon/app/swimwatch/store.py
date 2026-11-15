"""Events and labels in SQLite, pictures in data/events/<day>/<id>/."""
from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, camera TEXT, ts REAL, trigger TEXT, n_frames INTEGER, rep INTEGER,
  in_water INTEGER, persons_near INTEGER, animals INTEGER,
  p_zs REAL, prior REAL, p_model REAL, trust REAL, p REAL, swimming INTEGER, uncertain INTEGER,
  label TEXT, labeled_ts REAL, labeled_by TEXT, notified INTEGER DEFAULT 0,
  details TEXT, emb BLOB, dir TEXT
);
CREATE INDEX IF NOT EXISTS ev_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ev_label ON events(label);
"""


class Store:
    def __init__(self, data_dir: Path):
        self.dir = data_dir / "events"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(data_dir / "swimwatch.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()
        self._cache = None

    def event_dir(self, ev_id: str, ts: float) -> Path:
        d = self.dir / time.strftime("%Y-%m-%d", time.localtime(ts)) / ev_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def add(self, ev: dict):
        cols = ["id", "camera", "ts", "trigger", "n_frames", "rep", "in_water", "persons_near", "animals",
                "p_zs", "prior", "p_model", "trust", "p", "swimming", "uncertain", "details", "emb", "dir"]
        row = dict(ev)
        row["details"] = json.dumps(ev["details"])
        row["emb"] = ev["emb"].astype(np.float32).tobytes()
        with self.lock:
            self.db.execute(f"INSERT OR REPLACE INTO events ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [row[c] for c in cols])
            self.db.commit()

    def set_label(self, ev_id: str, label: str | None, by: str):
        with self.lock:
            cur = self.db.execute("UPDATE events SET label=?, labeled_ts=?, labeled_by=? WHERE id=?",
                                  (label, time.time() if label else None, by if label else None, ev_id))
            self.db.commit()
            self._cache = None
            return cur.rowcount > 0

    def set_notified(self, ev_id: str):
        with self.lock:
            self.db.execute("UPDATE events SET notified=1 WHERE id=?", (ev_id,))
            self.db.commit()

    def labeled(self, camera: str):
        """Vectors and labels of all labeled events of a camera (cached until a label changes)."""
        with self.lock:
            if self._cache is None:
                rows = self.db.execute("SELECT camera, label, emb FROM events WHERE label IS NOT NULL").fetchall()
                cache = {}
                for r in rows:
                    cache.setdefault(r["camera"], ([], []))
                    cache[r["camera"]][0].append(np.frombuffer(r["emb"], dtype=np.float32))
                    cache[r["camera"]][1].append(r["label"])
                self._cache = cache
            embs, labels = self._cache.get(camera, ([], []))
        return (np.stack(embs) if embs else None), labels

    def get(self, ev_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM events WHERE id=?", (ev_id,)).fetchone()
        return self._row(r) if r else None

    def list(self, which: str = "all", limit: int = 60, before: float | None = None, camera: str | None = None):
        q, args = "SELECT * FROM events WHERE 1=1", []
        if which == "todo":
            q += " AND label IS NULL"
        elif which == "labeled":
            q += " AND label IS NOT NULL"
        elif which == "swim":
            q += " AND swimming=1"
        if camera:
            q += " AND camera=?"
            args.append(camera)
        if before:
            q += " AND ts<?"
            args.append(before)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        return [self._row(r) for r in self.db.execute(q, args).fetchall()]

    def stats(self):
        r = self.db.execute("SELECT COUNT(*) n, SUM(label IS NULL) todo, SUM(swimming) swim FROM events").fetchone()
        by = dict(self.db.execute("SELECT label, COUNT(*) FROM events WHERE label IS NOT NULL GROUP BY label").fetchall())
        # how often the guess (swimming yes/no) matched the label
        ok = self.db.execute("SELECT COUNT(*), SUM(swimming = (label='swim')) FROM events WHERE label IS NOT NULL").fetchone()
        return {"events": r["n"] or 0, "todo": r["todo"] or 0, "swim": r["swim"] or 0, "labels": by,
                "checked": ok[0] or 0, "correct": ok[1] or 0}

    def cleanup(self, keep_days: int):
        """Removes old unlabeled events. Labeled ones are kept: they are the training data."""
        limit = time.time() - keep_days * 86400
        with self.lock:
            rows = self.db.execute("SELECT id, dir FROM events WHERE ts<? AND label IS NULL", (limit,)).fetchall()
            for r in rows:
                shutil.rmtree(r["dir"], ignore_errors=True)
            self.db.execute("DELETE FROM events WHERE ts<? AND label IS NULL", (limit,))
            self.db.commit()
        return len(rows)

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d.pop("emb", None)
        d["details"] = json.loads(d["details"] or "{}")
        return d
