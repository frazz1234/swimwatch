"""Small Home Assistant client: REST calls plus one WebSocket for triggers and events."""
from __future__ import annotations

import asyncio
import itertools
import logging

import aiohttp

log = logging.getLogger("swimwatch.ha")


class HomeAssistant:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token
        self.session: aiohttp.ClientSession | None = None
        self._ids = itertools.count(1)
        self.connected = asyncio.Event()
        self.trigger_entities: list[str] = []
        self.watch_entities: list[str] = []      # any state change of these calls on_state
        self._ws = None

    @property
    def ws_url(self):
        # Through the Supervisor (add-on) the WebSocket is at /core/websocket, else /api/websocket.
        return f"{self.url}/websocket" if self.url.endswith("supervisor/core") else f"{self.url}/api/websocket"

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    async def start(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    async def close(self):
        if self.session:
            await self.session.close()

    # ---------- REST ----------
    async def check(self, url: str | None = None, token: str | None = None) -> str:
        """"ok", "unreachable" or "refused" (bad token)."""
        url, token = (url or self.url).rstrip("/"), token if token is not None else self.token
        if not token:
            return "refused"
        try:
            async with self.session.get(f"{url}/api/", headers={"Authorization": f"Bearer {token}"},
                                        timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return "ok"
                return "refused" if r.status in (401, 403) else "unreachable"
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return "unreachable"

    async def states(self) -> list[dict]:
        async with self.session.get(f"{self.url}/api/states", headers=self.headers) as r:
            return await r.json() if r.status == 200 else []

    async def config(self) -> dict:
        async with self.session.get(f"{self.url}/api/config", headers=self.headers) as r:
            return await r.json() if r.status == 200 else {}

    async def services(self) -> list[dict]:
        async with self.session.get(f"{self.url}/api/services", headers=self.headers) as r:
            return await r.json() if r.status == 200 else []

    async def template(self, tpl: str):
        async with self.session.post(f"{self.url}/api/template", headers=self.headers, json={"template": tpl}) as r:
            if r.status != 200:
                log.warning("template -> %s %s", r.status, (await r.text())[:200])
                return None
            return await r.text()

    async def get_state(self, entity_id: str) -> dict | None:
        async with self.session.get(f"{self.url}/api/states/{entity_id}", headers=self.headers) as r:
            if r.status != 200:
                return None
            return await r.json()

    async def set_state(self, entity_id: str, state, attributes: dict):
        async with self.session.post(f"{self.url}/api/states/{entity_id}", headers=self.headers,
                                     json={"state": state, "attributes": attributes}) as r:
            if r.status not in (200, 201):
                log.warning("set_state %s -> %s %s", entity_id, r.status, await r.text())

    async def camera_image(self, entity_id: str) -> bytes | None:
        try:
            async with self.session.get(f"{self.url}/api/camera_proxy/{entity_id}", headers=self.headers,
                                        timeout=aiohttp.ClientTimeout(total=25)) as r:
                if r.status != 200:
                    log.debug("camera_proxy %s -> %s", entity_id, r.status)
                    return None
                return await r.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.debug("camera_proxy %s failed: %s", entity_id, exc)
            return None

    async def call_service(self, service: str, data: dict | None = None, target: dict | None = None):
        domain, name = service.split(".", 1)
        body = dict(data or {})
        body.update(target or {})
        async with self.session.post(f"{self.url}/api/services/{domain}/{name}", headers=self.headers,
                                     json=body, timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status != 200:
                log.warning("service %s -> %s %s", service, r.status, (await r.text())[:300])
                return False
            return True

    async def fire_event(self, event_type: str, data: dict):
        async with self.session.post(f"{self.url}/api/events/{event_type}", headers=self.headers, json=data) as r:
            return r.status == 200

    async def upload_media(self, folder: str, filename: str, content: bytes) -> str | None:
        """Put a picture in HA's local media (/media/<folder>/). Returns its /media/local/... path."""
        form = aiohttp.FormData()
        form.add_field("media_content_id", f"media-source://media_source/local/{folder}")
        form.add_field("file", content, filename=filename, content_type="image/jpeg")
        async with self.session.post(f"{self.url}/api/media_source/local_source/upload",
                                     headers=self.headers, data=form) as r:
            if r.status != 200:
                log.warning("media upload -> %s %s", r.status, (await r.text())[:300])
                return None
        return f"/media/local/{folder}/{filename}"

    # ---------- WebSocket ----------
    async def ws_call(self, payload: dict):
        """One WebSocket command on its own short connection (e.g. create a helper). Returns its result."""
        async with self.session.ws_connect(self.ws_url, timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": self.token})
            if (await ws.receive_json()).get("type") != "auth_ok":
                raise RuntimeError("auth failed")
            await ws.send_json({"id": 1, **payload})
            while True:
                msg = await asyncio.wait_for(ws.receive_json(), 30)
                if msg.get("id") == 1:
                    if not msg.get("success"):
                        raise RuntimeError(msg.get("error"))
                    return msg.get("result")

    async def resubscribe(self):
        """Reconnects so a new list of trigger entities is used."""
        if self._ws is not None:
            await self._ws.close()

    async def run_websocket(self, on_trigger, on_event: dict, on_state=None):
        """Keeps one WebSocket open (reconnects). on_trigger(entity_id) when one of self.trigger_entities
        turns on; on_event[event_type](data) for each subscribed event."""
        delay = 2
        while True:
            try:
                async with self.session.ws_connect(self.ws_url, heartbeat=30,
                                                   timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
                    self._ws = ws
                    trigger_entities = list(self.trigger_entities)
                    msg = await ws.receive_json()
                    await ws.send_json({"type": "auth", "access_token": self.token})
                    msg = await ws.receive_json()
                    if msg.get("type") != "auth_ok":
                        raise RuntimeError(f"auth failed: {msg}")
                    subs = {}
                    if trigger_entities:
                        i = next(self._ids)
                        subs[i] = "trigger"
                        await ws.send_json({"id": i, "type": "subscribe_trigger", "trigger": {
                            "platform": "state", "entity_id": trigger_entities, "to": "on"}})
                    watch = list(self.watch_entities)
                    if watch and on_state:
                        i = next(self._ids)
                        subs[i] = "watch"
                        await ws.send_json({"id": i, "type": "subscribe_trigger", "trigger": {
                            "platform": "state", "entity_id": watch}})
                    for etype in on_event:
                        i = next(self._ids)
                        subs[i] = etype
                        await ws.send_json({"id": i, "type": "subscribe_events", "event_type": etype})
                    log.info("WebSocket connected, watching %s", ", ".join(trigger_entities) or "nothing yet")
                    self.connected.set()
                    delay = 2
                    async for raw in ws:
                        if raw.type != aiohttp.WSMsgType.TEXT:
                            continue
                        msg = raw.json()
                        if msg.get("type") == "result" and not msg.get("success"):
                            log.error("subscription %s failed: %s", subs.get(msg.get("id")), msg.get("error"))
                        if msg.get("type") != "event":
                            continue
                        kind = subs.get(msg.get("id"))
                        ev = msg["event"]
                        if kind == "trigger":
                            ent = ev["variables"]["trigger"]["entity_id"]
                            asyncio.create_task(on_trigger(ent))
                        elif kind == "watch":
                            tv = ev["variables"]["trigger"]
                            asyncio.create_task(on_state(tv["entity_id"], (tv.get("to_state") or {}).get("state")))
                        elif kind in on_event:
                            asyncio.create_task(on_event[kind](ev.get("data", {})))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("WebSocket lost (%s), retry in %ss", exc, delay)
            self._ws = None
            self.connected.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
