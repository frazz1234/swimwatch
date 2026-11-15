"""Swim alerts, pause ("we're going swimming") and the reminder while paused.

SwimWatch creates three helpers in Home Assistant so people can use them from a dashboard, a phone widget
or their own automations:
  input_boolean.swimwatch_pause            on = no swim alerts
  input_select.swimwatch_pause_mode        "until we get out" (auto) / "until I turn it back on"
  input_boolean.swimwatch_critical_alerts  on = swim alerts are critical (ring even on silent / Do Not Disturb)
The same controls are in the web page and in the notifications.
"""
from __future__ import annotations

import json
import logging
import time

from .i18n import t

log = logging.getLogger("swimwatch.control")

PAUSE = "input_boolean.swimwatch_pause"
MODE = "input_select.swimwatch_pause_mode"
CRITICAL = "input_boolean.swimwatch_critical_alerts"
HELPERS = [
    ("input_boolean", "swimwatch_pause", "h_pause", {"icon": "mdi:pause-circle"}),
    ("input_select", "swimwatch_pause_mode", "h_mode", {"icon": "mdi:timer-sand"}),
    ("input_boolean", "swimwatch_critical_alerts", "h_critical", {"icon": "mdi:alarm-light"}),
]
MODES = ["out", "manual"]


def _dur(sec: float, lang: str) -> str:
    m = int(sec // 60)
    h, m = divmod(m, 60)
    if lang == "fr":
        return f"{h} h {m:02d}" if h else f"{m} min"
    return f"{h} h {m:02d} min" if h else f"{m} min"


class Control:
    def __init__(self, sw):
        self.sw = sw
        self.file = sw.cfg.data_dir / "control.json"
        st = json.loads(self.file.read_text(encoding="utf-8")) if self.file.exists() else {}
        self.paused_since = st.get("paused_since")
        self.mode = st.get("mode", "out")
        self.critical = bool(st.get("critical", False))
        self.last_reminder = st.get("last_reminder")
        self.activity_since_pause = st.get("activity_since_pause")  # last motion/person seen while paused
        self.helpers_ok = False
        self.swimming = {}  # camera id -> was the swim sensor on (for "newly on" alerts)

    @property
    def paused(self) -> bool:
        return self.paused_since is not None

    @property
    def opts(self) -> dict:
        return self.sw.cfg["alerts"]

    def _save(self):
        self.file.write_text(json.dumps({
            "paused_since": self.paused_since, "mode": self.mode, "critical": self.critical,
            "last_reminder": self.last_reminder, "activity_since_pause": self.activity_since_pause}), encoding="utf-8")

    def status(self) -> dict:
        return {"paused": self.paused, "paused_since": self.paused_since, "mode": self.mode,
                "critical": self.critical, "helpers_ok": self.helpers_ok,
                "entities": {"pause": PAUSE, "mode": MODE, "critical": CRITICAL}}

    # ------------------------------------------------------------ helpers in Home Assistant
    async def ensure_helpers(self):
        tx = t(self.sw.lang)
        ha = self.sw.ha
        try:
            for domain, oid, name_key, extra in HELPERS:
                items = await ha.ws_call({"type": f"{domain}/list"})
                fields = dict(extra)
                if domain == "input_select":
                    fields["options"] = [tx["mode_out"], tx["mode_manual"]]
                if not any(i.get("id") == oid for i in items):
                    created = await ha.ws_call({"type": f"{domain}/create", "name": oid, **fields})
                    log.info("created helper %s.%s", domain, created.get("id"))
                # (re)name in the current language; the entity id stays the same
                await ha.ws_call({"type": f"{domain}/update", f"{domain}_id": oid, "name": tx[name_key], **fields})
            self.helpers_ok = True
        except Exception as exc:  # noqa: BLE001
            self.helpers_ok = False
            log.warning("could not create the Home Assistant helpers (%s). Pause and critical alerts still work "
                        "from the web page and the notifications.", exc)
            return
        self.sw.ha.watch_entities = [PAUSE, MODE, CRITICAL]
        await self._push_to_ha()

    async def _push_to_ha(self):
        """Makes the helpers show SwimWatch's own state (after a restart, or a change from the page/phone)."""
        if not self.helpers_ok:
            return
        ha, tx = self.sw.ha, t(self.sw.lang)
        await ha.call_service("input_boolean.turn_on" if self.paused else "input_boolean.turn_off", target={"entity_id": PAUSE})
        await ha.call_service("input_boolean.turn_on" if self.critical else "input_boolean.turn_off", target={"entity_id": CRITICAL})
        await ha.call_service("input_select.select_option", {"option": tx["mode_" + self.mode]}, {"entity_id": MODE})

    async def on_state(self, entity: str, state: str | None):
        """A helper changed in Home Assistant (dashboard, widget, automation)."""
        if state in (None, "unavailable", "unknown"):
            return
        if entity == PAUSE:
            if state == "on" and not self.paused:
                await self.pause(self.mode, source="ha")
            elif state == "off" and self.paused:
                await self.resume("ha")
        elif entity == CRITICAL:
            if self.critical != (state == "on"):
                self.critical = state == "on"
                self._save()
                log.info("critical alerts %s", "on" if self.critical else "off")
        elif entity == MODE:
            st = await self.sw.ha.get_state(MODE)
            opts = (st or {}).get("attributes", {}).get("options", [])
            if state in opts and opts.index(state) < len(MODES):
                self.mode = MODES[opts.index(state)]
                self._save()

    # ------------------------------------------------------------ pause
    async def pause(self, mode: str = "out", source: str = "web"):
        self.mode = mode if mode in MODES else "out"
        self.paused_since = self.last_reminder = time.time()
        self.activity_since_pause = None
        self._save()
        log.info("paused (%s) from %s", self.mode, source)
        if source != "ha":
            await self._push_to_ha()

    async def resume(self, source: str = "web", auto_quiet_min: int | None = None):
        if not self.paused:
            return
        self.paused_since = self.last_reminder = self.activity_since_pause = None
        self._save()
        log.info("resumed from %s", source)
        if source != "ha":
            await self._push_to_ha()
        if auto_quiet_min is not None:
            tx = t(self.sw.lang)
            await self._send(tx["resumed_title"], tx["resumed_msg"].format(min=auto_quiet_min), {}, critical=False)

    def activity(self):
        """Motion or a person at the pool (used to know when people got out)."""
        if self.paused:
            self.activity_since_pause = time.time()
            self._save()

    async def tick(self):
        """Every few seconds: auto-resume and reminder."""
        if not self.paused:
            return
        now = time.time()
        quiet = int(self.opts.get("resume_quiet_minutes", 20))
        if self.mode == "out" and self.activity_since_pause and now - self.activity_since_pause >= quiet * 60:
            await self.resume("auto", auto_quiet_min=quiet)
            return
        every = float(self.opts.get("reminder_minutes", 60)) * 60
        if every and now - (self.last_reminder or self.paused_since) >= every:
            self.last_reminder = now
            self._save()
            tx = t(self.sw.lang)
            await self._send(tx["paused_title"], tx["paused_msg"].format(dur=_dur(now - self.paused_since, self.sw.lang)),
                             {"actions": [{"action": "SWIMWATCH_RESUME", "title": tx["act_resume"]},
                                          {"action": "SWIMWATCH_SNOOZE", "title": tx["act_snooze"]}],
                              "tag": "swimwatch-paused"}, critical=False)

    # ------------------------------------------------------------ swim alerts
    async def swim_state(self, cid: str, on: bool, ev: dict | None, image: str | None):
        """Called after each sensor refresh. Alerts when a camera's swim sensor turns on."""
        was = self.swimming.get(cid)
        self.swimming[cid] = on
        if not on or was is None or was:   # only off -> on; never on startup
            return
        if self.paused:
            log.info("%s: swimming, no alert (paused)", cid)
            return
        await self.alert(cid, ev, image)

    async def alert(self, cid: str, ev: dict | None, image: str | None, test: bool = False):
        tx = t(self.sw.lang)
        cam = self.sw.cfg.cameras.get(cid, {"name": "SwimWatch"})
        if test:
            title, msg = tx["test_title"], tx["test_msg"]
        else:
            what = tx["in_water"].format(n=ev["in_water"]).rstrip(" ?") if ev and ev.get("in_water") else tx["someone"]
            title = tx["alert_title"].format(name=cam["name"])
            msg = tx["alert_msg"].format(time=time.strftime("%H:%M", time.localtime(ev["ts"] if ev else time.time())),
                                         what=what, p=f"{(ev or {}).get('p', 1):.0%}")
        actions = [{"action": "SWIMWATCH_PAUSE:out", "title": tx["act_pause"]}]
        if ev:
            actions.append({"action": f"SWIMWATCH_FALSE:{ev['id']}", "title": tx["act_false"]})
        data = {"actions": actions, "tag": f"swimwatch-alert-{cid}", "group": "swimwatch-alert"}
        if image:
            data["image"] = image
        link = self.sw.link(ev["id"] if ev else "")
        if link:
            data["url"] = data["clickAction"] = link
        await self._send(title, msg, data, critical=self.critical)
        log.info("%s: swim alert sent (critical=%s)%s", cid, self.critical, " [test]" if test else "")

    async def _send(self, title: str, message: str, data: dict, critical: bool):
        services = self.opts.get("services") or []
        data = dict(data)
        if critical:
            # iOS: critical alert (rings on silent / Focus). Android: alarm channel.
            data["push"] = {"sound": {"name": "default", "critical": 1, "volume": 1.0}, "interruption-level": "critical"}
            data.update({"ttl": 0, "priority": "high", "channel": "alarm_stream"})
        for svc in services:
            await self.sw.ha.call_service(svc, {"title": title, "message": message, "data": data})

    # ------------------------------------------------------------ phone buttons
    async def on_action(self, action: str) -> bool:
        if action.startswith("SWIMWATCH_PAUSE"):
            mode = action.split(":", 1)[1] if ":" in action else "out"
            await self.pause(mode, source="phone")
        elif action == "SWIMWATCH_RESUME":
            await self.resume("phone")
        elif action == "SWIMWATCH_SNOOZE":
            self.last_reminder = time.time()
            self._save()
        elif action.startswith("SWIMWATCH_FALSE:"):
            ev_id = action.split(":", 1)[1]
            ev = self.sw.store.get(ev_id)
            if ev:
                label = "near" if ev["persons_near"] or ev["in_water"] else "animal" if ev["animals"] else "none"
                await self.sw.set_label(ev_id, label, "phone")
        else:
            return False
        return True
