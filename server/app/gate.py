"""Принятие решений о проезде и связь с агентом управления воротами."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

import cv2
from fastapi import WebSocket, WebSocketDisconnect

from .config import Config
from .db import Database
from .plates import display, match_plate

log = logging.getLogger("lpr.gate")


class AgentHub:
    """Агент (компьютер у ворот) держит постоянное WebSocket-соединение с сервером.

    Соединение исходящее со стороны агента, поэтому на объекте не нужен белый IP
    и проброс портов.
    """

    def __init__(self):
        self.ws: WebSocket | None = None
        self.info: dict = {}
        self.connected_at = 0.0
        self.last_seen = 0.0
        self._pending: dict[str, asyncio.Future] = {}

    @property
    def online(self) -> bool:
        return self.ws is not None

    async def serve(self, ws: WebSocket):
        await ws.accept()
        if self.ws is not None:
            try:
                await self.ws.close(code=4000, reason="replaced")
            except Exception:
                pass
        self.ws, self.info = ws, {}
        self.connected_at = self.last_seen = time.time()
        log.info("Агент подключился: %s", ws.client)
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                self.last_seen = time.time()
                kind = msg.get("type")
                if kind == "hello":
                    self.info = {k: v for k, v in msg.items() if k != "type"}
                    log.info("Агент: %s", self.info)
                elif kind == "ack":
                    fut = self._pending.pop(msg.get("id", ""), None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                elif kind == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
        except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
            pass
        finally:
            if self.ws is ws:
                self.ws = None
                log.warning("Агент отключился")

    async def send_open(self, pulse: float, timeout: float) -> tuple[bool, str]:
        ws = self.ws
        if ws is None:
            return False, "агент ворот не на связи"
        cmd_id = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = fut
        try:
            await ws.send_text(json.dumps({"type": "open", "id": cmd_id, "pulse": pulse}))
            ack = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return False, "агент не подтвердил команду"
        except Exception as e:  # соединение оборвалось
            return False, f"ошибка отправки: {e}"
        finally:
            self._pending.pop(cmd_id, None)
        return bool(ack.get("ok")), ack.get("error", "") or "открыто"

    def status(self) -> dict:
        return {
            "online": self.online,
            "info": self.info,
            "connected_at": self.connected_at if self.online else None,
            "last_seen": self.last_seen or None,
        }


class AccessController:
    def __init__(self, cfg: Config, db: Database, hub: AgentHub, notifier=None):
        self.cfg = cfg
        self.db = db
        self.hub = hub
        self.notifier = notifier
        self.cams = {c.id: c for c in cfg.cameras}
        self.loop: asyncio.AbstractEventLoop | None = None
        self._last_command = 0.0
        self._last_open_plate: dict[str, float] = {}
        self._last_denied_notify: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.snap_dir = cfg.data_dir / "snapshots"

    def bind_loop(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop

    # вызывается из потоков камер
    def on_camera_event(self, kind: str, *args):
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._handle(kind, *args)))

    async def _handle(self, kind: str, *args):
        try:
            if kind == "confirmed":
                await self._on_confirmed(*args)
            elif kind == "finished":
                await self._on_finished(*args)
        except Exception:
            log.exception("Ошибка обработки события %s", kind)

    # --- открытие --------------------------------------------------------------
    async def open_gate(self, reason: str, force: bool = False) -> tuple[bool, str]:
        async with self._lock:
            now = time.time()
            if not force and now - self._last_command < self.cfg.gate.min_interval:
                return True, "команда уже отправлялась только что"
            ok, detail = await self.hub.send_open(self.cfg.gate.pulse_seconds, self.cfg.gate.ack_timeout)
            if ok:
                self._last_command = time.time()
            log.info("Открытие ворот (%s): %s — %s", reason, "OK" if ok else "ОШИБКА", detail)
            return ok, detail

    # --- обработка распознаваний ---------------------------------------------
    async def _on_confirmed(self, session, text: str, conf: float):
        cam = self.cams[session.camera]
        if session.decided or cam.open_policy == "none":
            return
        allowed = self.db.allowed_plates()
        matched, owner = None, ""
        if cam.open_policy == "whitelist":
            matched = match_plate(text, list(allowed), self.cfg.recognition.max_distance)
            if matched is None:
                return
            owner = allowed[matched]["owner"]
        session.decided = True

        key = matched or text
        now = time.time()
        if now - self._last_open_plate.get(key, 0) < self.cfg.gate.plate_cooldown:
            ok, detail = True, "повторное распознавание — ворота уже открывались"
            repeat = True
        else:
            self._last_open_plate[key] = now
            ok, detail = await self.open_gate(f"{display(key)} / {cam.name}")
            if not ok:
                self._last_open_plate.pop(key, None)  # дать шанс повторить
            repeat = False

        snap, crop = await self._save_images(session, text)
        event_id = self.db.add_event(
            camera=cam.id, plate=text, matched=matched, owner=owner, confidence=round(conf, 3),
            hits=session.counts.get(text, 0), decision="granted" if ok else "error",
            detail=detail, snapshot=snap, crop=crop,
        )
        if self.notifier and not repeat:
            await self.notifier.event(self.db.get_event(event_id), self._abs(snap))

    async def _on_finished(self, session):
        if session.decided or not session.counts:
            return
        text, hits, conf = session.best()
        rc = self.cfg.recognition
        # одиночные неуверенные чтения — скорее всего шум, в журнал не пишем
        if hits < rc.min_confirmations and conf < 0.85:
            return
        cam = self.cams[session.camera]
        decision = "denied" if cam.open_policy == "whitelist" else "logged"
        snap, crop = await self._save_images(session, text)
        event_id = self.db.add_event(
            camera=cam.id, plate=text, confidence=round(conf, 3), hits=hits,
            decision=decision, detail="нет в списке" if decision == "denied" else "",
            snapshot=snap, crop=crop,
        )
        now = time.time()
        if (self.notifier and decision == "denied"
                and now - self._last_denied_notify.get(text, 0) > 120):
            self._last_denied_notify[text] = now
            await self.notifier.event(self.db.get_event(event_id), self._abs(snap))

    # --- снимки --------------------------------------------------------------------
    def _abs(self, rel: str | None) -> Path | None:
        return self.snap_dir / rel if rel else None

    async def _save_images(self, session, text: str) -> tuple[str | None, str | None]:
        frame, crop = session.frame, session.crop

        def save():
            day = time.strftime("%Y-%m-%d")
            (self.snap_dir / day).mkdir(parents=True, exist_ok=True)
            base = f"{day}/{time.strftime('%H%M%S')}_{session.camera}_{text}_{uuid.uuid4().hex[:6]}"
            snap_rel = crop_rel = None
            if frame is not None:
                img = frame
                h, w = img.shape[:2]
                if w > 1280:
                    img = cv2.resize(img, (1280, int(h * 1280 / w)))
                cv2.imwrite(str(self.snap_dir / f"{base}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 80])
                snap_rel = f"{base}.jpg"
            if crop is not None and crop.size:
                cv2.imwrite(str(self.snap_dir / f"{base}_plate.jpg"), crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
                crop_rel = f"{base}_plate.jpg"
            return snap_rel, crop_rel

        return await asyncio.to_thread(save)

    # --- очистка старых данных ------------------------------------------------
    async def cleanup_loop(self):
        while True:
            try:
                cutoff = time.time() - self.cfg.retention_days * 86400
                for e in self.db.old_events(cutoff):
                    for rel in (e["snapshot"], e["crop"]):
                        if rel:
                            (self.snap_dir / rel).unlink(missing_ok=True)
                self.db.delete_events_before(cutoff)
                for d in self.snap_dir.glob("*"):
                    if d.is_dir() and not any(d.iterdir()):
                        d.rmdir()
            except Exception:
                log.exception("Ошибка очистки")
            await asyncio.sleep(6 * 3600)
