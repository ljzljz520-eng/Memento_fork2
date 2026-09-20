import Xlib.display
import Xlib.X
import Xlib.xobject.drawable
import av
import fractions
import time
import asyncio
import cv2
import numpy as np
import os

FPS = 0.5
SECONDS_PER_REC = 10
VIDEO_CLOCK_RATE = 90000
VIDEO_PTIME = 1 / FPS
VIDEO_TIME_BASE = fractions.Fraction(1, VIDEO_CLOCK_RATE)
# RESOLUTION = (2560, 1440)
# RESOLUTION = (1280, 720)
RESOLUTION = (1920, 1080)
MAX_TWS = 10000 * FPS * SECONDS_PER_REC
FRAME_CACHE_SIZE = int((MAX_TWS / FPS / SECONDS_PER_REC))
CACHE_PATH = os.path.join(os.environ["HOME"], ".cache", "memento")

# Sentinel stored in place of window_title for frames dropped by the capture
# policy. Must never collide with a real WM_CLASS.
GAP_APP = "__memento_gap__"
DECISION_ALLOW = "allow"
DECISION_DROP = "drop"
DECISION_REDACT = "redact"

_CONFIG_HOME = os.environ.get(
    "XDG_CONFIG_HOME", os.path.join(os.environ["HOME"], ".config")
)
CONFIG_PATH = os.path.join(_CONFIG_HOME, "memento")
POLICY_PATH = os.path.join(CONFIG_PATH, "policy.json")
GOVERNANCE_MARKER = "governance.json"


def normalize_app(app):
    return (app or "").strip().lower()


def get_active_window():
    display = Xlib.display.Display()
    window = display.get_input_focus().focus
    if isinstance(window, Xlib.xobject.drawable.Window):
        wmclass = window.get_wm_class()
        if wmclass is None:
            window = window.query_tree().parent
            wmclass = window.get_wm_class()
        if wmclass is None:
            return "None"
        winclass = wmclass[1]
        return winclass
    else:
        return "None"


def _get_window_title(display, window):
    # Prefer the UTF-8 _NET_WM_NAME property, fall back to WM_NAME
    try:
        atom = display.intern_atom("_NET_WM_NAME")
        prop = window.get_full_property(atom, Xlib.X.AnyPropertyType)
        if prop is not None and prop.value:
            value = prop.value
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="ignore")
            return str(value)
    except Exception:
        pass
    try:
        name = window.get_wm_name()
        if name:
            if isinstance(name, bytes):
                return name.decode("utf-8", errors="ignore")
            return str(name)
    except Exception:
        pass
    return ""


def _get_window_display(display, window, monitors):
    # monitors is the mss monitor list (index 0 is the virtual screen,
    # physical monitors start at 1). When unavailable, assume monitor 1.
    if not monitors or len(monitors) <= 1:
        return 1
    try:
        root = display.screen().root
        coords = window.translate_coords(root, 0, 0)
        x = getattr(coords, "dst_x", getattr(coords, "x", None))
        y = getattr(coords, "dst_y", getattr(coords, "y", None))
        if x is None or y is None:
            return 1
        for i in range(1, len(monitors)):
            m = monitors[i]
            if (
                m["left"] <= x < m["left"] + m["width"]
                and m["top"] <= y < m["top"] + m["height"]
            ):
                return i
    except Exception:
        pass
    return 1


def get_active_window_info(monitors=None):
    """Return the normalized identity inputs of the active window.

    Keys: "app" (WM_CLASS instance, kept raw for storage/segment use),
    "title" (raw _NET_WM_NAME/WM_NAME), "display" (mss monitor index).
    Normalization for rule matching happens in memento.policy.
    """
    display = Xlib.display.Display()
    try:
        window = display.get_input_focus().focus
        if not isinstance(window, Xlib.xobject.drawable.Window):
            return {"app": "None", "title": "", "display": 1}

        wmclass = window.get_wm_class()
        if wmclass is None:
            parent = window.query_tree().parent
            if isinstance(parent, Xlib.xobject.drawable.Window):
                wmclass = parent.get_wm_class()
                window_for_title = parent
            else:
                window_for_title = window
        else:
            window_for_title = window

        app = wmclass[1] if wmclass is not None else "None"
        title = _get_window_title(display, window_for_title)
        display_id = _get_window_display(display, window, monitors)
        return {"app": app, "title": title, "display": display_id}
    except Exception:
        return {"app": "None", "title": "", "display": 1}
    finally:
        try:
            display.close()
        except Exception:
            pass


# check that a y coordinate is within a line with a tolerance of y_tol
def is_within_line(y, line, y_tol):
    return abs(y - line) < y_tol


# check if there is already a line of coordinate y
def line_exists(y, lines, y_tol):
    for line in lines.keys():
        if is_within_line(y, line, y_tol):
            return line
    return None


def same_sentence(last_word, x, x_tol):
    l_x = last_word["x"]
    l_w = last_word["w"]

    return not (x > l_x + l_w + x_tol)


class Recorder:
    _start: float
    _timestamp: int

    def __init__(self, filename):
        self.filename = filename
        # The container/stream is created lazily: a segment where every frame
        # is dropped by the capture policy must not contain a single pixel.
        self.output = None
        self.stream = None
        self._start = None
        self._timestamp = None

    def _ensure_stream(self):
        if self.output is not None:
            return
        self.output = av.open(self.filename, "w")
        self.stream = self.output.add_stream("h264", str(FPS))
        self.stream.height = RESOLUTION[1]
        self.stream.width = RESOLUTION[0]
        self.stream.bit_rate = 8500e1
        self._start = time.time()
        self._timestamp = None

    def start(self):
        self._start = time.time()

    async def next_timestamp(self):
        if self._timestamp is not None:
            self._timestamp += int(VIDEO_PTIME * VIDEO_CLOCK_RATE)
            wait = self._start + (self._timestamp / VIDEO_CLOCK_RATE) - time.time()
            await asyncio.sleep(wait)
        else:
            self._start = time.time()
            self._timestamp = 0
        return self._timestamp, VIDEO_TIME_BASE

    async def new_im(self, im):
        self._ensure_stream()
        pts, time_base = await self.next_timestamp()
        frame = av.video.frame.VideoFrame.from_ndarray(im, format="bgr24")
        frame.pts = pts
        frame.time_base = time_base
        packet = self.stream.encode(frame)
        if packet is not None:
            self.output.mux(packet)

    def stop(self):
        if self.output is None:
            # Nothing was recorded in this segment: keep an empty placeholder
            # so segment numbering and the timeline's mp4 count stay consistent
            with open(self.filename, "wb"):
                pass
            return
        packet = self.stream.encode(None)
        if packet is not None:
            self.output.mux(packet)
        self.output.close()
        self.output = None
        self.stream = None


def in_rect(rect, pos):
    x, y, w, h = rect
    return x <= pos[0] <= x + w and y <= pos[1] <= y + h


def rect_in_rect(rect1, rect2):
    x1, y1, w1, h1 = rect1
    x2, y2, w2, h2 = rect2
    return x1 >= x2 and y1 >= y2 and x1 + w1 <= x2 + w2 and y1 + h1 <= y2 + h2


def draw_results(res, frame):
    for entry in res:
        x = int(entry["x"])
        y = int(entry["y"])
        w = int(entry["w"])
        h = int(entry["h"])
        text = entry["text"]

        red_rect = np.ones((h, w, 3), dtype=np.uint8)
        red_rect[:, :, 2] = 0
        red_rect *= 200
        sub_img = frame[y : y + h, x : x + w]
        res = cv2.addWeighted(sub_img, 0.5, red_rect, 0.5, 1.0)
        if res is None:
            continue
        frame[y : y + h, x : x + w] = res
        frame = cv2.putText(
            frame,
            text,
            (x, y + 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 0, 0),
            2,
        )
    return frame


def _draw_results(res, image):
    for entry in res:
        x = int(entry["x"])
        y = int(entry["y"])
        w = int(entry["w"])
        h = int(entry["h"])
        text = entry["text"]

        cv2.rectangle(image, (x, y), (x + w, y + h), (0, 255, 0), 2)
        cv2.putText(
            image, text, (x, y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 200), 2
        )

    return image


def bb_center(entry):
    x = int(entry["x"])
    y = int(entry["y"])
    w = int(entry["w"])
    h = int(entry["h"])

    return np.array([x + w / 2, y + h / 2])


def init_paragraph():
    p = {}
    p["text"] = ""
    p["x"] = 100000
    p["y"] = 100000
    p["w"] = 0
    p["h"] = 0
    p["center"] = bb_center(p)
    return p


def update_paragraph(p, entry):
    p["text"] += " " + entry["text"]
    p["x"] = min(p["x"], entry["x"])
    p["y"] = min(p["y"], entry["y"])
    entry_x2 = entry["x"] + entry["w"]
    entry_y2 = entry["y"] + entry["h"]
    p_x2 = p["x"] + p["w"]
    p_y2 = p["y"] + p["h"]

    p["w"] = max(p_x2, entry_x2) - p["x"]
    p["h"] = max(p_y2, entry_y2) - p["y"]
    p["center"] = bb_center(p)
    return p


def make_paragraphs(res, tol=500):
    paragraphs = []
    for entry in res[1:]:
        center = bb_center(entry)
        merged = False
        for i, p in enumerate(paragraphs):
            if np.linalg.norm(p["center"] - center) < tol:
                paragraphs[i] = update_paragraph(p, entry)
                merged = True
                break
        if not merged:
            paragraphs.append(init_paragraph())
            paragraphs[-1] = update_paragraph(paragraphs[-1], entry)

    return paragraphs


def imgdiff(im1, im2):
    diff = np.bitwise_xor(im1, im2)
    return np.sum(diff) / (im1.shape[0] * im1.shape[1] * im1.shape[2])


def recording():
    res = os.popen("ps aux | grep memento-bg").read()
    # > 3 because ps and grep themselves are included in the output (2) and if memento-bg is running (and not waiting for starting prompt), there are at least two processes
    return len(res.splitlines()) > 3
