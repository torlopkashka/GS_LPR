"""Чтение RTSP-потоков с камер и распознавание номеров.

На каждую камеру запускаются два потока:
  * FrameReader — непрерывно читает поток и хранит только последний кадр
    (так не копится задержка, даже если распознавание не успевает);
  * CameraWorker — с заданной частотой берёт кадр, проверяет движение,
    ищет номера и «голосует» по нескольким кадрам, прежде чем принять решение.
"""

from __future__ import annotations

import logging
import os
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import cv2
import numpy as np

from .config import CameraConfig, RecognitionConfig
from .plates import clean_reading

log = logging.getLogger("lpr.camera")

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000")


# --------------------------------------------------------------------------
# Голосование по кадрам
# --------------------------------------------------------------------------
@dataclass
class Session:
    """Один «проезд»: серия чтений номера одной машины."""

    camera: str
    first: float
    last: float
    counts: dict[str, int] = field(default_factory=dict)
    confs: dict[str, list[float]] = field(default_factory=dict)
    confirmed: set[str] = field(default_factory=set)
    decided: bool = False
    best_conf: float = 0.0
    frame: np.ndarray | None = None
    crop: np.ndarray | None = None

    def best(self) -> tuple[str, int, float]:
        text = max(self.counts, key=lambda t: (self.counts[t], statistics.mean(self.confs[t])))
        return text, self.counts[text], statistics.mean(self.confs[text])

    def similar(self, text: str) -> bool:
        from .plates import levenshtein

        return any(levenshtein(text, t) <= 2 for t in self.counts)


class PlateVoter:
    """Группирует чтения в сессии и сообщает о подтверждённых номерах."""

    def __init__(self, camera: str, min_confirmations: int, session_gap: float):
        self.camera = camera
        self.min_confirmations = max(1, min_confirmations)
        self.session_gap = session_gap
        self.sessions: list[Session] = []

    def add(self, text: str, conf: float, now: float, frame=None, crop=None) -> tuple[Session, bool]:
        """Добавляет чтение. Возвращает (сессия, номер только что подтверждён)."""
        session = next((s for s in self.sessions if s.similar(text)), None)
        if session is None:
            session = Session(camera=self.camera, first=now, last=now)
            self.sessions.append(session)
        session.last = now
        session.counts[text] = session.counts.get(text, 0) + 1
        session.confs.setdefault(text, []).append(conf)
        if conf > session.best_conf and frame is not None:
            session.best_conf = conf
            session.frame = frame.copy()
            session.crop = None if crop is None else crop.copy()
        newly = session.counts[text] >= self.min_confirmations and text not in session.confirmed
        if newly:
            session.confirmed.add(text)
        return session, newly

    def expire(self, now: float) -> list[Session]:
        done = [s for s in self.sessions if now - s.last > self.session_gap]
        self.sessions = [s for s in self.sessions if s not in done]
        return done


# --------------------------------------------------------------------------
# Движок распознавания
# --------------------------------------------------------------------------
class Engine:
    def __init__(self, cfg: RecognitionConfig):
        import onnxruntime as ort
        from fast_alpr import ALPR

        def opts():
            o = ort.SessionOptions()
            o.intra_op_num_threads = cfg.onnx_threads
            o.inter_op_num_threads = 1
            return o

        self.alpr = ALPR(
            detector_model=cfg.detector_model,
            detector_conf_thresh=cfg.detector_conf,
            detector_providers=["CPUExecutionProvider"],
            detector_sess_options=opts(),
            ocr_model=cfg.ocr_model,
            ocr_device="cpu",
            ocr_sess_options=opts(),
        )

    def predict(self, img: np.ndarray) -> list[tuple[str, float, tuple[int, int, int, int]]]:
        out = []
        for r in self.alpr.predict(img):
            if r.ocr is None or not r.ocr.text:
                continue
            conf = r.ocr.confidence
            conf = statistics.mean(conf) if isinstance(conf, list) else float(conf)
            b = r.detection.bounding_box
            out.append((r.ocr.text, conf, (b.x1, b.y1, b.x2, b.y2)))
        return out


# --------------------------------------------------------------------------
# Чтение потока
# --------------------------------------------------------------------------
class FrameReader(threading.Thread):
    def __init__(self, cam: CameraConfig):
        super().__init__(daemon=True, name=f"reader-{cam.id}")
        self.cam = cam
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self.frame_ts = 0.0
        self.frame_no = 0
        self.connected = False
        self.error = ""
        self._halt = threading.Event()

    def latest(self) -> tuple[np.ndarray | None, int, float]:
        with self._lock:
            return self._frame, self.frame_no, self.frame_ts

    def stop(self):
        self._halt.set()

    def run(self):
        backoff = 2.0
        while not self._halt.is_set():
            cap = cv2.VideoCapture(self.cam.url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                self.connected = False
                self.error = "не удалось открыть поток"
                log.warning("[%s] не удалось открыть поток, повтор через %.0f c", self.cam.id, backoff)
                self._halt.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            log.info("[%s] поток открыт", self.cam.id)
            self.connected, self.error, backoff = True, "", 2.0
            fails = 0
            while not self._halt.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    fails += 1
                    if fails > 25:
                        self.error = "поток прерван"
                        log.warning("[%s] поток прерван, переподключение", self.cam.id)
                        break
                    time.sleep(0.05)
                    continue
                fails = 0
                with self._lock:
                    self._frame = frame
                    self.frame_no += 1
                    self.frame_ts = time.time()
            cap.release()
            self.connected = False
            self._halt.wait(1)


# --------------------------------------------------------------------------
# Обработка камеры
# --------------------------------------------------------------------------
class CameraWorker(threading.Thread):
    """Сообщения в on_event: ("confirmed", session, text, conf) и ("finished", session)."""

    def __init__(self, cam: CameraConfig, rcfg: RecognitionConfig, on_event: Callable):
        super().__init__(daemon=True, name=f"worker-{cam.id}")
        self.cam = cam
        self.rcfg = rcfg
        self.on_event = on_event
        self.reader = FrameReader(cam)
        self.voter = PlateVoter(cam.id, rcfg.min_confirmations, rcfg.session_gap)
        self._halt = threading.Event()
        self._prev_small: np.ndarray | None = None
        self.motion_until = 0.0
        self.last_analyzed = 0.0
        self.last_boxes: list[tuple[str, float, tuple[int, int, int, int]]] = []
        self.last_boxes_ts = 0.0
        self.analyze_ms = 0.0
        self.last_plate = ""
        self.engine: Engine | None = None

    def stop(self):
        self._halt.set()
        self.reader.stop()

    # --- вспомогательное ---------------------------------------------------
    def _roi(self, frame: np.ndarray) -> tuple[np.ndarray, int, int]:
        if not self.cam.roi:
            return frame, 0, 0
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = self.cam.roi
        x1, x2 = int(x1 * w), int(x2 * w)
        y1, y2 = int(y1 * h), int(y2 * h)
        return frame[y1:y2, x1:x2], x1, y1

    def _motion(self, img: np.ndarray) -> bool:
        h, w = img.shape[:2]
        scale = 160 / max(w, 1)
        small = cv2.resize(img, (160, max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
        small = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
        prev, self._prev_small = self._prev_small, small
        if prev is None or prev.shape != small.shape:
            return True
        diff = cv2.absdiff(prev, small)
        return (diff > 25).mean() > self.cam.motion_threshold

    def preview_jpeg(self) -> bytes | None:
        frame, _, _ = self.reader.latest()
        if frame is None:
            return None
        img = frame.copy()
        h, w = img.shape[:2]
        if self.cam.roi:
            x1, y1, x2, y2 = self.cam.roi
            cv2.rectangle(img, (int(x1 * w), int(y1 * h)), (int(x2 * w), int(y2 * h)), (255, 180, 0), 2)
        if time.time() - self.last_boxes_ts < 3:
            for text, conf, (bx1, by1, bx2, by2) in self.last_boxes:
                cv2.rectangle(img, (bx1, by1), (bx2, by2), (0, 255, 0), 2)
                cv2.putText(img, f"{text} {conf:.0%}", (bx1, max(20, by1 - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
        if w > 1280:
            img = cv2.resize(img, (1280, int(h * 1280 / w)))
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
        return buf.tobytes() if ok else None

    def status(self) -> dict:
        _, _, ts = self.reader.latest()
        return {
            "id": self.cam.id,
            "name": self.cam.name,
            "role": self.cam.role,
            "connected": self.reader.connected and time.time() - ts < 10,
            "error": self.reader.error,
            "last_frame_age": round(time.time() - ts, 1) if ts else None,
            "analyze_ms": round(self.analyze_ms),
            "last_plate": self.last_plate,
        }

    # --- основной цикл ------------------------------------------------------
    def run(self):
        try:
            self.engine = Engine(self.rcfg)
        except Exception:
            log.exception("[%s] не удалось загрузить модели распознавания", self.cam.id)
            return
        self.reader.start()
        period = 1.0 / max(self.cam.process_fps, 0.1)
        last_no = -1
        while not self._halt.is_set():
            started = time.time()
            frame, no, _ = self.reader.latest()
            if frame is not None and no != last_no:
                last_no = no
                try:
                    self._process(frame, started)
                except Exception:
                    log.exception("[%s] ошибка обработки кадра", self.cam.id)
            for session in self.voter.expire(time.time()):
                self.on_event("finished", session)
            self._halt.wait(max(0.0, period - (time.time() - started)))

    def _process(self, frame: np.ndarray, now: float):
        roi, ox, oy = self._roi(frame)
        if self._motion(roi):
            self.motion_until = now + 2.0
        idle = self.cam.idle_interval
        if now > self.motion_until and not (idle and now - self.last_analyzed >= idle):
            return
        self.last_analyzed = now
        t0 = time.perf_counter()
        results = self.engine.predict(roi)
        self.analyze_ms = (time.perf_counter() - t0) * 1000
        boxes = []
        for raw, conf, (x1, y1, x2, y2) in results:
            box = (x1 + ox, y1 + oy, x2 + ox, y2 + oy)
            boxes.append((raw, conf, box))
            text = clean_reading(raw, self.rcfg.plate_format)
            if not text or conf < self.rcfg.min_ocr_conf:
                log.debug("[%s] отброшено: %s (%.2f)", self.cam.id, raw, conf)
                continue
            self.last_plate = text
            crop = frame[max(box[1], 0):box[3], max(box[0], 0):box[2]] if self.rcfg.save_crops else None
            session, newly = self.voter.add(text, conf, now, frame, crop)
            log.debug("[%s] чтение %s (%.2f) x%d", self.cam.id, text, conf, session.counts[text])
            if newly:
                self.on_event("confirmed", session, text, statistics.mean(session.confs[text]))
        if boxes:
            self.last_boxes, self.last_boxes_ts = boxes, now
