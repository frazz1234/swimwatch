"""Real Home Assistant entities through MQTT, when a broker exists (e.g. the Mosquitto add-on).

Each pool camera becomes a device "SwimWatch <name>" with: swimming (binary sensor), people in the water,
probability, last analysis time, and an image entity with the last analyzed picture (for dashboards).
Without a broker SwimWatch keeps setting plain states through the REST API (no device, no image).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from . import __version__
from .i18n import t

log = logging.getLogger("swimwatch.mqtt")
PREFIX = "homeassistant"
STATUS = "swimwatch/status"


class Mqtt:
    def __init__(self, sw):
        self.sw = sw
        self.client = None
        self.connected = False
        self.announced: set[str] = set()
        self.info = None   # where it is connected, for the web page

    async def _credentials(self) -> dict | None:
        if self.sw.cfg.addon:
            try:
                async with self.sw.ha.session.get(
                        "http://supervisor/services/mqtt",
                        headers={"Authorization": f"Bearer {os.environ['SUPERVISOR_TOKEN']}"}) as r:
                    if r.status != 200:
                        return None
                    d = (await r.json()).get("data") or {}
                    return {"host": d.get("host"), "port": int(d.get("port", 1883)),
                            "username": d.get("username"), "password": d.get("password")} if d.get("host") else None
            except Exception as exc:  # noqa: BLE001
                log.debug("no MQTT service: %s", exc)
                return None
        m = self.sw.cfg["mqtt"]
        return dict(m) if m.get("host") else None

    async def run(self):
        """Connects when a broker is available, reconnects, and checks again every 5 minutes otherwise."""
        import aiomqtt
        while True:
            creds = await self._credentials()
            if not creds:
                await asyncio.sleep(300)
                continue
            try:
                will = aiomqtt.Will(STATUS, "offline", qos=1, retain=True)
                async with aiomqtt.Client(hostname=creds["host"], port=int(creds.get("port") or 1883),
                                          username=creds.get("username") or None, password=creds.get("password") or None,
                                          identifier="swimwatch", will=will) as client:
                    self.client, self.connected = client, True
                    self.info = f"{creds['host']}:{creds.get('port') or 1883}"
                    log.info("MQTT connected (%s): real Home Assistant entities", self.info)
                    await client.publish(STATUS, "online", qos=1, retain=True)
                    self.announced.clear()
                    await self.announce()
                    await client.subscribe(f"{PREFIX}/status")
                    async for msg in client.messages:      # HA restarted: send everything again
                        if msg.payload == b"online":
                            await asyncio.sleep(5)
                            self.announced.clear()
                            await self.announce()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("MQTT lost (%s), retry in 30 s", exc)
            self.client, self.connected = None, False
            await asyncio.sleep(30)

    # ------------------------------------------------------------ discovery
    def _device_topic(self, cid: str) -> str:
        return f"{PREFIX}/device/swimwatch_{cid}/config"

    async def announce(self):
        """Discovery messages for every camera (and removal for cameras that are gone)."""
        if not self.connected:
            return
        tx = t(self.sw.lang)
        known = self.sw.cfg.data_dir / "mqtt_devices.json"
        before = set(json.loads(known.read_text())) if known.exists() else set()
        for cid, cam in self.sw.cfg.cameras.items():
            if cid not in before:
                # first time over MQTT: remove the plain REST states that used these entity ids,
                # or Home Assistant would name the new entities "..._2"
                for ent in (f"binary_sensor.{cid}_swimming", f"sensor.{cid}_people_in_water", f"sensor.{cid}_last_analysis"):
                    await self.sw.ha.delete_state(ent)
            state = f"swimwatch/{cid}/state"
            payload = {
                "dev": {"ids": [f"swimwatch_{cid}"], "name": f"SwimWatch {cam['name']}", "mf": "SwimWatch",
                        "mdl": "Pool camera", "sw": __version__},
                "o": {"name": "SwimWatch", "sw": __version__, "url": "https://github.com/frazz1234/swimwatch"},
                "availability_topic": STATUS,
                "cmps": {
                    "swimming": {"p": "binary_sensor", "unique_id": f"swimwatch_{cid}_swimming",
                                 "default_entity_id": f"binary_sensor.{cid}_swimming", "name": tx["e_swimming"],
                                 "device_class": "occupancy", "state_topic": state,
                                 "value_template": "{{ value_json.swimming }}", "payload_on": "ON", "payload_off": "OFF",
                                 "json_attributes_topic": state, "json_attributes_template": "{{ value_json.attributes | tojson }}"},
                    "people": {"p": "sensor", "unique_id": f"swimwatch_{cid}_people", "default_entity_id": f"sensor.{cid}_people_in_water",
                               "name": tx["e_people"], "state_topic": state, "value_template": "{{ value_json.people }}",
                               "state_class": "measurement", "icon": "mdi:account-multiple"},
                    "probability": {"p": "sensor", "unique_id": f"swimwatch_{cid}_probability",
                                    "default_entity_id": f"sensor.{cid}_swim_probability", "name": tx["e_probability"],
                                    "state_topic": state, "value_template": "{{ value_json.probability }}",
                                    "unit_of_measurement": "%", "state_class": "measurement", "icon": "mdi:percent"},
                    "last": {"p": "sensor", "unique_id": f"swimwatch_{cid}_last", "default_entity_id": f"sensor.{cid}_last_analysis",
                             "name": tx["e_last"], "state_topic": state, "value_template": "{{ value_json.last }}",
                             "device_class": "timestamp"},
                    "picture": {"p": "image", "unique_id": f"swimwatch_{cid}_picture", "default_entity_id": f"image.{cid}_last_picture",
                                "name": tx["e_picture"], "image_topic": f"swimwatch/{cid}/image", "content_type": "image/jpeg"},
                },
            }
            await self.client.publish(self._device_topic(cid), json.dumps(payload), qos=1, retain=True)
            self.announced.add(cid)
        for cid in before - set(self.sw.cfg.cameras):   # cameras removed in the setup page
            await self.client.publish(self._device_topic(cid), "", qos=1, retain=True)
            await self.client.publish(f"swimwatch/{cid}/image", b"", qos=1, retain=True)
        known.write_text(json.dumps(sorted(self.sw.cfg.cameras)))
        await asyncio.sleep(2)   # let HA create the entities before the first states
        for cid in self.sw.cfg.cameras:
            await self.sw.refresh_sensors(cid)
            last = self.sw.store.list("all", limit=1, camera=cid)
            if last:
                from pathlib import Path
                f = Path(last[0]["dir"]) / "a.jpg"
                if f.exists():
                    await self.publish_image(cid, f.read_bytes())

    # ------------------------------------------------------------ states
    async def publish_state(self, cid: str, state: dict):
        if self.connected and self.client:
            await self.client.publish(f"swimwatch/{cid}/state", json.dumps(state), qos=1, retain=True)

    async def publish_image(self, cid: str, jpeg: bytes):
        if self.connected and self.client:
            await self.client.publish(f"swimwatch/{cid}/image", jpeg, qos=1, retain=True)
