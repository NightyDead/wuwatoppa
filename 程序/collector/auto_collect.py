"""Match ready chests beside a moving Wuwa Tappo pet. Ctrl+Alt stops the worker."""

from dataclasses import dataclass, replace
from pathlib import Path
import argparse
import ctypes
from ctypes import wintypes
import importlib.util
import os
import math
import sys
import threading
import time

import numpy as np
from PIL import Image, ImageGrab, ImageDraw
from monitoring import MonitorControl, parse_hours, prompt_hours, run_monitor
from claim_history import RunRecord
from region_capture import grab_region, GdiRegionCapture
from ui_events import UiEvents


ASSETS = Path(__file__).resolve().parent / 'assets'
VERSION = '5.0'


_clicker = None


def clicker_module():
    """Load the sibling F15 clicker once so gap clicks share the stop switch."""
    global _clicker
    if _clicker is None:
        path = Path(__file__).resolve().parent.parent / 'clicker' / 'macro.py'
        if not path.is_file():
            raise RuntimeError(f'缺少连点器脚本，无法执行间隙连点：{path}')
        spec = importlib.util.spec_from_file_location('_pet_gap_clicker', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _clicker = module
    return _clicker


def run_gap_clicks(keyboard, count, interval, stopped, wait, progress=None):
    """Send one burst of F15 presses between rounds; returns (sent, reason)."""
    reason = {}
    sent = clicker_module().run_macro(count, interval, keyboard.send_f15, stopped, wait,
                                      progress=progress, reason=reason)
    return sent, reason.get('value')


def attempt_claim(detector, desktop, stopped, wait, record=None, preferred_slot=None,
                  observer=None):
    if stopped():
        return 'stopped'
    observer = observer if observer is not None else PetObserver(detector, desktop)
    first_frame = observer.locate(stopped)
    if stopped():
        return 'stopped'
    if first_frame is None:
        return 'moving'
    first = first_frame.chests
    if not first:
        return 'waiting'
    owned = [c for c in first if desktop.owned_by_pet(round(c.x), round(c.y))]
    if not owned:
        observer.clear_cache()
        return 'other-window'
    if preferred_slot is not None:
        owned = [candidate for candidate in owned if candidate.slot == preferred_slot]
        if not owned:
            return 'waiting'
    target = owned[0]
    observer.target = target
    wait(0.15)
    if stopped():
        return 'stopped'
    second_frame = observer.recheck(target)
    if stopped():
        return 'stopped'
    if second_frame is None:
        return 'moving'
    second = second_frame.chests
    matching = [c for c in second if c.slot == target.slot and
                (c.x-target.x)**2 + (c.y-target.y)**2 <= 3**2]
    if not matching:
        return 'moving'
    current = max(matching, key=lambda c: c.score)
    x, y = round(current.x), round(current.y)
    if not desktop.owned_by_pet(x, y):
        observer.clear_cache()
        return 'other-window'
    if stopped():
        return 'stopped'
    if record is not None:
        record('before', second_frame.image, current, second_frame.origin)
    if desktop.click(x, y) is False:
        return 'busy'
    wait(0.8)
    if stopped():
        return 'stopped'
    after_frame = observer.recheck(current, max_anchor_shift=3)
    if stopped():
        return 'stopped'
    if after_frame is None:
        return 'unknown'
    if record is not None:
        record('after', after_frame.image, current, after_frame.origin)
    # Disappearance behind a window must not be treated as a collected chest.
    if not after_frame.anchor_visible:
        return 'unknown'
    if any(chest.slot == target.slot for chest in after_frame.chests):
        return 'unverified'
    return 'changed'


@dataclass(frozen=True)
class Chest:
    x: float
    y: float
    score: float
    scale: float
    slot: int
    anchor_x: float = None
    anchor_y: float = None
    variant: int = None


def shift_chest(chest, dx, dy):
    return replace(chest, x=chest.x+dx, y=chest.y+dy,
                   anchor_x=None if chest.anchor_x is None else chest.anchor_x+dx,
                   anchor_y=None if chest.anchor_y is None else chest.anchor_y+dy)


@dataclass
class Observation:
    image: Image.Image
    origin: tuple
    chests: list
    anchor_visible: bool = False


class PetObserver:
    """Track in physical screen coordinates while capturing only the needed ROI."""
    def __init__(self, detector, desktop):
        self.detector, self.desktop = detector, desktop
        self.target = None
        self.layout = None

    def clear_cache(self):
        self.target = None
        self.layout = None
        self.detector.clear_cache()

    def recheck(self, target, max_anchor_shift=None):
        bounds = self.desktop.bounds()
        if self.layout is not None and bounds != self.layout:
            self.clear_cache()
            return None
        box = self.detector.capture_region(target, bounds)
        if box[0] >= box[2] or box[1] >= box[3]:
            return None
        image, origin = self.desktop.capture(box)
        if bounds != self.desktop.bounds():
            self.clear_cache()
            return None
        local = shift_chest(target, -origin[0], -origin[1])
        found, visible = self.detector.inspect_near(image, local, max_anchor_shift)
        physical = [shift_chest(c, *origin) for c in found]
        if visible:
            self.target = physical[0] if physical else target
        return Observation(image, origin, physical, visible)

    def locate(self, stopped):
        if stopped():
            return None
        bounds = self.desktop.bounds()
        if self.target is not None and self.layout == bounds:
            frame = self.recheck(self.target)
            if stopped():
                return None
            if frame is not None and frame.anchor_visible:
                if len(frame.chests) < 2:
                    # 气泡与状态栏图标比例不一致时，局部复查可能只认回一个；
                    # 在同一张局部截图上按所有尺寸重搜一次，别把还在的宝箱当成已领走。
                    local = shift_chest(self.target, -frame.origin[0], -frame.origin[1])
                    found = self.detector.rescan_region(frame.image, local)
                    if len(found) > len(frame.chests):
                        frame.chests = [shift_chest(c, *frame.origin) for c in found]
                        self.target = frame.chests[0]
                return frame
        self.clear_cache()
        if stopped():
            return None
        box = self.desktop.pet_monitor_bounds()
        if stopped():
            return None
        image, origin = self.desktop.capture(box)
        if bounds != self.desktop.bounds():
            return None
        found = self.detector.find(image)
        physical = [shift_chest(c, *origin) for c in found]
        self.layout = bounds
        if physical:
            self.target = physical[0]
        return Observation(image, origin, physical, bool(physical))


def _gray(image):
    return np.asarray(image.convert('L'), dtype=np.float64) / 255.0


def display_scale():
    """Windows 缩放比例（100% = 1.0），取不到时当 1.0。"""
    try:
        return ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100
    except (AttributeError, OSError):
        return 1.0


def _integral(array, squared=False):
    table = np.zeros((array.shape[0] + 1, array.shape[1] + 1), dtype=np.float64)
    pixels = table[1:, 1:]
    if squared:
        np.square(array, out=pixels)
    else:
        pixels[:] = array
    pixels.cumsum(axis=0, out=pixels)
    pixels.cumsum(axis=1, out=pixels)
    return table


def _sums(integral, height, width):
    sums = integral[height:, width:] - integral[:-height, width:]
    sums -= integral[height:, :-width]
    sums += integral[:-height, :-width]
    return sums


def _fast_len(value):
    """Smallest 5-smooth FFT length >= value, so the transform stays cheap."""
    if value <= 16:
        return value
    best = 1 << (value - 1).bit_length()
    five = 1
    while five < best:
        three_five = five
        while three_five < best:
            size = three_five
            while size < value:
                size *= 2
            if size < best:
                best = size
            three_five *= 3
        five *= 5
    return best


class _NccFrame:
    """Reuse image transforms across templates within a single search."""
    def __init__(self, image):
        self.image = image
        self.shape = tuple(_fast_len(length) for length in image.shape)
        self.spectrum = None
        self.integrals = None

    def correlate(self, kernel):
        height, width = kernel.shape
        rows, cols = self.image.shape[0] - height + 1, self.image.shape[1] - width + 1
        if rows < 1 or cols < 1:
            return np.empty((0, 0))
        # Valid shifts never wrap, so the FFT only needs to cover the image.
        if self.spectrum is None:
            self.spectrum = np.fft.rfft2(self.image, s=self.shape, axes=(-2, -1))
        product = np.fft.rfft2(kernel, s=self.shape, axes=(-2, -1))
        np.conjugate(product, out=product)
        product *= self.spectrum
        result = np.fft.irfft2(product, s=self.shape, axes=(-2, -1))
        return np.ascontiguousarray(result[:rows, :cols])

    def variance(self, height, width):
        if self.integrals is None:
            self.integrals = _integral(self.image), _integral(self.image, squared=True)
        sums = _sums(self.integrals[0], height, width)
        np.square(sums, out=sums)
        sums /= height * width
        variance = _sums(self.integrals[1], height, width)
        variance -= sums
        return variance


def _correlate(image, kernel):
    return _NccFrame(image).correlate(kernel)


def _ncc(image, template, frame=None):
    height, width = template.shape
    if image.shape[0] < height or image.shape[1] < width:
        return np.empty((0, 0))
    if frame is None:
        frame = _NccFrame(image)
    centered = template - template.mean()
    energy = (centered ** 2).sum()
    scores = frame.correlate(centered)
    variance = frame.variance(height, width)
    valid = variance > 1e-5
    np.maximum(variance, 0, out=variance)
    variance *= energy
    np.sqrt(variance, out=variance)
    valid &= variance > 1e-8
    np.divide(scores, variance, out=scores, where=valid)
    scores[~valid] = -1.0
    return np.clip(scores, -1.0, 1.0, out=scores)


class ChestDetector:
    def __init__(self, scales=(0.5, 0.55, 0.6, 0.65, 0.7, 0.75,
                               0.8, 1.0, 1.25, 1.5, 2.0),
                 anchor_threshold=0.88, chest_threshold=0.88,
                 anchor_floor=0.75, chest_fallback=0.92):
        self.anchor_threshold = anchor_threshold
        self.chest_threshold = chest_threshold
        # 桌宠缩小时状态栏的“≡”只剩十来个像素，锚点分数常常落到 0.75～0.87；
        # 允许用高分宝箱气泡兼底，否则明明看得见的宝箱会被当成不存在。
        self.anchor_floor = anchor_floor
        self.chest_fallback = chest_fallback
        self.variants = []
        self._cached = None
        # 状态栏图标与宝箱气泡未必按同一比例缩小（桌宠大小可调），
        # 这里把每种尺寸的宝箱图样收一份，供当前尺寸没命中时补搜。
        self.chest_catalog = []
        # Themes have different menu artwork and chest offsets. Keep their
        # anchors, chest templates and geometry paired instead of lowering NCC.
        themes = (
            ('anchor.png', ('chest.png', 'chest_lower.png'),
             ((26, -46), (19, -15))),
            ('anchor_white.png', ('chest_white.png',),
             ((27.5, -54.5), (20.5, -23.5))),
        )
        for anchor_name, chest_names, offsets in themes:
            with Image.open(ASSETS / anchor_name) as source:
                anchor = source.convert('RGB')
            templates = []
            for name in chest_names:
                with Image.open(ASSETS / name) as source:
                    templates.append(source.convert('RGB'))
            for scale in scales:
                def resize(image):
                    size = (max(2, round(image.width * scale)),
                            max(2, round(image.height * scale)))
                    return _gray(image.resize(size, Image.Resampling.BICUBIC))
                chest_gray = [resize(t) for t in templates]
                self.variants.append((scale, resize(anchor), chest_gray, offsets))
                self.chest_catalog.append((scale, chest_gray))
        self.legacy_count = len(self.variants)
        self.calibrated = []
        self.coarse = []
        with Image.open(ASSETS / 'anchor_calibrated.png') as source:
            anchor = source.convert('RGB')
        with Image.open(ASSETS / 'chest_calibrated.png') as source:
            chest = source.convert('RGB')
        # These scales refer to the supplied software-100% screenshot, unlike
        # the historical templates. Fine scales are searched only in small ROIs.
        for percent in range(50, 201, 5):
            scale = percent / 100
            def resize_calibrated(image):
                return _gray(image.resize((round(image.width*scale), round(image.height*scale)),
                                          Image.Resampling.BICUBIC))
            variant = len(self.variants)
            chest_gray = [resize_calibrated(chest)]
            self.variants.append((scale, resize_calibrated(anchor), chest_gray,
                                  ((45, -102), (33, -42))))
            self.chest_catalog.append((scale, chest_gray))
            self.calibrated.append(variant)
            if percent % 25 == 0:
                self.coarse.append(variant)

    def anchor_present(self, image):
        frame = _NccFrame(_gray(image))
        for _, anchor, _, _ in self.variants:
            scores = _ncc(frame.image, anchor, frame)
            if scores.size and scores.max() >= self.anchor_threshold:
                return True
        return False

    def clear_cache(self):
        self._cached = None

    def _region(self, image, target, extra=0):
        return self.capture_region(target, (0, 0, image.width, image.height), extra)

    def capture_region(self, target, bounds, extra=0):
        scale, anchor, templates, offsets = self.variants[target.variant]
        # 气泡与状态栏图标未必同比例，固定偏移会有一二十像素误差；
        # 边距太小会把宝箱边缘裁掉，局部复查时明明在屏幕上却认不出来。
        margin = max(12, math.ceil(36*scale)) + extra
        ax, ay = target.anchor_x, target.anchor_y
        return (max(bounds[0], math.floor(ax + min(0, *(dx*scale for dx, _ in offsets)) - margin)),
                max(bounds[1], math.floor(ay + min(0, *(dy*scale for _, dy in offsets)) - margin)),
                min(bounds[2], math.ceil(ax + max(anchor.shape[1],
                    *(dx*scale for dx, _ in offsets)) + margin)),
                min(bounds[3], math.ceil(ay + max(anchor.shape[0],
                    *(dy*scale for _, dy in offsets)) + margin)))

    def inspect_near(self, image, target, max_anchor_shift=None):
        """Reobserve one pet with its known theme/scale; never search globally."""
        if target.variant is None or target.anchor_x is None or target.anchor_y is None:
            return [], False
        scale = target.scale
        ax, ay = target.anchor_x, target.anchor_y
        box = self._region(image, target)
        if box[0] >= box[2] or box[1] >= box[3]:
            return [], False
        shift = max(6, 12 * scale) if max_anchor_shift is None else max_anchor_shift
        found, visible = self._scan(image, [(target.variant, self.variants[target.variant])],
                                    box, anchor_hint=(ax, ay, shift))
        if visible:
            # Keep tracking the menu even when both chests are cooling down.
            self._cached = (image.size, found[0] if found else target)
        return found, visible

    def find(self, image):
        if self._cached is not None and self._cached[0] == image.size:
            found, visible = self.inspect_near(image, self._cached[1])
            if visible and len(found) < 2:
                # A small size adjustment can leave the old menu recognizable
                # while moving the chests. Try other scales locally first.
                extra = self.rescan_region(image, self._cached[1])
                if len(extra) > len(found):
                    found = extra
                    self._cached = (image.size, found[0])
            if visible:
                return found
        # Movement, scale/theme changes and display changes invalidate the hint.
        self.clear_cache()
        frame = _NccFrame(_gray(image))
        found = self._find_calibrated(image, frame)
        if not found:
            found = self._scan_scales(image, frame)
        if found:
            self._cached = (image.size, found[0])
        return found

    def rescan_region(self, image, target, extra=40):
        """Re-scan the menu's neighbourhood at every scale."""
        found, _ = self._scan(image, enumerate(self.variants),
                              self._region(image, target,
                                           extra=math.ceil(extra*target.scale)))
        return found

    def _scan_scales(self, image, frame, enough=2):
        """Walk the historical theme/scale table and stop at the first pair,
        so a full-frame miss costs seconds instead of a minute."""
        candidates = []
        for index in range(self.legacy_count):
            found, _ = self._scan(image, [(index, self.variants[index])], frame=frame)
            candidates.extend(found)
            unique = self._unique(candidates)
            if len(unique) >= enough:
                return unique
        return self._unique(candidates)

    def _find_calibrated(self, image, frame):
        """Seven coarse full-frame anchors, then fine scales near each proposal."""
        candidates = []
        for index in self.coarse:
            scale, anchor, _, _ = self.variants[index]
            scores = _ncc(frame.image, anchor, frame)
            if not scores.size:
                continue
            nearby = [(i, self.variants[i]) for i in self.calibrated
                      if abs(self.variants[i][0]-scale) <= 0.151]
            for _ in range(6):
                ay, ax = np.unravel_index(np.argmax(scores), scores.shape)
                # 粗尺度与真实大小差半档时分数会掉到 0.6 左右，
                # 这里只当提案，细尺度还在 ±15% 内重扫。
                if scores[ay, ax] < 0.62:
                    break
                radius = max(5, round(12*scale))
                scores[max(0, ay-radius):ay+radius+1,
                       max(0, ax-radius):ax+radius+1] = -1
                largest = nearby[-1][0]
                hint = Chest(0, 0, 0, self.variants[largest][0], 0, ax, ay, largest)
                found, _ = self._scan(image, nearby, self._region(image, hint),
                                       anchor_hint=(ax, ay, max(8, 12*scale)))
                candidates.extend(found)
            if len(self._unique(candidates)) >= 2:
                break
        return self._unique(candidates)

    def _scan(self, image, variants, box=None, anchor_hint=None, frame=None):
        left_origin, top_origin = (0, 0) if box is None else box[:2]
        gray = frame.image if frame is not None else _gray(image if box is None else image.crop(box))
        xmin = ymin = 0
        xmax, ymax = gray.shape[1], gray.shape[0]
        if anchor_hint is not None:
            hx, hy, shift = anchor_hint
            xmin = max(0, math.ceil(hx-left_origin-shift))
            xmax = min(xmax, math.floor(hx-left_origin+shift)+1)
            ymin = max(0, math.ceil(hy-top_origin-shift))
            ymax = min(ymax, math.floor(hy-top_origin+shift)+1)
            if xmin >= xmax or ymin >= ymax:
                return [], False
            variants = tuple(variants)
            if not variants:
                return [], False
            # Include the full template at each allowed anchor position.
            # Chest matching still uses the original, wider region.
            height = max(anchor.shape[0] for _, (_, anchor, _, _) in variants)
            width = max(anchor.shape[1] for _, (_, anchor, _, _) in variants)
            frame = _NccFrame(gray[ymin:ymax+height-1, xmin:xmax+width-1])
        elif frame is None:
            frame = _NccFrame(gray)

        candidates = []
        visible = False
        for variant, (scale, anchor, templates, offsets) in variants:
            scores = _ncc(frame.image, anchor, frame)[:ymax-ymin, :xmax-xmin]
            if not scores.size:
                continue
            for _ in range(6):
                ay, ax = np.unravel_index(np.argmax(scores), scores.shape)
                anchor_score = float(scores[ay, ax])
                if anchor_score < self.anchor_floor:
                    break
                visible = True
                strict = anchor_score >= self.anchor_threshold
                radius = max(4, round(10 * scale))
                scores[max(0, ay-radius):ay+radius+1,
                       max(0, ax-radius):ax+radius+1] = -1
                ay += ymin
                ax += xmin
                # Offsets are relative to the stable status-bar menu icon.
                for slot, (dx, dy) in enumerate(offsets):
                    cx, cy = ax + dx * scale, ay + dy * scale
                    anchor_pos = (left_origin + ax, top_origin + ay)
                    best = self._match_chest(gray, templates, cx, cy, scale, strict,
                                             (left_origin, top_origin), anchor_pos,
                                             variant, slot)
                    if best is None:
                        # 气泡与状态栏图标可能不是同一比例：当前尺寸没命中时，
                        # 再用相近尺寸和其他主题的宝箱图样补搜一次；
                        # 比例差得多时预测位置会偏几十像素，窗口也要放宽。
                        best = self._match_chest(gray, self._nearby_templates(scale),
                                                 cx, cy, scale, strict,
                                                 (left_origin, top_origin), anchor_pos,
                                                 variant, slot,
                                                 padding=round(28*scale))
                    if best is not None:
                        candidates.append(best)
        return self._unique(candidates), visible

    def _nearby_templates(self, scale):
        """Chest artwork at sizes near the menu's, taken from every theme."""
        low, high = scale * 0.75, scale * 1.3
        pool = []
        for template_scale, templates in self.chest_catalog:
            if low <= template_scale <= high:
                pool.extend(templates)
        return pool

    def _match_chest(self, gray, templates, cx, cy, scale, strict, origin, anchor,
                     variant, slot, padding=None):
        best = None
        if padding is None:
            padding = round(8 * scale)
        searches = []
        for template in templates:
            th, tw = template.shape
            left = max(0, round(cx - tw / 2) - padding)
            top = max(0, round(cy - th / 2) - padding)
            right = min(gray.shape[1], round(cx + tw / 2) + padding)
            bottom = min(gray.shape[0], round(cy + th / 2) + padding)
            if right - left >= tw and bottom - top >= th:
                searches.append((template, left, top, right, bottom))
        if not searches:
            return None

        # Nearby templates use overlapping boxes. Reuse one image transform,
        # then keep each template's original valid positions and search order.
        x0 = min(left for _, left, _, _, _ in searches)
        y0 = min(top for _, _, top, _, _ in searches)
        x1 = max(right for _, _, _, right, _ in searches)
        y1 = max(bottom for _, _, _, _, bottom in searches)
        frame = _NccFrame(gray[y0:y1, x0:x1])
        for template, left, top, right, bottom in searches:
            th, tw = template.shape
            scores = _ncc(frame.image, template, frame)
            local = scores[top-y0:bottom-y0-th+1, left-x0:right-x0-tw+1]
            ly, lx = np.unravel_index(np.argmax(local), local.shape)
            score = float(local[ly, lx])
            if score >= self.chest_threshold and (strict or score >= self.chest_fallback):
                candidate = Chest(origin[0] + left + lx + tw / 2,
                                  origin[1] + top + ly + th / 2,
                                  score, scale, slot, anchor[0], anchor[1], variant)
                if best is None or candidate.score > best.score:
                    best = candidate
        return best

    @staticmethod
    def _unique(candidates):
        unique = []
        for candidate in sorted(candidates, key=lambda c: c.score, reverse=True):
            if all((candidate.x - old.x) ** 2 + (candidate.y - old.y) ** 2 >
                   (6 * max(candidate.scale, old.scale)) ** 2 for old in unique):
                unique.append(candidate)
        return unique


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD),
                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD),
                ('dwExtraInfo', ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG),
                ('mouseData', wintypes.DWORD), ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [('uMsg', wintypes.DWORD), ('wParamL', wintypes.WORD),
                ('wParamH', wintypes.WORD)]


class INPUTUNION(ctypes.Union):
    _fields_ = [('ki', KEYBDINPUT), ('mi', MOUSEINPUT), ('hi', HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ('data',)
    _fields_ = [('type', wintypes.DWORD), ('data', INPUTUNION)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT),
                ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD)]


class WindowsDesktop:
    def __init__(self):
        self.wait = time.sleep
        self.restore_pointer = True
        self.last_click_info = None
        self.input_blocked = False
        self.stop_requested = None
        self.user32 = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        api = self.user32
        api.GetAsyncKeyState.argtypes = [ctypes.c_int]
        api.GetAsyncKeyState.restype = wintypes.SHORT
        api.GetSystemMetrics.argtypes = [ctypes.c_int]
        api.GetSystemMetrics.restype = ctypes.c_int
        api.WindowFromPoint.argtypes = [wintypes.POINT]
        api.WindowFromPoint.restype = wintypes.HWND
        api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        api.GetWindowThreadProcessId.restype = wintypes.DWORD
        api.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        api.SetCursorPos.restype = wintypes.BOOL
        api.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        api.GetCursorPos.restype = wintypes.BOOL
        api.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        api.SendInput.restype = wintypes.UINT
        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self.advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
        self.advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                  ctypes.POINTER(wintypes.HANDLE)]
        self.advapi32.OpenProcessToken.restype = wintypes.BOOL
        self.advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)]
        self.advapi32.GetTokenInformation.restype = wintypes.BOOL
        api.IsWindowVisible.argtypes = [wintypes.HWND]
        api.IsWindowVisible.restype = wintypes.BOOL
        api.IsIconic.argtypes = [wintypes.HWND]
        api.IsIconic.restype = wintypes.BOOL
        api.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        api.GetWindowTextW.restype = ctypes.c_int
        api.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        api.MonitorFromWindow.restype = wintypes.HANDLE
        api.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
        api.GetMonitorInfoW.restype = wintypes.BOOL
        self.kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel32.CreateMutexW.restype = wintypes.HANDLE
        expected_size = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        if ctypes.sizeof(INPUT) != expected_size:
            raise RuntimeError('Windows INPUT 结构大小不正确。')

    def lock(self):
        ctypes.set_last_error(0)
        handle = self.kernel32.CreateMutexW(None, False, 'Local\\WuwaTappoAutoCollect')
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            self.kernel32.CloseHandle(handle)
            raise RuntimeError('自动领取已经在运行。请先按 Ctrl+Alt 停止旧的实例。')
        return handle

    def stop_pressed(self):
        return bool(self.user32.GetAsyncKeyState(0x11) & 0x8000 and
                    self.user32.GetAsyncKeyState(0x12) & 0x8000)

    def should_stop(self):
        requested = getattr(self, 'stop_requested', None)
        return self.stop_pressed() or (requested is not None and requested())

    def bounds(self):
        left, top = self.user32.GetSystemMetrics(76), self.user32.GetSystemMetrics(77)
        return (left, top, left+self.user32.GetSystemMetrics(78),
                top+self.user32.GetSystemMetrics(79))

    def capture(self, box=None):
        bounds = self.bounds()
        if box is None:
            return ImageGrab.grab(all_screens=True).convert('RGB'), bounds[:2]
        clipped = (max(bounds[0], box[0]), max(bounds[1], box[1]),
                   min(bounds[2], box[2]), min(bounds[3], box[3]))
        if clipped[0] >= clipped[2] or clipped[1] >= clipped[3]:
            raise ValueError('局部截图范围超出屏幕。')
        return grab_region(clipped), clipped[:2]

    def process_name(self, pid):
        """Image name of a process, or '' when Windows refuses to tell."""
        process = self.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return ''
        try:
            size = wintypes.DWORD(32768)
            name = ctypes.create_unicode_buffer(size.value)
            if not self.kernel32.QueryFullProcessImageNameW(process, 0, name, ctypes.byref(size)):
                return ''
            return os.path.basename(name.value)
        finally:
            self.kernel32.CloseHandle(process)

    def owned_by_pet(self, x, y):
        hwnd = self.user32.WindowFromPoint(wintypes.POINT(x, y))
        if not hwnd:
            return False
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return self.process_name(pid.value).casefold() == 'wuwatappo.exe'

    def pet_windows(self):
        """Visible top-level windows and their wuwatappo.exe process ids."""
        windows = []
        matches = {}

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _):
            if not self.user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value and pid.value not in matches:
                matches[pid.value] = self.process_name(pid.value).casefold() == 'wuwatappo.exe'
            if matches.get(pid.value):
                windows.append((hwnd, pid.value))
            return True

        self.user32.EnumWindows(visit, 0)
        return windows

    def pet_processes(self):
        return sorted({pid for _, pid in self.pet_windows()})

    def pet_monitor_bounds(self):
        """Locate the pet's main window, with a fallback for unknown window titles."""
        main_monitors = set()
        monitors = set()
        title = ctypes.create_unicode_buffer(256)
        for hwnd, _ in self.pet_windows():
            if self.user32.IsIconic(hwnd):
                continue
            monitor = self.user32.MonitorFromWindow(hwnd, 0)  # MONITOR_DEFAULTTONULL
            if not monitor:
                continue
            monitors.add(monitor)
            # Helper windows can stay on the original display when the pet moves.
            if (self.user32.GetWindowTextW(hwnd, title, len(title))
                    and title.value.strip().casefold() == 'wuwa tappo'):
                main_monitors.add(monitor)
        monitors = main_monitors or monitors
        if len(monitors) != 1:
            return None
        info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        if not self.user32.GetMonitorInfoW(monitors.pop(), ctypes.byref(info)):
            return None
        rect = info.rcMonitor
        return rect.left, rect.top, rect.right, rect.bottom

    def elevated(self, pid=None):
        """Whether this helper (or ``pid``) runs elevated; None when unknown."""
        process = (self.kernel32.GetCurrentProcess() if pid is None
                   else self.kernel32.OpenProcess(0x1000, False, pid))
        if not process:
            return None
        token = wintypes.HANDLE()
        try:
            if not self.advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
                return None
            state = wintypes.DWORD()
            size = wintypes.DWORD()
            if not self.advapi32.GetTokenInformation(token, 20, ctypes.byref(state),
                                                     ctypes.sizeof(state), ctypes.byref(size)):
                return None
            return bool(state.value)
        finally:
            if token:
                self.kernel32.CloseHandle(token)
            if pid is not None:
                self.kernel32.CloseHandle(process)

    def pet_elevated(self):
        """Elevation of the running pet, or None when it is not on screen."""
        for pid in self.pet_processes():
            state = self.elevated(pid)
            if state is not None:
                return state
        return None

    def click(self, x, y):
        # Avoid injecting into a physical mouse drag.
        if self.user32.GetAsyncKeyState(0x01) & 0x8000 or \
                self.user32.GetAsyncKeyState(0x02) & 0x8000:
            return False
        if self.should_stop() or not self.owned_by_pet(x, y):
            return False
        old = wintypes.POINT()
        if not self.user32.GetCursorPos(ctypes.byref(old)):
            raise ctypes.WinError(ctypes.get_last_error())
        left, top = self.user32.GetSystemMetrics(76), self.user32.GetSystemMetrics(77)
        width, height = self.user32.GetSystemMetrics(78), self.user32.GetSystemMetrics(79)
        if width < 2 or height < 2 or not (left <= x < left+width and top <= y < top+height):
            raise RuntimeError('点击坐标超出虚拟桌面范围。')
        move = INPUT()
        move.mi.dx = round((x-left) * 65535 / (width-1))
        move.mi.dy = round((y-top) * 65535 / (height-1))
        # Deliver a real mouse-move input event, including to input-driven UIs.
        move.mi.dwFlags = 0x0001 | 0x2000 | 0x4000 | 0x8000
        if self.user32.SendInput(1, ctypes.byref(move), ctypes.sizeof(INPUT)) != 1:
            raise RuntimeError('Windows 未能发送鼠标移动输入。')
        down_sent = False
        try:
            # Give a frame-based UI time to observe hover and the cursor position.
            self.wait(0.15)
            actual = wintypes.POINT()
            if not self.user32.GetCursorPos(ctypes.byref(actual)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.last_click_info = {
                'target': (x, y), 'cursor': (actual.x, actual.y),
                'move_sent': True, 'down_sent': False, 'up_sent': False,
            }
            if abs(actual.x-x) > 2 or abs(actual.y-y) > 2:
                raise RuntimeError(f'光标未到达目标：请求 {(x,y)}，实际 {(actual.x,actual.y)}。')
            if self.should_stop() or not self.owned_by_pet(x, y):
                return False
            event = INPUT()
            event.mi.dwFlags = 0x0002  # MOUSEEVENTF_LEFTDOWN
            if self.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT)) != 1:
                raise RuntimeError('Windows 未能发送鼠标按下。请确认脚本与桌宠权限一致。')
            down_sent = True
            self.last_click_info['down_sent'] = True
            self.wait(0.12)
            event.mi.dwFlags = 0x0004  # MOUSEEVENTF_LEFTUP
            if self.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT)) != 1:
                raise RuntimeError('Windows 未能发送鼠标松开。')
            down_sent = False
            self.last_click_info['up_sent'] = True
            self.last_click_info['released_at'] = time.monotonic()
            # Keep the pointer over the button until its release has been processed.
            self.wait(0.15)
            return not self.should_stop()
        finally:
            if down_sent:
                release = INPUT()
                release.mi.dwFlags = 0x0004
                self.user32.SendInput(1, ctypes.byref(release), ctypes.sizeof(INPUT))
            current = wintypes.POINT()
            if getattr(self, 'restore_pointer', True) and \
                    self.user32.GetCursorPos(ctypes.byref(current)) and \
                    abs(current.x-x) <= 2 and abs(current.y-y) <= 2:
                self.user32.SetCursorPos(old.x, old.y)


def report_elevation(desktop):
    """Name the UIPI trap: an elevated pet ignores input from a lower process."""
    ours = desktop.elevated()
    pet = desktop.pet_elevated()
    desktop.input_blocked = bool(pet and not ours)
    label = '管理员' if ours else '普通用户'
    if pet is None:
        print(f'运行权限：{label}；暂时没有找到桌宠窗口。', flush=True)
    elif pet and not ours:
        print(f'运行权限：{label}；桌宠以管理员权限运行，Windows 会拦截本助手发出的鼠标点击。'
              '请退出助手，右键“桌宠助手.exe”（源码版为“启动.cmd”）'
              '选择“以管理员身份运行”。', flush=True)
    else:
        print(f'运行权限：{label}；与桌宠一致，鼠标点击可以送达。', flush=True)


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='跟随桌宠识别宝箱并领取；Ctrl+Alt 停止。')
    parser.add_argument('--interval', type=float, default=900.0, help='检查间隔秒数，默认 900（15 分钟）')
    parser.add_argument('--hours', type=parse_hours, help='运行时长上限（小时）；未指定时启动前填写')
    parser.add_argument('--check', action='store_true', help='只检查环境和参考图，不截图、不点击')
    parser.add_argument('--preview', action='store_true', help='截一张识别预览，不点击')
    parser.add_argument('--once', action='store_true', help='只检查并尝试点击一个宝箱，然后退出')
    parser.add_argument('--idle-count', type=int, default=0,
                        help='每轮领取结束后在等待间隙发送的按键次数，0 表示不连点')
    parser.add_argument('--idle-interval', type=float, default=0.1,
                        help='间隙连点的按键间隔秒数，默认 0.1')
    parser.add_argument('--stop-file', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--plan', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--ui-events', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--ui-run-id', default='', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval < 0.5:
        parser.error('检查间隔必须为至少 0.5 秒的有限数值。')
    if not 0 <= args.idle_count <= 100000:
        parser.error('间隙连点次数必须为 0～100000 的整数（0 表示不连点）。')
    if not math.isfinite(args.idle_interval) or not 0.0001 <= args.idle_interval <= 60:
        parser.error('间隙连点间隔必须为 0.0001～60 秒的有限数值。')
    if sys.platform != 'win32':
        parser.error('此工具适用于 Windows。')

    hours = args.hours
    if not (args.check or args.preview or args.once) and hours is None:
        hours = prompt_hours()
        if hours is None:
            print('已取消启动。')
            return 0

    # Per-monitor DPI awareness keeps screenshots and physical mouse coordinates aligned.
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except AttributeError:
        ctypes.windll.user32.SetProcessDPIAware()
    desktop = WindowsDesktop()
    desktop.restore_pointer = not args.once
    report_elevation(desktop)
    detector = ChestDetector()
    observer = PetObserver(detector, desktop)
    if args.check:
        GdiRegionCapture()  # Validate API bindings without capturing pixels.
        ready = detector.find(Image.open(ASSETS / 'ready_reference.png'))
        cooldown = detector.find(Image.open(ASSETS / 'cooldown_reference.png'))
        if len(ready) != 2 or cooldown:
            raise RuntimeError('参考图检查失败，未启动点击。')
        print(f'自动领取 v{VERSION} 环境检查通过：可领图识别 2 个宝箱，冷却图识别 0 个。未截图、未点击。')
        print(f'屏幕区域 {desktop.bounds()}，系统缩放 {display_scale():.0%}。')
        return 0
    if args.preview:
        box = desktop.pet_monitor_bounds()
        if box is None:
            raise RuntimeError('无法确定桌宠所在的屏幕。请打开桌宠并将它留在一块屏幕上，再试一次。')
        frame, origin = desktop.capture(box)
        found = detector.find(frame)
        draw = ImageDraw.Draw(frame)
        for chest in found:
            x, y = round(chest.x), round(chest.y)
            owned = desktop.owned_by_pet(x + origin[0], y + origin[1])
            color = 'lime' if owned else 'red'
            draw.rectangle((x-12, y-12, x+12, y+12), outline=color, width=2)
            draw.text((x+14, y), f'{chest.score:.2f}', fill=color)
        output = Path(__file__).resolve().parent / 'preview.png'
        frame.save(output)
        print(f'预览已保存：{output}\n共识别到 {len(found)} 个宝箱图标。'
              f'\n截图尺寸 {frame.width}×{frame.height}（物理像素），系统缩放 {display_scale():.0%}。'
              '\n绿色框：识别成功且属于桌宠；红色框：识别成功，但属于图片查看器等其他窗口。'
              '\n图片中的宝箱仅供识别测试，不会点击。预览模式未发送任何点击。')
        return 0

    keyboard = clicker_module().WindowsKeyboard() if args.idle_count else None
    diagnostics = Path(__file__).resolve().parent / 'diagnostics'
    diagnostics.mkdir(exist_ok=True)
    log_path = Path(__file__).resolve().parent / 'auto_collect.log'
    handle = desktop.lock()
    stop = threading.Event()
    control = MonitorControl(stop, None if args.once else hours * 3600)
    desktop.stop_requested = control.stopped
    desktop.wait = control.wait

    def watch_stop():
        while not control.stopped():
            if desktop.stop_pressed() or (args.stop_file is not None and args.stop_file.exists()):
                stop.set()
                return
            stop.wait(0.01)

    watcher = threading.Thread(target=watch_stop, daemon=True)
    watcher.start()
    if args.stop_file is not None and args.stop_file.exists():
        stop.set()
    print(f'自动领取 v{VERSION} 已开始；按 Ctrl+Alt 停止。无需固定桌宠位置。', flush=True)
    print('首次全屏定位；之后只截图桌宠附近，局部定位失效后再全屏查找。', flush=True)
    print('每轮最多处理当前两个宝箱；每个宝箱最多尝试 5 次（含第一次）。', flush=True)
    if args.once:
        print('单次测试：测试后鼠标保留在目标位置，方便确认实际点在哪里。', flush=True)
    else:
        print(f'本次运行上限 {hours:g} 小时；每 {args.interval/60:g} 分钟检查一轮，启动后立即检查。', flush=True)
    if keyboard is not None:
        print(f'间隙连点：每轮领取结束后发送 {args.idle_count} 次，间隔 {args.idle_interval:g} 秒。', flush=True)

    def log(message):
        line = f'[{time.strftime("%H:%M:%S")}] {message}'
        print(line, flush=True)
        with log_path.open('a', encoding='utf-8') as stream:
            stream.write(line + '\n')

    pending_slot = None
    last_target_slot = None
    clicked_at = None
    blocked_hint_shown = False

    def record(tag, image, target, origin):
        nonlocal last_target_slot
        local_x, local_y = round(target.x-origin[0]), round(target.y-origin[1])
        box = (max(0, local_x-150), max(0, local_y-130),
               min(image.width, local_x+150), min(image.height, local_y+130))
        image.crop(box).save(diagnostics / f'{tag}.png')
        if tag == 'before':
            last_target_slot = target.slot
            log(f'尝试点击坐标 ({round(target.x)}, {round(target.y)})，图标匹配度 {target.score:.3f}。')

    claimed = 0
    session_record = None
    round_claims = 0
    rounds = 0
    ui_events = UiEvents(args.ui_events and not args.once, args.ui_run_id)

    def ui_event(event, next_seconds=None, reason=''):
        record_data = getattr(session_record, 'data', None)
        history_id = record_data.get('id', '') if isinstance(record_data, dict) else ''
        ui_events.send(event, claimed, control.clock(), control.deadline, next_seconds, reason, history_id)

    def save_count(reason=None):
        if session_record is not None:
            try:
                if reason is None:
                    session_record.update(claimed)
                else:
                    session_record.finish(reason)
            except OSError as error:
                log(f'领取数量记录未能保存：{error}。界面中的计数仍有效。')

    def attempt():
        nonlocal pending_slot, last_target_slot, clicked_at
        last_target_slot = None
        clicked_at = None
        try:
            status = attempt_claim(detector, desktop, control.stopped, control.wait,
                                   record=record, preferred_slot=pending_slot, observer=observer)
            pending_slot = last_target_slot if status == 'unverified' else None
            return status
        finally:
            if desktop.last_click_info is not None:
                clicked_at = desktop.last_click_info.get('released_at')
                log(f'鼠标输入记录：{desktop.last_click_info}')
                desktop.last_click_info = None

    def report(status, detail=None):
        nonlocal claimed, round_claims, rounds, pending_slot, blocked_hint_shown
        if status == 'cycle':
            pending_slot = None
            round_claims = 0
            rounds += 1
            ui_event('checking')
            log(f'开始第 {rounds} 轮检查；本次运行已领取 {claimed} 个宝箱。')
        elif status == 'changed':
            claimed += 1
            round_claims += 1
            save_count()
            ui_event('claimed')
            log(f'已领取宝箱：本轮 {round_claims} 个，本次运行累计 {claimed} 个'
                f'（复查图标已变化，实际领取以桌宠内显示为准）。')
        elif status == 'sleeping':
            ui_event('sleeping', detail)
            log(f'本轮结束：本轮领取 {round_claims} 个，本次运行累计 {claimed} 个；'
                f'约 {detail/60:.1f} 分钟后检查下一轮；Ctrl+Alt 可随时停止。')
        elif status == 'error':
            log(f'本轮出现错误：{detail}。将等待下一轮重试。')
        elif status == 'rechecking':
            log(f'暂未识别到可领取宝箱，本轮每秒复查一次，最多 {detail} 次。')
        elif status == 'retry':
            log(f'本宝箱第 {detail["completed"]}/{detail["limit"]} 次未确认生效；1 秒后重新定位，尝试第 {detail["next"]} 次。')
        elif status == 'retry-exhausted':
            log(f'本宝箱已尝试 {detail} 次，图标仍未变化；结束本轮，等待下一轮，监测继续。')
        else:
            messages = {
                'waiting': '没有识别到可领取宝箱，等待下一轮。',
                'moving': '桌宠正在移动或图标变化，本轮跳过。',
                'other-window': '窗口归属校验不通过，本轮跳过。',
                'busy': '本轮未发送点击，等待下一轮。',
                'unverified': '点击后宝箱图标仍在，未确认生效。',
                'unknown': '点击后桌宠被遮挡或位置变化，未确认生效；下一轮再试。',
            }
            if status in messages:
                log(messages[status])
            if (status == 'unverified' and not blocked_hint_shown
                    and getattr(desktop, 'input_blocked', False)):
                blocked_hint_shown = True
                log('点击已经发出，但桌宠没有反应：桌宠以管理员权限运行，本助手没有，'
                    'Windows 会丢弃这记鼠标点击（光标仍然会移动）。'
                    '请关闭助手，双击“以管理员身份运行.cmd”，'
                    '或右键“桌宠助手.exe”选择“以管理员身份运行”。')

    def gap_clicking(remaining):
        """Send one key burst right after a round, then leave the rest of the wait."""
        if keyboard is None or control.stopped():
            return
        ui_event('gap')
        log(f'距离下次检查约 {remaining/60:.1f} 分钟，先间隙连点 {args.idle_count} 次'
            f'（间隔 {args.idle_interval:g} 秒）。')

        def progress(sent):
            if sent % 500 == 0 and sent != args.idle_count:
                log(f'间隙连点进度：{sent}/{args.idle_count}。')

        sent, reason = run_gap_clicks(keyboard, args.idle_count, args.idle_interval,
                                      control.stopped, control.wait, progress)
        if reason == 'stopped':
            log(f'间隙连点已停止：共发送 {sent} 次。')
        else:
            log(f'间隙连点完成：发送 {sent} 次。')
        if sent and not control.stopped():
            # Let the pet animation settle so the next round is not judged as moving.
            control.wait(1.0)

    try:
        if not args.once and not control.stopped():
            try:
                session_record = RunRecord()
            except OSError as error:
                log(f'无法建立领取数量记录：{error}。本次仍会正常监测。')
        ui_event('started')
        reason = run_monitor(attempt, control, interval=args.interval, once=args.once,
                             report=report, gap=gap_clicking if keyboard is not None else None,
                             ready_rechecks=5, click_finished_at=lambda: clicked_at)
        save_count(reason)
        ui_event('finished', reason=reason)
        if reason == 'time-limit':
            log(f'已达到设定的运行时长上限，自动退出；本次运行共领取 {claimed} 个宝箱。')
        elif reason == 'once':
            log(f'单次测试已结束；本次共领取 {claimed} 个宝箱。持续监测请运行 start_auto_collect.cmd。')
        else:
            log(f'已收到停止请求；本次运行共领取 {claimed} 个宝箱。')
    except KeyboardInterrupt:
        save_count('stopped')
        ui_event('finished', reason='stopped')
        log(f'已中断监测；本次运行共领取 {claimed} 个宝箱。')
        raise
    except Exception:
        save_count('error')
        ui_event('finished', reason='error')
        raise
    finally:
        stop.set()
        watcher.join(timeout=1)
        desktop.kernel32.CloseHandle(handle)
        print('自动领取已停止。', flush=True)
    if args.plan and reason == 'stopped':
        print('计划模式：已被停止，后续步骤不会自动开始。', flush=True)
        return 3
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n已停止。')
        sys.exit(0)
    except (OSError, RuntimeError) as error:
        print(f'\n错误：{error}')
        sys.exit(1)
