"""Web page: setup wizard, label events in bulk, draw the pool zone, test the analysis.
All URLs in the page are relative, so it also works inside Home Assistant (add-on ingress)."""
from __future__ import annotations

import base64
import json
import re
import secrets
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from .app import SwimWatch
from .config import slug
from .i18n import LABEL_KEYS, TEXT

STATIC = Path(__file__).parent / "static"
INGRESS_IPS = {"172.30.32.2", "127.0.0.1"}

# Cameras that only stream on demand (battery cameras): how to open and close their live video.
ON_DEMAND = {
    "eufy_security": {"start": "eufy_security.start_p2p_livestream", "stop": "eufy_security.stop_p2p_livestream"},
}
MOTION_RE = re.compile(r"motion|mouvement|movement|person|personne|human|humain|people|occupancy|presence", re.I)
PERSON_RE = re.compile(r"person|personne|human|humain|people", re.I)


def make_web(sw: SwimWatch) -> FastAPI:
    api = FastAPI(title="SwimWatch", docs_url=None, redoc_url=None, openapi_url=None)

    @api.middleware("http")
    async def guard(request: Request, call_next):
        if sw.cfg.addon:
            # As an add-on, only Home Assistant's ingress proxy may talk to us (HA does the login).
            if request.client and request.client.host not in INGRESS_IPS:
                return JSONResponse({"detail": "open SwimWatch from the Home Assistant sidebar"}, 403)
        else:
            pw = sw.cfg["web"].get("password")
            if pw:
                auth = request.headers.get("authorization", "")
                ok = False
                if auth.startswith("Basic "):
                    try:
                        ok = secrets.compare_digest(base64.b64decode(auth[6:]).decode().split(":", 1)[1], pw)
                    except Exception:  # noqa: BLE001
                        ok = False
                if not ok:
                    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="SwimWatch"'})
        return await call_next(request)

    @api.get("/", response_class=HTMLResponse)
    async def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @api.get("/api/info")
    async def info():
        need = sw.need_connection
        if not need and not sw.cfg.addon and not sw.ha.connected.is_set():
            need = await sw.ha.check() == "refused"     # token revoked or deleted
        return {"need_connection": need, "ha_url": sw.ha.url, "addon": sw.cfg.addon,
                "lang": sw.lang, "texts": TEXT, "label_keys": LABEL_KEYS, "stats": sw.store.stats(),
                "ready": sw.ready.is_set(), "load_error": sw.load_error, "connected": sw.ha.connected.is_set(),
                "notify": sw.cfg["notify"], "alerts": sw.cfg["alerts"], "language": sw.cfg["language"],
                "control": sw.control.status(),
                "cameras": list(sw.cfg.cameras.values())}

    # ---------------------------------------------------------------- connection (outside the add-on)
    @api.post("/api/setup/connection")
    async def connection(body: dict = Body(...)):
        if sw.cfg.addon:
            raise HTTPException(400, "not needed in the add-on")
        # Only when SwimWatch can't use Home Assistant right now (first start, token revoked...),
        # so nobody on the network can swap the token of a working install.
        if await sw.ha.check() == "ok":
            raise HTTPException(403, "already connected")
        url = (body.get("url") or "").strip().rstrip("/")
        token = (body.get("token") or "").strip()
        if not re.match(r"^https?://[^\s/]+", url) or not token:
            raise HTTPException(400, "address and token needed")
        res = await sw.ha.check(url, token)
        if res != "ok":
            raise HTTPException(401 if res == "refused" else 502, res)
        sw.cfg.save_connection(url, token)
        sw.ha.url, sw.ha.token = url, token
        await sw.connect()
        return {"ok": True}

    # ---------------------------------------------------------------- events
    @api.get("/api/events")
    async def events(which: str = "all", limit: int = 60, before: float | None = None):
        return sw.store.list(which, min(limit, 300), before)

    def _event_file(ev_id: str, name: str) -> Path:
        ev = sw.store.get(ev_id)
        if not ev:
            raise HTTPException(404)
        p = Path(ev["dir"]) / name
        if not p.exists():
            raise HTTPException(404)
        return p

    @api.get("/api/events/{ev_id}/annotated.jpg")
    async def annotated(ev_id: str):
        return FileResponse(_event_file(ev_id, "a.jpg"), media_type="image/jpeg")

    @api.get("/api/events/{ev_id}/frame/{n}.jpg")
    async def frame(ev_id: str, n: int):
        return FileResponse(_event_file(ev_id, f"f{n}.jpg"), media_type="image/jpeg")

    @api.post("/api/events/{ev_id}/label")
    async def label(ev_id: str, body: dict = Body(...)):
        try:
            ok = await sw.set_label(ev_id, body.get("label"), "web")
        except ValueError:
            raise HTTPException(400, "bad label")
        if not ok:
            raise HTTPException(404)
        return {"ok": True}

    # ---------------------------------------------------------------- Home Assistant discovery (setup)
    async def _on_demand_map() -> dict:
        """camera entity -> integration, for cameras that need their live video opened."""
        out = {}
        for integ in ON_DEMAND:
            raw = await sw.ha.template("{{ integration_entities('%s') | select('match', 'camera\\\\.') | list | tojson }}" % integ)
            try:
                for e in json.loads(raw or "[]"):
                    out[e] = integ
            except json.JSONDecodeError:
                pass
        return out

    @api.get("/api/ha/cameras")
    async def ha_cameras():
        od = await _on_demand_map()
        cams = [{"entity": s["entity_id"], "name": s["attributes"].get("friendly_name") or s["entity_id"],
                 "state": s["state"], "on_demand": s["entity_id"] in od}
                for s in await sw.ha.states() if s["entity_id"].startswith("camera.") and s["state"] != "unavailable"]
        return sorted(cams, key=lambda c: c["name"].lower())

    @api.get("/api/ha/snapshot.jpg")
    async def ha_snapshot(entity: str):
        if not re.fullmatch(r"camera\.[a-z0-9_]+", entity):
            raise HTTPException(400)
        img = await sw.ha.camera_image(entity)
        if not img:
            raise HTTPException(502, "no picture from Home Assistant")
        return Response(img, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @api.get("/api/ha/suggest")
    async def ha_suggest(entity: str):
        """Motion / person sensors of the same device as the camera, and how to open its live video."""
        if not re.fullmatch(r"camera\.[a-z0-9_]+", entity):
            raise HTTPException(400)
        raw = await sw.ha.template("{{ device_entities(device_id('%s')) | list | tojson if device_id('%s') else '[]' }}" % (entity, entity))
        try:
            same_device = set(json.loads(raw or "[]"))
        except json.JSONDecodeError:
            same_device = set()
        states = await sw.ha.states()
        binaries = []
        for s in states:
            e = s["entity_id"]
            if not e.startswith("binary_sensor."):
                continue
            name = s["attributes"].get("friendly_name") or e
            dc = s["attributes"].get("device_class") or ""
            binaries.append({"entity": e, "name": name, "device_class": dc, "same_device": e in same_device,
                             "motion": dc in ("motion", "occupancy", "presence") or bool(MOTION_RE.search(e + " " + name)),
                             "person": bool(PERSON_RE.search(e + " " + name))})
        binaries.sort(key=lambda b: (not b["same_device"], not b["motion"], b["name"].lower()))
        mine = [b for b in binaries if b["same_device"] and b["motion"]]
        od = await _on_demand_map()
        integ = od.get(entity)
        return {"triggers": [b["entity"] for b in mine],
                "live_triggers": [b["entity"] for b in mine if b["person"]] if integ else [],
                "on_demand": bool(integ), "binary_sensors": binaries}

    @api.get("/api/ha/notify")
    async def ha_notify():
        out = []
        for d in await sw.ha.services():
            if d["domain"] == "notify":
                out += [f"notify.{s}" for s in d["services"] if s.startswith("mobile_app_")]
        return sorted(out)

    # ---------------------------------------------------------------- setup: save
    @api.post("/api/setup/camera")
    async def setup_camera(body: dict = Body(...)):
        ent = body.get("camera", "")
        if not re.fullmatch(r"camera\.[a-z0-9_]+", ent):
            raise HTTPException(400, "camera")
        name = (body.get("name") or "Pool").strip()[:40]
        cid = body.get("id") or slug(name)
        zone = [[round(float(x), 4), round(float(y), 4)] for x, y in body.get("zone", [])]
        if len(zone) < 3:
            raise HTTPException(400, "zone needs at least 3 points")
        cam = {"id": cid, "name": name, "camera": ent,
               "triggers": [e for e in body.get("triggers", []) if e.startswith("binary_sensor.")],
               "live_triggers": [], "interval_minutes": float(body.get("interval_minutes") or 0), "zone": zone,
               "live": None}
        old = sw.cfg.cameras.get(cid)
        od = await _on_demand_map()
        integ = od.get(ent)
        if old and old.get("live") and old["camera"] == ent:
            cam["live"] = old["live"]            # keep a hand-made live setup (YAML)
        elif integ and body.get("use_live", True):
            p = ON_DEMAND[integ]
            others = sorted(e for e, i in od.items() if i == integ and e != ent)
            cam["live"] = {"seconds": 8, "frames": 4,
                           "start": {"service": p["start"], "target": {"entity_id": ent}},
                           "stop": {"service": p["stop"], "target": {"entity_id": ent}},
                           "skip_if_streaming": others}
        if cam["live"]:
            cam["live_triggers"] = [e for e in body.get("live_triggers", []) if e in cam["triggers"]]
        if not cam["triggers"] and not cam["interval_minutes"]:
            cam["interval_minutes"] = 5     # no sensor at all: look every 5 minutes
        sw.cfg.save_camera(cam)
        await sw.settings_changed()
        return sw.cfg.cameras[cid]

    @api.delete("/api/setup/camera/{cid}")
    async def delete_camera(cid: str):
        sw.cfg.delete_camera(cid)
        await sw.settings_changed()
        return {"ok": True}

    @api.post("/api/setup/settings")
    async def setup_settings(body: dict = Body(...)):
        changes = {}
        if "notify" in body:
            n = dict(sw.cfg["notify"])
            n["services"] = [s for s in body["notify"].get("services", []) if s.startswith("notify.")]
            if body["notify"].get("ask") in ("always", "uncertain", "never"):
                n["ask"] = body["notify"]["ask"]
            changes["notify"] = n
        if "alerts" in body:
            a = dict(sw.cfg["alerts"])
            b = body["alerts"]
            if "services" in b:
                a["services"] = [x for x in b["services"] if x.startswith("notify.")]
            for k, lo, hi in (("reminder_minutes", 0, 1440), ("resume_quiet_minutes", 5, 240)):
                if k in b:
                    a[k] = max(lo, min(hi, int(b[k])))
            changes["alerts"] = a
        if body.get("language") in ("auto", "en", "fr"):
            changes["language"] = body["language"]
        sw.cfg.save(changes)
        await sw.settings_changed()
        return {"ok": True}

    # ---------------------------------------------------------------- pause and alerts
    @api.post("/api/control/pause")
    async def pause(body: dict = Body(default={})):
        await sw.control.pause(body.get("mode", "out"), source="web")
        return sw.control.status()

    @api.post("/api/control/resume")
    async def resume():
        await sw.control.resume("web")
        return sw.control.status()

    @api.post("/api/control/critical")
    async def critical(body: dict = Body(...)):
        sw.control.critical = bool(body.get("on"))
        sw.control._save()
        await sw.control._push_to_ha()
        return sw.control.status()

    @api.post("/api/control/test-alert")
    async def test_alert():
        if not sw.cfg["alerts"].get("services"):
            raise HTTPException(400, "no phone chosen for alerts")
        await sw.control.alert(next(iter(sw.cfg.cameras), ""), None, None, test=True)
        return {"ok": True, "critical": sw.control.critical}

    # ---------------------------------------------------------------- test
    @api.post("/api/cameras/{cid}/analyze")
    async def analyze_now(cid: str, body: dict = Body(default={})):
        cam = sw.cfg.cameras.get(cid)
        if not cam:
            raise HTTPException(404)
        img = await sw.ha.camera_image(cam["camera"])
        if not img:
            raise HTTPException(502, "no picture from Home Assistant")
        try:
            ev = await sw.process(cid, [img], "manual", {"stale": False, "live": 0}, notify=False)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc))
        if body.get("notify"):
            await sw.maybe_ask(ev, (Path(ev["dir"]) / "a.jpg").read_bytes(), True, force=True)
        return sw.store.get(ev["id"])

    return api
