"""Settings, in this order (later wins):
built-in defaults < optional YAML file (--config) < data/settings.json (written by the setup page).

As a Home Assistant add-on (SUPERVISOR_TOKEN set) nothing needs to be configured by hand:
HA is reached through the Supervisor and everything else is chosen in the setup page.
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

import yaml

DEFAULTS = {
    "home_assistant": {"url": "http://homeassistant.local:8123", "token": ""},
    "web": {"host": "0.0.0.0", "port": 8765, "public_url": "", "password": ""},
    "data_dir": "data",
    "models_dir": "",           # default: <data_dir>/models
    "language": "auto",         # auto (from Home Assistant) | en | fr
    "threads": 0,               # 0 = half of the CPU cores (leave room for Home Assistant)
    "detector": {"model": "", "confidence": 0.35},
    "notify": {"services": [], "ask": "uncertain", "min_gap_minutes": 3, "media_dir": "swimwatch"},
    "sensor": {"hold_minutes": 10},
    # swim alerts: phones, reminder while paused, and how long without movement means "we got out"
    "alerts": {"services": [], "reminder_minutes": 60, "resume_quiet_minutes": 20},
    "keep_days": 30,
    "cameras": [],
}

CAMERA_DEFAULTS = {
    "name": "Pool",
    "triggers": [],          # entities that start an analysis when they turn on
    "live_triggers": [],     # these also open the live video for a few seconds
    "interval_minutes": 0,   # also analyze every N minutes (for cameras with no motion sensor)
    "min_interval": 20,
    "capture": {"wait_new_image": 15, "poll": 1.0},
    "live": None,
    "zone": [],
}

# Settings the setup page may change (anything else stays in the YAML file).
EDITABLE = {"language", "notify", "alerts", "sensor", "cameras", "detector", "threads", "keep_days"}


def merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def slug(text: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "pool"


class Config:
    def __init__(self, path: str | Path | None = None):
        self.addon = bool(os.environ.get("SUPERVISOR_TOKEN"))
        raw = {}
        self.root = Path.cwd()
        if path and Path(path).exists():
            self.root = Path(path).resolve().parent
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        self._load_dotenv(self.root / ".env")
        self.file_data = merge(DEFAULTS, raw)
        if self.addon:
            self.file_data["home_assistant"] = {"url": "http://supervisor/core", "token": os.environ["SUPERVISOR_TOKEN"]}
        if os.environ.get("SWIMWATCH_DATA"):          # set to /data in the Docker image
            self.file_data["data_dir"] = os.environ["SWIMWATCH_DATA"]
        ha = self.file_data["home_assistant"]
        ha["token"] = ha.get("token") or os.environ.get("HA_TOKEN", "")
        ha["url"] = os.environ.get("HA_URL", ha["url"]).rstrip("/")
        self.data_dir = (self.root / self.file_data["data_dir"]).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        md = self.file_data["models_dir"]
        self.models_dir = (self.root / md).resolve() if md else self.data_dir / "models"
        self.models_dir.mkdir(parents=True, exist_ok=True)
        if not self.file_data.get("threads"):
            self.file_data["threads"] = max(1, (os.cpu_count() or 2) // 2)
        self.settings_file = self.data_dir / "settings.json"
        # Address and token typed in the web page (wins over the YAML file and environment).
        self.secrets_file = self.data_dir / "connection.json"
        if self.secrets_file.exists() and not self.addon:
            sec = json.loads(self.secrets_file.read_text(encoding="utf-8"))
            ha["url"] = (sec.get("url") or ha["url"]).rstrip("/")
            ha["token"] = sec.get("token") or ha["token"]
        self.reload()

    def save_connection(self, url: str, token: str):
        tmp = self.secrets_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"url": url, "token": token}), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.secrets_file)
        self.file_data["home_assistant"] = {"url": url, "token": token}
        self.reload()

    def reload(self):
        saved = {}
        if self.settings_file.exists():
            saved = json.loads(self.settings_file.read_text(encoding="utf-8"))
        self.data = merge(self.file_data, {k: v for k, v in saved.items() if k in EDITABLE})
        self.cameras = {}
        for cam in self.data["cameras"]:
            c = merge(CAMERA_DEFAULTS, cam)
            c["id"] = c.get("id") or slug(c["name"])
            self.cameras[c["id"]] = c

    def save(self, changes: dict):
        """Saves settings from the setup page to data/settings.json."""
        saved = {}
        if self.settings_file.exists():
            saved = json.loads(self.settings_file.read_text(encoding="utf-8"))
        for k, v in changes.items():
            if k in EDITABLE:
                saved[k] = v
        tmp = self.settings_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(saved, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.settings_file)
        self.reload()

    def save_camera(self, cam: dict):
        cams = [c for c in self.data["cameras"] if (c.get("id") or slug(c.get("name", ""))) != cam["id"]]
        cams.append(cam)
        self.save({"cameras": cams})

    def delete_camera(self, cam_id: str):
        self.save({"cameras": [c for c in self.data["cameras"] if (c.get("id") or slug(c.get("name", ""))) != cam_id]})

    @staticmethod
    def _load_dotenv(env: Path):
        if not env.exists():
            return
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    def __getitem__(self, key):
        return self.data[key]
