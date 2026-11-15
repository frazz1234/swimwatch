"""The service: listens to Home Assistant, grabs pictures, analyzes them, reports and asks for labels."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import os
import time

from .analyze import Analyzer
from .config import Config
from .control import Control
from .ha import HomeAssistant
from .i18n import LABEL_KEYS, t
from .store import Store

log = logging.getLogger("swimwatch")
ACTION = "SWIMWATCH"


class CamState:
    def __init__(self):
        self.busy = False
        self.want_live = False
        self.last_start = 0.0
        self.last_periodic = time.time()


class SwimWatch:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ha = HomeAssistant(cfg["home_assistant"]["url"], cfg["home_assistant"]["token"])
        self.store = Store(cfg.data_dir)
        self.analyzer: Analyzer | None = None
        self.state: dict[str, CamState] = {}
        self.last_notify = 0.0
        self.ready = asyncio.Event()
        self.lang = "en"
        self.ingress_path = None
        self.load_error = None
        self.control = Control(self)

    # ------------------------------------------------------------ start
    async def start(self):
        await self.ha.start()
        await self._set_language()
        if self.cfg.addon:
            await self._find_ingress()
        self._sync_state()
        await self.control.ensure_helpers()
        asyncio.create_task(self.ha.run_websocket(self.on_trigger, {"mobile_app_notification_action": self.on_action},
                                                  self.control.on_state))
        asyncio.create_task(self._housekeeping())
        loop = asyncio.get_running_loop()
        try:
            self.analyzer = await loop.run_in_executor(
                None, lambda: Analyzer(self.cfg.models_dir, self.cfg["detector"], self.cfg["threads"]))
        except Exception as exc:  # noqa: BLE001
            self.load_error = str(exc)
            log.exception("could not load the models")
            return
        self.ready.set()

    async def _set_language(self):
        lang = self.cfg["language"]
        if lang == "auto":
            lang = ((await self.ha.config()).get("language") or "en").split("-")[0]
        self.lang = lang if lang in ("en", "fr") else "en"

    async def _find_ingress(self):
        """Path of the add-on page inside HA, opened when a notification is tapped."""
        try:
            async with self.ha.session.get("http://supervisor/addons/self/info",
                                           headers={"Authorization": f"Bearer {os.environ['SUPERVISOR_TOKEN']}"}) as r:
                info = (await r.json()).get("data", {})
            if info.get("ingress"):
                self.ingress_path = f"/hassio/ingress/{info['slug']}"
        except Exception as exc:  # noqa: BLE001
            log.debug("no ingress info: %s", exc)

    def _sync_state(self):
        for cid in self.cfg.cameras:
            self.state.setdefault(cid, CamState())
        self.ha.trigger_entities = sorted({e for c in self.cfg.cameras.values() for e in c["triggers"] + c["live_triggers"]})

    def link(self, ev_id: str = "") -> str | None:
        """Link opened when a notification is tapped (web page, or the add-on page inside HA)."""
        base = self.cfg["web"].get("public_url") or self.ingress_path
        return f"{base.rstrip('/')}/#{ev_id}" if base else None

    async def media_image(self, ev: dict) -> str | None:
        """Puts the event's annotated picture in HA media (for notifications)."""
        from pathlib import Path
        f = Path(ev["dir"]) / "a.jpg"
        if not f.exists():
            return None
        return await self.ha.upload_media(self.cfg["notify"].get("media_dir", "swimwatch"), f"{ev['id']}.jpg", f.read_bytes())

    async def settings_changed(self):
        """Called after the setup page saved something."""
        old_lang = self.lang
        await self._set_language()
        self._sync_state()
        if self.lang != old_lang or not self.control.helpers_ok:
            await self.control.ensure_helpers()
        await self.ha.resubscribe()
        for cid in self.cfg.cameras:
            await self.refresh_sensors(cid)

    async def _housekeeping(self):
        n = 0
        while True:
            for cid, cam in list(self.cfg.cameras.items()):
                st = self.state.get(cid)
                if not st:
                    continue
                every = float(cam.get("interval_minutes") or 0) * 60
                if every and time.time() - st.last_periodic >= every and not st.busy:
                    st.last_periodic = time.time()
                    st.busy, st.last_start = True, time.time()
                    asyncio.create_task(self._run_event(cid, "interval"))
                if n % 6 == 0:
                    try:
                        await self.refresh_sensors(cid, can_alert=False)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("sensor refresh failed: %s", exc)
            try:
                await self.control.tick()
                if not self.control.helpers_ok and n % 30 == 29:
                    await self.control.ensure_helpers()
                    await self.ha.resubscribe()
            except Exception as exc:  # noqa: BLE001
                log.warning("control tick failed: %s", exc)
            if n % 2160 == 0:
                removed = self.store.cleanup(self.cfg["keep_days"])
                if removed:
                    log.info("removed %d old unlabeled events", removed)
            n += 1
            await asyncio.sleep(10)

    # ------------------------------------------------------------ triggers
    async def on_trigger(self, entity_id: str):
        for cid, cam in self.cfg.cameras.items():
            is_live = entity_id in cam["live_triggers"]
            if entity_id not in cam["triggers"] and not is_live:
                continue
            st = self.state[cid]
            self.control.activity()
            if is_live:
                st.want_live = True
            if st.busy:
                continue
            if time.time() - st.last_start < cam["min_interval"]:
                log.debug("%s: %s ignored (too soon)", cid, entity_id)
                continue
            st.busy, st.last_start = True, time.time()
            asyncio.create_task(self._run_event(cid, entity_id))

    async def _run_event(self, cid: str, trigger: str):
        st = self.state[cid]
        try:
            if not self.ready.is_set():
                log.info("%s: %s ignored, models not loaded yet", cid, trigger)
                return
            log.info("%s: triggered by %s", cid, trigger)
            frames, info = await self.capture(cid)
            if frames:
                await self.process(cid, frames, trigger, info)
            else:
                log.warning("%s: no picture", cid)
        except Exception:  # noqa: BLE001
            log.exception("%s: event failed", cid)
        finally:
            st.busy, st.want_live = False, False

    # ------------------------------------------------------------ capture
    async def capture(self, cid: str):
        cam, st = self.cfg.cameras[cid], self.state[cid]
        ent, cap = cam["camera"], cam["capture"]
        first = await self.ha.camera_image(ent)
        seen = {hashlib.md5(first).hexdigest()} if first else set()
        frames, info = [], {"stale": False, "live": 0}
        t0 = time.time()
        got = None
        while time.time() - t0 < cap["wait_new_image"]:
            if st.want_live and cam.get("live") and info["live"] == 0:
                live = await self.live_grab(cid)
                info["live"] = len(live) or -1
                frames += live
                break
            if got and time.time() - got > 3:  # new picture in; a person trigger had 3 s to come
                break
            await asyncio.sleep(cap["poll"])
            img = await self.ha.camera_image(ent)
            if img:
                h = hashlib.md5(img).hexdigest()
                if h not in seen:
                    seen.add(h)
                    frames.append(img)
                    got = got or time.time()
        if not frames and first:
            frames, info["stale"] = [first], True  # the camera may have sent its picture before the trigger
        return frames[:6], info

    async def live_grab(self, cid: str) -> list[bytes]:
        cam = self.cfg.cameras[cid]
        live, ent = cam["live"], cam["camera"]
        for other in live.get("skip_if_streaming", []):
            s = await self.ha.get_state(other)
            if s and s["state"] == "streaming":
                log.info("%s: no live grab, %s is streaming (someone is watching)", cid, other)
                return []
        s = await self.ha.get_state(ent)
        already = bool(s and s["state"] == "streaming")
        if not already:
            start = live["start"]
            await self.ha.call_service(start["service"], start.get("data"), start.get("target"))
        for _ in range(int(live.get("start_timeout", 15) * 2)):
            s = await self.ha.get_state(ent)
            if s and s["state"] == "streaming":
                break
            await asyncio.sleep(0.5)
        else:
            log.warning("%s: live did not start", cid)
            return []
        await asyncio.sleep(live.get("settle", 2))
        frames, n = [], int(live.get("frames", 4))
        gap = float(live.get("seconds", 8)) / n
        for _ in range(n):
            t0 = time.time()
            img = await self.ha.camera_image(ent)
            if img:
                frames.append(img)
            await asyncio.sleep(max(0, gap - (time.time() - t0)))
        if not already and live.get("stop"):
            stop = live["stop"]
            await self.ha.call_service(stop["service"], stop.get("data"), stop.get("target"))
        log.info("%s: live grab %d frames", cid, len(frames))
        return frames

    # ------------------------------------------------------------ analysis
    async def process(self, cid: str, frames: list[bytes], trigger: str, info: dict, notify: bool = True) -> dict:
        if not self.ready.is_set():
            raise RuntimeError(self.load_error or "models are still loading")
        cam = self.cfg.cameras[cid]
        zone = cam["zone"]
        loop = asyncio.get_running_loop()
        results = []
        for f in frames:
            results.append(await loop.run_in_executor(None, self.analyzer.analyze, f, zone))
        rep = max(range(len(results)), key=lambda i: (results[i]["prior"], len(results[i]["dets"])))
        r = results[rep]
        embs, labels = self.store.labeled(cid)
        p_model, trust = self.analyzer.knn(r["emb"], embs, labels)
        p = r["prior"] if p_model is None else (1 - trust) * r["prior"] + trust * p_model
        in_water = max(x["in_water"] for x in results)
        near = max(x["persons_near"] for x in results)
        animals = max(x["animals"] for x in results)
        seen_something = bool(in_water or near or animals)
        uncertain = 0.25 <= p <= 0.75 or (seen_something and trust < 0.5)
        ts = time.time()
        ev_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts)) + f"-{cid}"
        d = self.store.event_dir(ev_id, ts)
        for i, f in enumerate(frames):
            (d / f"f{i}.jpg").write_bytes(f)
        annotated = self.analyzer.annotate(frames[rep], zone, r["dets"], t(self.lang)["kinds"])
        (d / "a.jpg").write_bytes(annotated)
        ev = {
            "id": ev_id, "camera": cid, "ts": ts, "trigger": trigger, "n_frames": len(frames), "rep": rep,
            "in_water": in_water, "persons_near": near, "animals": animals, "p_zs": r["p_zs"],
            "prior": r["prior"], "p_model": p_model, "trust": trust, "p": round(p, 3),
            "swimming": int(p >= 0.5), "uncertain": int(uncertain), "emb": r["emb"], "dir": str(d),
            "details": {"info": info, "zone": zone, "frames": [
                {k: v for k, v in x.items() if k != "emb"} for x in results]},
        }
        self.store.add(ev)
        if in_water or near:
            self.control.activity()
        log.info("%s: %s p=%.2f (prior %.2f, model %s, trust %.2f) water=%d near=%d animals=%d frames=%d%s",
                 cid, ev_id, p, r["prior"], p_model, trust, in_water, near, animals, len(frames),
                 " stale" if info.get("stale") else "")
        await self.refresh_sensors(cid, can_alert=True)
        await self.ha.fire_event("swimwatch_analysis", {
            "camera": cid, "event_id": ev_id, "swimming": bool(ev["swimming"]), "probability": ev["p"],
            "persons_in_water": in_water, "persons_near": near, "animals": animals, "uncertain": bool(uncertain)})
        if notify:
            await self.maybe_ask(ev, annotated, seen_something)
        return ev

    # ------------------------------------------------------------ asking for labels
    async def maybe_ask(self, ev: dict, annotated: bytes, seen_something: bool, force: bool = False):
        n = self.cfg["notify"]
        mode = n.get("ask", "uncertain")
        if not n.get("services"):
            return
        if self.control.paused and not force:
            return  # people are swimming on purpose: no questions now
        if not force:
            if mode == "never" or (mode == "uncertain" and not ev["uncertain"]):
                return
            if mode == "always" and not (seen_something or ev["p"] >= 0.25):
                return
            if time.time() - self.last_notify < n.get("min_gap_minutes", 3) * 60:
                log.info("%s: not asking, last question was less than %s min ago", ev["id"], n.get("min_gap_minutes"))
                return
        self.last_notify = time.time()
        tx = t(self.lang)
        image = await self.ha.upload_media(n.get("media_dir", "swimwatch"), f"{ev['id']}.jpg", annotated)
        cam = self.cfg.cameras[ev["camera"]]
        bits = []
        if ev["in_water"]:
            bits.append(tx["in_water"].format(n=ev["in_water"]))
        if ev["persons_near"]:
            bits.append(tx["near"].format(n=ev["persons_near"]))
        if ev["animals"]:
            bits.append(tx["animal"])
        msg = tx["ask_msg"].format(time=time.strftime("%H:%M", time.localtime(ev["ts"])),
                                   what=", ".join(bits) or tx["unclear"], p=f"{ev['p']:.0%}")
        actions = [{"action": f"{ACTION}:{k}:{ev['id']}", "title": tx["labels"][k]} for k in LABEL_KEYS]
        data = {"tag": f"swimwatch-{ev['id']}", "group": "swimwatch", "actions": actions}
        if image:
            data["image"] = image
        link = self.link(ev["id"])
        if link:
            data["url"] = data["clickAction"] = link
        for svc in n["services"]:
            await self.ha.call_service(svc, {"title": tx["ask_title"].format(name=cam["name"]), "message": msg, "data": data})
        self.store.set_notified(ev["id"])
        log.info("%s: asked for a label", ev["id"])

    async def on_action(self, data: dict):
        action = data.get("action", "")
        if await self.control.on_action(action):
            return
        if not action.startswith(ACTION + ":"):
            return
        _, label, ev_id = action.split(":", 2)
        if label in LABEL_KEYS:
            await self.set_label(ev_id, label, "phone")

    async def set_label(self, ev_id: str, label: str | None, by: str = "web"):
        if label is not None and label not in LABEL_KEYS:
            raise ValueError(label)
        ok = self.store.set_label(ev_id, label, by)
        if ok:
            log.info("%s labeled %s (%s)", ev_id, label, by)
            ev = self.store.get(ev_id)
            if ev["camera"] in self.cfg.cameras:
                await self.refresh_sensors(ev["camera"])
        return ok

    # ------------------------------------------------------------ Home Assistant entities
    async def refresh_sensors(self, cid: str, can_alert: bool = False):
        cam = self.cfg.cameras.get(cid)
        if not cam:
            return
        tx = t(self.lang)
        hold = self.cfg["sensor"]["hold_minutes"] * 60
        recent = self.store.list("all", limit=50, camera=cid)
        last = recent[0] if recent else None
        swim_ev = None
        for ev in recent:
            if time.time() - ev["ts"] > hold:
                break
            if (ev["label"] == "swim") if ev["label"] else bool(ev["swimming"]):
                swim_ev = ev
                break
        on = swim_ev is not None
        media = self.cfg["notify"].get("media_dir", "swimwatch")
        await self.ha.set_state(f"binary_sensor.{cid}_swimming", "on" if on else "off", {
            "friendly_name": tx["ent_swim"].format(name=cam["name"]), "device_class": "occupancy",
            "icon": "mdi:swim" if on else "mdi:pool",
            "probability": last["p"] if last else None,
            "last_event": last["id"] if last else None,
            "last_label": last["label"] if last else None,
            "swim_event": swim_ev["id"] if swim_ev else None,
            "image": f"/media/local/{media}/{last['id']}.jpg" if last and last["notified"] else None,
            "camera": cam["camera"],
            "paused": self.control.paused,
        })
        if can_alert:
            await self.control.swim_state(cid, on, swim_ev, await self.media_image(swim_ev) if on else None)
        else:
            self.control.swimming[cid] = on   # labels and timers never send an alert
        await self.ha.set_state(f"sensor.{cid}_people_in_water", max(swim_ev["in_water"], 1) if on else 0, {
            "friendly_name": tx["ent_count"].format(name=cam["name"]), "icon": "mdi:account-multiple",
            "state_class": "measurement"})
        if last:
            iso = dt.datetime.fromtimestamp(last["ts"]).astimezone().isoformat(timespec="seconds")
            await self.ha.set_state(f"sensor.{cid}_last_analysis", iso, {
                "friendly_name": tx["ent_last"].format(name=cam["name"]), "device_class": "timestamp",
                "probability": last["p"], "persons_near": last["persons_near"], "animals": last["animals"]})
