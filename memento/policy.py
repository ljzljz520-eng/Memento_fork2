"""Unified capture decision policy.

A single decision is made for every captured frame between the screenshot
grab and ``Recorder.new_im``. The decision and the frame's pixels are one
capture event: the decision is fully applied (drop / in-pixel redaction)
before the frame is encoded, OCR'd or pushed to a cross-process queue.

Rules are user-defined in ``$XDG_CONFIG_HOME/memento/policy.json``.
"""

import datetime
import json
import os
import re
from dataclasses import dataclass, field

import cv2
import numpy as np

import memento.utils as utils

POLICY_VERSION = 1

DEFAULT_POLICY = {
    "version": POLICY_VERSION,
    "default_action": utils.DECISION_ALLOW,
    "redaction": "black",
    "regions": [],
    "rules": [],
    "egress": {"allowed_fields": ["id", "time"]},
}

_ACTIONS = (utils.DECISION_ALLOW, utils.DECISION_DROP, utils.DECISION_REDACT)


@dataclass
class CaptureContext:
    """Normalized identity inputs of one capture event."""

    app: str
    title: str
    display: int
    moment: datetime.datetime


@dataclass
class CaptureDecision:
    """Outcome of evaluating the policy for one capture event."""

    action: str
    rule_id: str = None
    reason: str = None
    # Resolution-space rectangles [x, y, w, h] whose pixels must be masked.
    # A single full-frame rect means the whole frame is redacted.
    regions: list = field(default_factory=list)

    @property
    def allowed(self):
        return self.action == utils.DECISION_ALLOW

    @property
    def dropped(self):
        return self.action == utils.DECISION_DROP

    @property
    def redacted(self):
        return self.action == utils.DECISION_REDACT

    @staticmethod
    def allow():
        return CaptureDecision(utils.DECISION_ALLOW)


def _parse_hhmm(value):
    parts = value.split(":")
    return int(parts[0]) * 60 + int(parts[1])


class _Rule:
    def __init__(self, spec):
        self.id = str(spec.get("id", "rule"))
        action = spec.get("action", utils.DECISION_ALLOW)
        if action not in _ACTIONS:
            raise ValueError("invalid action %s for rule %s" % (action, self.id))
        self.action = action

        self.apps = [utils.normalize_app(a) for a in spec.get("apps", [])]
        self.title_contains = [
            s.lower() for s in spec.get("title_contains", []) if s
        ]
        self.title_regex = None
        if spec.get("title_regex"):
            self.title_regex = re.compile(spec["title_regex"], re.IGNORECASE)
        self.displays = set(spec.get("displays", []))

        self.time = spec.get("time")
        self.weekdays = None
        self.start_min = None
        self.end_min = None
        if self.time:
            if self.time.get("weekdays") is not None:
                self.weekdays = set(self.time["weekdays"])
            if self.time.get("start") is not None:
                self.start_min = _parse_hhmm(self.time["start"])
                self.end_min = _parse_hhmm(
                    self.time.get("end", self.time["start"])
                )

        self.regions = [
            (int(x), int(y), int(w), int(h))
            for x, y, w, h in spec.get("regions", [])
        ]

    def _time_matches(self, moment):
        if self.time is None:
            return True
        weekday = moment.weekday()
        if self.start_min is None:
            # weekdays only: whole-day matching
            return self.weekdays is None or weekday in self.weekdays

        minutes = moment.hour * 60 + moment.minute
        if self.start_min <= self.end_min:
            in_window = self.start_min <= minutes < self.end_min
            day_ok = self.weekdays is None or weekday in self.weekdays
        else:
            # Window crosses midnight: early-morning moments belong to the
            # rule window that started on the previous weekday.
            in_window = minutes >= self.start_min or minutes < self.end_min
            if self.weekdays is None:
                day_ok = True
            elif minutes < self.end_min:
                day_ok = ((weekday - 1) % 7) in self.weekdays
            else:
                day_ok = weekday in self.weekdays
        return in_window and day_ok

    def matches(self, ctx):
        app = utils.normalize_app(ctx.app)
        title = (ctx.title or "").lower()

        if self.apps and app not in self.apps:
            return False
        if self.title_contains and not any(s in title for s in self.title_contains):
            return False
        if self.title_regex is not None and not self.title_regex.search(ctx.title or ""):
            return False
        if self.displays and ctx.display not in self.displays:
            return False
        if not self._time_matches(ctx.moment):
            return False
        return True


def is_governed(cache_path=utils.CACHE_PATH):
    """Old recordings (no governance marker) keep their legacy behavior."""
    return os.path.exists(os.path.join(cache_path, utils.GOVERNANCE_MARKER))


def write_governance_marker(policy, cache_path=utils.CACHE_PATH):
    os.makedirs(cache_path, exist_ok=True)
    with open(os.path.join(cache_path, utils.GOVERNANCE_MARKER), "w") as f:
        json.dump({"version": POLICY_VERSION, "policy": policy.config}, f)


class CapturePolicy:
    def __init__(self, config, governed=True):
        self.config = config
        self.governed = governed
        self.default_action = config.get(
            "default_action", utils.DECISION_ALLOW
        )
        if self.default_action not in _ACTIONS:
            self.default_action = utils.DECISION_ALLOW
        self.redaction_mode = config.get("redaction", "black")
        self.always_regions = [
            (int(x), int(y), int(w), int(h))
            for x, y, w, h in config.get("regions", [])
        ]
        self.rules = []
        for spec in config.get("rules", []):
            try:
                self.rules.append(_Rule(spec))
            except Exception as e:
                print("Ignoring invalid capture rule:", e)

    @classmethod
    def load(cls, path=None, governed=True):
        path = path or utils.POLICY_PATH
        if not governed:
            return cls(DEFAULT_POLICY, governed=False)
        config = dict(DEFAULT_POLICY)
        try:
            if not os.path.exists(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    json.dump(DEFAULT_POLICY, f, indent=2)
            with open(path) as f:
                user_config = json.load(f)
            config.update({k: v for k, v in user_config.items() if v is not None})
        except Exception as e:
            print("Could not load capture policy, allowing everything:", e)
            config = dict(DEFAULT_POLICY)
        return cls(config, governed=True)

    def decide(self, ctx):
        if not self.governed:
            return CaptureDecision.allow()

        for rule in self.rules:
            if not rule.matches(ctx):
                continue
            regions = list(rule.regions)
            if rule.action == utils.DECISION_REDACT:
                regions += self.always_regions
                regions = _normalize_regions(regions)
                if not regions:
                    # Redact rule without regions masks the whole frame
                    regions = [(0, 0, utils.RESOLUTION[0], utils.RESOLUTION[1])]
            return CaptureDecision(
                action=rule.action,
                rule_id=rule.id,
                reason="rule:" + rule.id,
                regions=regions,
            )

        # User-defined regions are always masked, independent of any rule
        if self.always_regions:
            return CaptureDecision(
                action=utils.DECISION_REDACT,
                rule_id="user-regions",
                reason="rule:user-regions",
                regions=_normalize_regions(self.always_regions),
            )

        regions = []
        if self.default_action == utils.DECISION_REDACT:
            # A redact default without regions masks the whole frame
            regions = [(0, 0, utils.RESOLUTION[0], utils.RESOLUTION[1])]
        return CaptureDecision(self.default_action, regions=regions)


def _normalize_regions(regions):
    seen = set()
    out = []
    for rect in regions:
        x, y, w, h = rect
        if w <= 0 or h <= 0:
            continue
        rect = (x, y, w, h)
        if rect not in seen:
            seen.add(rect)
            out.append(rect)
    return out


def apply_redaction(im, regions, mode="black"):
    """Mask the given resolution-space rects on the raw frame, in place.

    This MUST run before encoding, OCR and cross-process queueing.
    Black fill is the default as it is provably OCR-proof.
    """
    height, width = im.shape[:2]
    for x, y, w, h in regions:
        x1 = max(0, min(width, int(x)))
        y1 = max(0, min(height, int(y)))
        x2 = max(0, min(width, int(x) + int(w)))
        y2 = max(0, min(height, int(y) + int(h)))
        if x2 <= x1 or y2 <= y1:
            continue
        if mode == "blur":
            roi = im[y1:y2, x1:x2]
            kernel = max(31, (min(w, h) // 2) * 2 + 1)
            im[y1:y2, x1:x2] = cv2.GaussianBlur(roi, (kernel, kernel), 0)
        else:
            im[y1:y2, x1:x2] = 0
    return im
