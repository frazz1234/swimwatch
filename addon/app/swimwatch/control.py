"""Alerts, pause ("we're going swimming"), pool season and the reminder while paused.

SwimWatch creates these helpers in Home Assistant so people can use them from a dashboard, a phone widget
or their own automations:
  input_boolean.swimwatch_season           on = pool open (off in winter: no analysis at all)
  input_boolean.swimwatch_pause            on = no alerts (people are swimming on purpose)
  input_select.swimwatch_pause_mode        "until we get out" (auto) / "until I turn it back on"
  input_boolean.swimwatch_critical_alerts  on = alerts may be critical (ring even on silent / Do Not Disturb)
  input_select.swimwatch_critical_when     when they are critical: always / at night / nobody home / both
The same controls are in the web page and in the notifications.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time

from .i18n import t

log = logging.getLogger("swimwatch.control")

SEASON = "input_boolean.swimwatch_season"
PAUSE = "input_boolean.swimwatch_pause"
MODE = "input_select.swimwatch_pause_mode"
CRITICAL = "input_boolean.swimwatch_critical_alerts"
WHEN = "input_select.swimwatch_critical_when"
MODES = ["out", "manual"]
WHENS = ["always", "night", "away", "night_or_away"]
HELPERS = [  # domain, object id, name text, options texts (selects), icon
    ("input_boolean", "swimwatch_season", "h_season", None, "mdi:pool"),
    ("input_boolean", "swimwatch_pause", "h_pause", None, "mdi:pause-circle"),
    ("input_select", "swimwatch_pause_mode", "h_mode", ["mode_" + m for m in MODES], "mdi:timer-sand"),
    ("input_boolean", "swimwatch_critical_alerts", "h_critical", None, "mdi:alarm-light"),
    ("input_select", "swimwatch_critical_when", "h_when", ["when_" + w for w in WHENS], "mdi:clock-alert"),
]


def _dur(sec: float, lang: str) -> str:
    m = int(sec // 60)
    h, m = divmod(m, 60)
    if lang == "fr":
        return f"{h} h {m:02d}" if h else f"{m} min"
    return f"{h} h {m:02d} min" if h else f"{m} min"


def _hm(text: str, default: str) -> dt.time:
    try:
        h, m = (int(x) for x in str(text or default).split(":"))
        return dt.time(h % 24, m % 60)
    except ValueError:
        h, m = (int(x) for x in default.split(":"))
        return dt.time(h, m)


class Control:
    def __init__(self, sw):
        self.sw = sw
        self.file = sw.cfg.data_dir / "control.json"
        st = json.loads(self.file.read_text(encoding="utf-8")) if self.file.exists() else {}
        self.paused_since = st.get("paused_since")
        self.mode = st.get("mode", "out")
        self.critical = bool(st.get("critical", False))
        self.when = st.get("when", "always")
        self.season = bool(st.get("season", True))
        self.last_reminder = st.get("last_reminder")
        self.activity_since_pause = st.get("activity_since_pause")  # last motion/person seen while paused
        self.helpers_ok = False
        self.swimming = {}     # camera id -> was the swim sensor on (alert only when it turns on)
        self.last_night = {}   # camera id -> time of the last "someone at the pool at night" alert

    @property
    def paused(self) -> bool:
        return self.paused_since is not None

    @property
    def opts(self) -> dict:
        return self.sw.cfg["alerts"]

    def _save(self):
        self.file.write_text(json.dumps({
            "paused_since": self.paused_since, "mode": self.mode, "critical": self.critical, "when": self.when,
            "season": self.season, "last_reminder": self.last_reminder,
            "activity_since_pause": self.activity_since_pause}), encoding="utf-8")

    def status(self) -> dict:
        return {"paused": self.paused, "paused_since": self.paused_since, "mode": self.mode,
                "critical": self.critical, "when": self.when, "season": self.season, "night": self.is_night(),
                "helpers_ok": self.helpers_ok,
                "entities": {"season": SEASON, "pause": PAUSE, "mode": MODE, "critical": CRITICAL, "when": WHEN}}

    # ------------------------------------------------------------ helpers in Home Assistant
    async def ensure_helpers(self):
        tx = t(self.sw.lang)
        ha = self.sw.ha
        try:
            lists = {}
            for domain, oid, name_key, option_keys, icon in HELPERS:
                if domain not in lists:
                    lists[domain] = await ha.ws_call({"type": f"{domain}/list"})
                fields = {"icon": icon}
                if option_keys:
                    fields["options"] = [tx[k] for k in option_keys]
                if not any(i.get("id") == oid for i in lists[domain]):
                    created = await ha.ws_call({"type": f"{domain}/create", "name": oid, **fields})
                    log.info("created helper %s.%s", domain, created.get("id"))
                # (re)name in the current language; the entity id stays the same
                await ha.ws_call({"type": f"{domain}/update", f"{domain}_id": oid, "name": tx[name_key], **fields})
            self.helpers_ok = True
        except Exception as exc:  # noqa: BLE001
            self.helpers_ok = False
            log.warning("could not create the Home Assistant helpers (%s). The controls still work "
                        "from the web page and the notifications.", exc)
            return
        self.sw.ha.watch_entities = [SEASON, PAUSE, MODE, CRITICAL, WHEN]
        await self.push_to_ha()

    async def push_to_ha(self):
        """Makes the helpers show SwimWatch's own state (after a restart, or a change from the page/phone)."""
        if not self.helpers_ok:
            return
        ha, tx = self.sw.ha, t(self.sw.lang)
        for ent, on in ((SEASON, self.season), (PAUSE, self.paused), (CRITICAL, self.critical)):
            await ha.call_service("input_boolean.turn_on" if on else "input_boolean.turn_off", target={"entity_id": ent})
        await ha.call_service("input_select.select_option", {"option": tx["mode_" + self.mode]}, {"entity_id": MODE})
        await ha.call_service("input_select.select_option", {"option": tx["when_" + self.when]}, {"entity_id": WHEN})

    async def _option_index(self, entity: str, state: str) -> int:
        st = await self.sw.ha.get_state(entity)
        opts = (st or {}).get("attributes", {}).get("options", [])
        return opts.index(state) if state in opts else -1

    async def on_state(self, entity: str, state: str | None):
        """A helper changed in Home Assistant (dashboard, widget, automation)."""
        if state in (None, "unavailable", "unknown"):
            return
        if entity == PAUSE:
            if state == "on" and not self.paused:
                await self.pause(self.mode, source="ha")
            elif state == "off" and self.paused:
                await self.resume("ha")
        elif entity == SEASON:
            if self.season != (state == "on"):
                await self.set_season(state == "on", source="ha")
        elif entity == CRITICAL:
            if self.critical != (state == "on"):
                self.critical = state == "on"
                self._save()
                log.info("critical alerts %s", "on" if self.critical else "off")
        elif entity == MODE:
            i = await self._option_index(MODE, state)
            if 0 <= i < len(MODES):
                self.mode = MODES[i]
                self._save()
        elif entity == WHEN:
            i = await self._option_index(WHEN, state)
            if 0 <= i < len(WHENS):
                self.when = WHENS[i]
                self._save()

    # ------------------------------------------------------------ season
    async def set_season(self, open_: bool, source: str = "web"):
        self.season = open_
        if not open_:
            self.paused_since = self.last_reminder = self.activity_since_pause = None
        self._save()
        log.info("pool season %s (from %s)", "open" if open_ else "closed", source)
        if source != "ha":
            await self.push_to_ha()
        for cid in self.sw.cfg.cameras:
            await self.sw.refresh_sensors(cid)

    # ------------------------------------------------------------ pause
    async def pause(self, mode: str = "out", source: str = "web"):
        self.mode = mode if mode in MODES else "out"
        self.paused_since = self.last_reminder = time.time()
        self.activity_since_pause = None
        self._save()
        log.info("paused (%s) from %s", self.mode, source)
        if source != "ha":
            await self.push_to_ha()

    async def resume(self, source: str = "web", auto_quiet_min: int | None = None):
        if not self.paused:
            return
        self.paused_since = self.last_reminder = self.activity_since_pause = None
        self._save()
        log.info("resumed from %s", source)
        if source != "ha":
            await self.push_to_ha()
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
        if not self.paused or not self.season:
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

    # ------------------------------------------------------------ when are alerts critical
    def is_night(self, when: dt.datetime | None = None) -> bool:
        now = (when or dt.datetime.now()).time()
        start, end = _hm(self.opts.get("night_start"), "22:00"), _hm(self.opts.get("night_end"), "07:00")
        return (start <= now or now < end) if start > end else (start <= now < end)

    async def nobody_home(self) -> bool:
        st = await self.sw.ha.get_state("zone.home")
        try:
            return int((st or {}).get("state", "1")) == 0
        except ValueError:
            return False

    async def critical_now(self) -> bool:
        if not self.critical:
            return False
        if self.when == "always":
            return True
        night = self.is_night()
        if self.when == "night":
            return night
        away = await self.nobody_home()
        return away if self.when == "away" else (night or away)

    # ------------------------------------------------------------ alerts
    async def swim_state(self, cid: str, on: bool, ev: dict | None, image: str | None):
        """Called after each analysis. Alerts when a camera's swim sensor turns on."""
        was = self.swimming.get(cid)
        self.swimming[cid] = on
        if not on or was is None or was:   # only off -> on; never on startup
            return
        if self.paused or not self.season:
            log.info("%s: swimming, no alert (%s)", cid, "paused" if self.paused else "season closed")
            return
        await self.alert(cid, ev, image)

    async def people_at_night(self, cid: str, ev: dict, image_fn):
        """Someone at the pool at night (not swimming): its own alert, at most every 10 minutes."""
        if not self.opts.get("night_people", True) or self.paused or not self.season or not self.is_night():
            return
        if ev["swimming"] or not (ev["persons_near"] or ev["in_water"]):
            return
        if time.time() - self.last_night.get(cid, 0) < 600:
            return
        self.last_night[cid] = time.time()
        await self.alert(cid, ev, await image_fn(ev), kind="night")

    async def alert(self, cid: str, ev: dict | None, image: str | None, test: bool = False, kind: str = "swim"):
        tx = t(self.sw.lang)
        cam = self.sw.cfg.cameras.get(cid, {"name": "SwimWatch"})
        when = time.strftime("%H:%M", time.localtime(ev["ts"] if ev else time.time()))
        if test:
            title, msg = tx["test_title"], tx["test_msg"]
        elif kind == "night":
            n = (ev["persons_near"] or 0) + (ev["in_water"] or 0)
            title = tx["night_title"].format(name=cam["name"])
            msg = tx["night_msg"].format(time=when, n=n)
        else:
            what = tx["in_water"].format(n=ev["in_water"]).rstrip(" ?") if ev and ev.get("in_water") else tx["someone"]
            title = tx["alert_title"].format(name=cam["name"])
            msg = tx["alert_msg"].format(time=when, what=what, p=f"{(ev or {}).get('p', 1):.0%}")
        actions = [{"action": "SWIMWATCH_PAUSE:out", "title": tx["act_pause"]}]
        if ev:
            actions.append({"action": f"SWIMWATCH_FALSE:{ev['id']}", "title": tx["act_false"]})
        data = {"actions": actions, "tag": f"swimwatch-alert-{cid}", "group": "swimwatch-alert"}
        if image:
            data["image"] = image
        link = self.sw.link(ev["id"] if ev else "")
        if link:
            data["url"] = data["clickAction"] = link
        critical = await self.critical_now()
        await self._send(title, msg, data, critical=critical)
        log.info("%s: %s alert sent (critical=%s)%s", cid, kind, critical, " [test]" if test else "")

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
