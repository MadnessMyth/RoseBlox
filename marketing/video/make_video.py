#!/usr/bin/env python3
"""Render the Roseblox sneak-peek video (30 s, 1920x1080, 128 BPM).

Usage: python3 marketing/video/make_video.py [--stills]

Needs Pillow, NumPy, ffmpeg and the Cantarell fonts. Every frame is a pure
function of time, and every cut, click and flash sits on the music's beat grid.
Writes roseblox-sneak-peek.mp4 and roseblox-sneak-peek.jpg next to this file;
--stills writes preview frames to stills/ instead.
"""

from __future__ import annotations

import argparse
import math
import subprocess
import wave
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SHOTS = ROOT / "docs" / "screenshots"
FONT_DIR = Path("/usr/share/fonts/opentype/cantarell")

W, H = 1920, 1080
FPS = 30
BPM = 128
BEAT = 60 / BPM
BAR = BEAT * 4
BARS = 16
TOTAL = BARS * BAR

ACCENT = (226, 92, 152)
ACCENT_HI = (255, 150, 200)
BG_DARK = (18, 13, 22)
TEXT = (248, 240, 246)
MUTED = (196, 182, 194)
PAGE_BG = (29, 26, 32)
ROW_BG = (47, 44, 49)

DRUM_BARS = set(range(2, 11)) | {12, 13, 14}


def bars(n: float) -> float:
    return n * BAR


def beats(n: float) -> float:
    return n * BEAT


# ---------------------------------------------------------------- helpers

def clamp01(x: float) -> float:
    return min(max(x, 0.0), 1.0)


def ease_out(x: float) -> float:
    return 1 - (1 - clamp01(x)) ** 3


def ease_in(x: float) -> float:
    return clamp01(x) ** 3


def ease_in_out(x: float) -> float:
    x = clamp01(x)
    return 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2


def back_out(x: float, s: float = 1.9) -> float:
    x = clamp01(x) - 1
    return 1 + (s + 1) * x ** 3 + s * x ** 2


@lru_cache(maxsize=None)
def font(weight: str, size: float) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / f"Cantarell-{weight}.otf"), size)


def with_alpha(img: Image.Image, a: float) -> Image.Image:
    if a >= 1:
        return img
    r, g, b, al = img.split()
    al = al.point(lambda v: int(v * max(a, 0.0)))
    return Image.merge("RGBA", (r, g, b, al))


def paste(dst: Image.Image, src: Image.Image, x: float, y: float, a: float = 1.0) -> None:
    if a > 0.003:
        dst.alpha_composite(with_alpha(src, a), (int(round(x)), int(round(y))))


def pulse_at(t: float) -> float:
    """Kick envelope: 1 on each beat in drum bars, decaying quickly."""
    b = int(t // BAR)
    if b not in DRUM_BARS:
        return 0.0
    return math.exp(-(t % BEAT) / 0.11)


@lru_cache(maxsize=None)
def text_img(text: str, weight: str, size: float, color: tuple, tracking: float = 0.0) -> Image.Image:
    f = font(weight, size)
    asc, desc = f.getmetrics()
    width = int(sum(f.getlength(c) for c in text) + tracking * max(0, len(text) - 1)) + 8
    img = Image.new("RGBA", (width, asc + desc + 6), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if tracking:
        x = 4.0
        for c in text:
            d.text((x, 2), c, font=f, fill=color)
            x += f.getlength(c) + tracking
    else:
        d.text((4, 2), text, font=f, fill=color)
    return img


def scaled(img: Image.Image, s: float) -> Image.Image:
    if abs(s - 1) < 1e-3:
        return img
    return img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))), Image.BICUBIC)


# ---------------------------------------------------------------- 3D

def perspective_coeffs(out_pts, in_pts):
    rows, rhs = [], []
    for (x, y), (u, v) in zip(out_pts, in_pts):
        rows.append([x, y, 1, 0, 0, 0, -x * u, -y * u])
        rhs.append(u)
        rows.append([0, 0, 0, x, y, 1, -x * v, -y * v])
        rhs.append(v)
    return np.linalg.solve(np.array(rows, float), np.array(rhs, float)).tolist()


def project_plane(w: int, h: int, rx: float, ry: float, rz: float, scale: float,
                  tx: float, ty: float, focal: float = 1700.0):
    """Corners of a w x h plane centred on the frame, rotated (degrees) and projected."""
    ax, ay, az = map(math.radians, (rx, ry, rz))
    out = []
    for cx, cy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
        x, y, z = cx * scale, cy * scale, 0.0
        x, y = x * math.cos(az) - y * math.sin(az), x * math.sin(az) + y * math.cos(az)
        y, z = y * math.cos(ax) - z * math.sin(ax), y * math.sin(ax) + z * math.cos(ax)
        x, z = x * math.cos(ay) + z * math.sin(ay), -x * math.sin(ay) + z * math.cos(ay)
        k = focal / (focal + z)
        out.append((W / 2 + tx + x * k, H / 2 + ty + y * k))
    return out


def warp(src: Image.Image, rx=0.0, ry=0.0, rz=0.0, scale=1.0, tx=0.0, ty=0.0) -> Image.Image:
    if max(abs(rx), abs(ry), abs(rz)) < 0.05 and abs(scale - 1) < 1e-3 and src.size == (W, H) \
            and abs(tx) < 0.5 and abs(ty) < 0.5:
        return src
    quad = project_plane(src.width, src.height, rx, ry, rz, scale, tx, ty)
    corners = [(0, 0), (src.width, 0), (src.width, src.height), (0, src.height)]
    coeffs = perspective_coeffs(quad, corners)
    return src.transform((W, H), Image.Transform.PERSPECTIVE, coeffs, Image.BICUBIC)


# ---------------------------------------------------------------- backgrounds and particles

def make_background(bright: float) -> Image.Image:
    w, h = W + 240, H + 160
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    base = np.array(BG_DARK, np.float32)
    g1 = np.exp(-(((xx - w * 0.18) / (w * 0.5)) ** 2 + ((yy - h * 0.1) / (h * 0.55)) ** 2))
    g2 = np.exp(-(((xx - w * 0.9) / (w * 0.42)) ** 2 + ((yy - h * 0.95) / (h * 0.5)) ** 2))
    g3 = np.exp(-(((xx - w * 0.5) / (w * 0.25)) ** 2 + ((yy - h * 0.5) / (h * 0.35)) ** 2))
    pink = np.array((130, 36, 84), np.float32)
    violet = np.array((66, 32, 104), np.float32)
    img = base + g1[..., None] * (pink - base) * 0.9 * bright + g2[..., None] * (violet - base) * 0.8 * bright
    img += g3[..., None] * np.array((40, 14, 32), np.float32) * (bright - 1) * 2.5
    img += np.random.default_rng(7).normal(0, 1.5, img.shape).astype(np.float32)
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB").convert("RGBA")


@lru_cache(maxsize=None)
def sparkle(size: int) -> Image.Image:
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = s / 2
    d.polygon([(c, 0), (c + s * 0.09, c - s * 0.09), (s, c), (c + s * 0.09, c + s * 0.09),
               (c, s), (c - s * 0.09, c + s * 0.09), (0, c), (c - s * 0.09, c - s * 0.09)],
              fill=(255, 225, 240, 255))
    glow = img.filter(ImageFilter.GaussianBlur(s * 0.12))
    glow.alpha_composite(img)
    return glow.resize((size * 2, size * 2), Image.LANCZOS)


RNG = np.random.default_rng(21)
STARS = [dict(x=RNG.uniform(0, W), y=RNG.uniform(0, H), vy=RNG.uniform(14, 46), vx=RNG.uniform(-6, 6),
              size=int(RNG.integers(4, 11)), f=RNG.uniform(0.3, 1.1), ph=RNG.uniform(0, 6.28),
              depth=RNG.uniform(0.4, 1.0)) for _ in range(70)]
BURST = [dict(ang=RNG.uniform(0, 6.28), speed=RNG.uniform(500, 1500), size=int(RNG.integers(5, 13)))
         for _ in range(46)]


def draw_stars(img: Image.Image, t: float, amount: float, pulse: float) -> None:
    if amount <= 0:
        return
    for s in STARS:
        y = (s["y"] - s["vy"] * t) % (H + 60) - 30
        x = (s["x"] + s["vx"] * t) % (W + 60) - 30
        tw = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(2 * math.pi * s["f"] * t + s["ph"]))
        a = amount * s["depth"] * tw * (0.75 + 0.5 * pulse)
        sp = sparkle(s["size"])
        paste(img, sp, x - sp.width / 2, y - sp.height / 2, min(1.0, a))


def draw_burst(img: Image.Image, dt: float, cx: float, cy: float) -> None:
    if dt < 0 or dt > 1.2:
        return
    for p in BURST:
        dist = p["speed"] * (1 - math.exp(-dt * 3.2)) / 3.2
        x = cx + math.cos(p["ang"]) * dist
        y = cy + math.sin(p["ang"]) * dist
        sp = sparkle(p["size"])
        paste(img, sp, x - sp.width / 2, y - sp.height / 2, (1 - dt / 1.2) ** 1.5)


def ring(img: Image.Image, cx: float, cy: float, r: float, width: int, color, a: float) -> None:
    if a <= 0.01:
        return
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse((cx - r, cy - r, cx + r, cy + r), outline=(*color, int(255 * a)),
                                  width=max(1, width))
    img.alpha_composite(layer)


# ---------------------------------------------------------------- screenshot cards

def toggle_sprites() -> tuple[Image.Image, Image.Image]:
    src = Image.open(SHOTS / "16-while-playing.png").convert("RGB")
    on = src.crop((1163, 468, 1211, 496))
    off = src.crop((1163, 630, 1211, 658))
    return on, off


@dataclass
class Shot:
    file: str
    crop: tuple[int, int, int, int] | None = None
    patches: list = field(default_factory=list)
    toggles_off: list[tuple[int, int]] = field(default_factory=list)


class Card:
    def __init__(self, shot: Shot):
        src = Image.open(SHOTS / shot.file).convert("RGB")
        on, off = toggle_sprites()
        self.on = on.convert("RGBA")
        d = ImageDraw.Draw(src)
        for box, text, pos in shot.patches:
            d.rectangle(box, fill=ROW_BG)
            d.text(pos, text, font=font("Regular", 11.5), fill=(155, 152, 157))
        for x, y in shot.toggles_off:
            src.paste(off, (int(x - off.width / 2 + 0.5), int(y - off.height / 2)))
        self.ox, self.oy = (shot.crop[0], shot.crop[1]) if shot.crop else (0, 0)
        if shot.crop:
            src = src.crop(shot.crop)
        self.base = src
        self.size = src.size
        w, h = self.size
        self.radius = max(10, w // 70)
        self.mask = Image.new("L", self.size, 0)
        ImageDraw.Draw(self.mask).rounded_rectangle((0, 0, w - 1, h - 1), self.radius, fill=255)
        self.pad = 80
        frame = Image.new("RGBA", (w + self.pad * 2, h + self.pad * 2), (0, 0, 0, 0))
        shadow = Image.new("L", frame.size, 0)
        ImageDraw.Draw(shadow).rounded_rectangle(
            (self.pad, self.pad + 26, self.pad + w, self.pad + h + 26), self.radius, fill=185)
        frame.paste((0, 0, 0, 255), (0, 0), shadow.filter(ImageFilter.GaussianBlur(34)))
        self.frame = frame

    def local(self, box):
        return (box[0] - self.ox, box[1] - self.oy, box[2] - self.ox, box[3] - self.oy)

    def to_card(self, x: float, y: float) -> tuple[float, float]:
        return self.pad + x - self.ox, self.pad + y - self.oy

    def render(self, t: float, seg: "Segment") -> Image.Image:
        shot = self.base
        if seg.reveal:
            at, boxes = seg.reveal
            k = ease_out((t - at) / 0.3)
            if k < 1:
                masked = shot.copy()
                d = ImageDraw.Draw(masked)
                for box in boxes:
                    d.rectangle(self.local(box), fill=PAGE_BG)
                shot = Image.blend(masked, shot, k)
        shot = shot.convert("RGBA")
        overlay = Image.new("RGBA", self.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        for at, box in seg.highlights:
            a = ease_out((t - at) / 0.2)
            if a <= 0:
                continue
            p = 1 - clamp01((t - at) / 0.45)
            g = 8 * p
            x0, y0, x1, y1 = self.local(box)
            d.rounded_rectangle((x0 - g, y0 - g, x1 + g, y1 + g), 9, fill=(*ACCENT, int(36 * a + 60 * p)))
            d.rounded_rectangle((x0 - g, y0 - g, x1 + g, y1 + g), 9, outline=(*ACCENT_HI, int(255 * a)), width=3)
        shot.alpha_composite(overlay)
        for at, (x, y) in seg.toggles:
            a = ease_out((t - at) / 0.1)
            if a > 0:
                paste(shot, self.on, x - self.ox - self.on.width / 2 + 0.5, y - self.oy - self.on.height / 2, a)
        card = self.frame.copy()
        card.paste(shot, (self.pad, self.pad), self.mask)
        w, h = self.size
        ImageDraw.Draw(card).rounded_rectangle(
            (self.pad, self.pad, self.pad + w - 1, self.pad + h - 1), self.radius,
            outline=(255, 255, 255, 46), width=2)
        return card


@dataclass
class Segment:
    start_bar: int
    n_bars: int
    shot: Shot
    labels: list[tuple[float, str, str]]  # (local time, eyebrow, title)
    camera: list[tuple[float, float | None, float | None, float]]
    tilt: list[tuple[float, float, float, float]]  # (local time, rx, ry, rz)
    clicks: list[tuple[float, int, int]] = field(default_factory=list)
    toggles: list[tuple[float, tuple[int, int]]] = field(default_factory=list)
    highlights: list[tuple[float, tuple[int, int, int, int]]] = field(default_factory=list)
    reveal: tuple[float, list] | None = None
    enter: str = "whip"

    @property
    def start(self) -> float:
        return bars(self.start_bar)

    @property
    def duration(self) -> float:
        return bars(self.n_bars)


PERF = Shot("14-performance.png", patches=[
    # Taken without GameMode and MangoHud installed; these are the subtitles
    # performance_page.py shows when they are.
    ((240, 638, 1100, 654), "Asks Feral GameMode to tune the system while the application runs.", (246, 640.5)),
    ((240, 693, 1100, 709), "Shows frame rate and frame times through MangoHud's Vulkan layer.", (246, 695.5)),
])
MODS = Shot("17-mods.png")
PLAY = Shot("16-while-playing.png", toggles_off=[(1187, 482), (1187, 590)])
DUP = Shot("22-player-duplicate.png", crop=(0, 0, 1428, 560))
HOME = Shot("07-home.png", crop=(0, 0, 1292, 660))

b1, b2, b3 = beats(1), beats(2), beats(3)
SEGMENTS = [
    Segment(3, 2, HOME,
            labels=[(0.0, "ROSEBLOX", "One click to play."), (bars(1), "ROSEBLOX", "Player and Studio.")],
            camera=[(beats(2), None, None, 1.0), (bars(1) - 0.12, 420, 320, 1.8), (bars(2), 400, 330, 1.95)],
            tilt=[(0.0, 8, -48, 6), (0.75, 0, 2, 0), (1.1, 0, 0, 0), (bars(2), 3, -5, -1)],
            clicks=[(bars(1), 296, 335)], enter="fly"),
    Segment(5, 2, PERF,
            labels=[(0.0, "PERFORMANCE", "GameMode, on."), (bars(1), "PERFORMANCE", "MangoHud, on.")],
            camera=[(0.0, 720, 560, 1.45), (b1, 720, 625, 1.7), (bars(1) - 0.1, 720, 625, 1.72),
                    (bars(1) + b1, 720, 680, 1.75), (bars(2), 720, 660, 1.85)],
            tilt=[(0.0, 4, 10, 0), (bars(1), 2, -2, 0), (bars(2), 4, -9, 1)],
            clicks=[(b1, 1176, 636), (bars(1) + b1, 1176, 690)],
            toggles=[(b1, (1176, 636)), (bars(1) + b1, (1176, 690))],
            highlights=[(b1, (231, 611, 1212, 663)), (bars(1) + b1, (231, 665, 1212, 717))]),
    Segment(7, 2, MODS,
            labels=[(0.0, "MODS", "Your fonts."), (bars(1), "MODS", "Skyboxes, sounds, textures.")],
            camera=[(0.0, 700, 300, 2.0), (b1, 720, 300, 2.3), (bars(1) - 0.1, 720, 400, 2.15),
                    (bars(1) + b1, 720, 660, 2.15), (bars(1) + b2 + 0.1, 720, 800, 2.2),
                    (bars(2), 720, 820, 2.3)],
            tilt=[(0.0, 3, -10, 0), (bars(1), 2, 3, 0), (bars(2), 4, 9, -1)],
            clicks=[(bars(1) + b1, 1063, 657)],
            highlights=[(b1, (231, 273, 1231, 326)), (bars(1) + b1, (231, 631, 1231, 683)),
                        (bars(1) + b2, (231, 797, 1231, 902))]),
    Segment(9, 2, PLAY,
            labels=[(0.0, "WHILE PLAYING", "Discord presence."), (bars(1), "WHILE PLAYING", "Server location.")],
            camera=[(0.0, 720, 470, 1.45), (b1, 720, 482, 1.62), (bars(1) - 0.1, 720, 520, 1.62),
                    (bars(1) + b1, 720, 590, 1.66), (bars(2), 720, 590, 1.75)],
            tilt=[(0.0, 4, 10, 0), (bars(1), 2, -3, 0), (bars(2), 3, -10, 1)],
            clicks=[(b1, 1187, 482), (bars(1) + b1, 1187, 590)],
            toggles=[(b1, (1187, 482)), (bars(1) + b1, (1187, 590))],
            highlights=[(b1, (231, 455, 1224, 509)), (bars(1) + b1, (231, 563, 1224, 617))]),
    Segment(11, 2, DUP,
            labels=[(0.0, "NEW IN 0.4.2", "Duplicate."), (bars(1), "NEW IN 0.4.2", "Two Players. Side by side.")],
            camera=[(0.0, None, None, 1.05), (b2 - 0.1, 1080, 360, 1.7), (b3, 1000, 400, 1.55),
                    (bars(1) - 0.1, 1000, 405, 1.45), (bars(1) + b1, 1060, 410, 1.6), (bars(2), 1060, 410, 1.7)],
            tilt=[(0.0, 4, -8, 0), (bars(1) - 0.05, 0, 0, 0), (bars(1) + 0.3, 6, 12, -2), (bars(2), 4, 6, -1)],
            clicks=[(b2, 1221, 358), (bars(1), 1113, 358), (bars(1) + b1, 1113, 453)],
            highlights=[(bars(1), (232, 314, 1394, 404)), (bars(1) + b1, (232, 406, 1394, 498))],
            reveal=(b3, [(230, 404, 1396, 502), (228, 278, 1010, 298), (228, 200, 1040, 226)])),
]

INTRO_END = bars(2)
SLAM = bars(2)
OUTRO = bars(13)


# ---------------------------------------------------------------- renderer

def camera_at(seg: Segment, t: float, center) -> tuple[float, float, float]:
    keys = [(k[0], center[0] if k[1] is None else k[1], center[1] if k[2] is None else k[2], k[3])
            for k in seg.camera]
    if t <= keys[0][0]:
        return keys[0][1:]
    for (t0, x0, y0, z0), (t1, x1, y1, z1) in zip(keys, keys[1:]):
        if t <= t1:
            k = ease_in_out((t - t0) / (t1 - t0))
            return x0 + (x1 - x0) * k, y0 + (y1 - y0) * k, z0 + (z1 - z0) * k
    return keys[-1][1:]


def tilt_at(seg: Segment, t: float) -> tuple[float, float, float]:
    keys = seg.tilt
    if t <= keys[0][0]:
        return keys[0][1:]
    for (t0, *a), (t1, *b) in zip(keys, keys[1:]):
        if t <= t1:
            k = (back_out if seg.enter == "fly" and t0 == 0 else ease_in_out)((t - t0) / (t1 - t0))
            return tuple(x + (y - x) * k for x, y in zip(a, b))
    return tuple(keys[-1][1:])


class Renderer:
    def __init__(self):
        self.bg = make_background(1.0)
        self.bg_hi = make_background(1.45)
        icon = Image.open(HERE / "icon.png").convert("RGBA")
        self.icon = icon.resize((300, 300), Image.LANCZOS)
        self.cards = {id(s.shot): Card(s.shot) for s in SEGMENTS}
        self.cursor = self.make_cursor()
        self.mosaic = self.make_mosaic()

    @staticmethod
    def make_cursor() -> Image.Image:
        pts = [(0, 0), (0, 34), (9, 26), (15, 40), (21, 37), (15, 24), (27, 24)]
        s = 3
        img = Image.new("RGBA", (40 * s, 48 * s), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        p = [(x * s + 6, y * s + 6) for x, y in pts]
        d.polygon(p, fill=(255, 255, 255, 255))
        d.line(p + [p[0]], fill=(20, 16, 22, 255), width=s * 2, joint="curve")
        shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).polygon([(x + 4, y + 6) for x, y in p], fill=(0, 0, 0, 120))
        shadow = shadow.filter(ImageFilter.GaussianBlur(5))
        shadow.alpha_composite(img)
        return shadow.resize((44, 53), Image.LANCZOS)

    @staticmethod
    def make_mosaic() -> Image.Image:
        files = ["07-home.png", "14-performance.png", "17-mods.png", "16-while-playing.png",
                 "22-player-duplicate.png", "10-notification-center.png", "03-setup-rosemary-ready.png",
                 "13-download-speed.png", "21-multiple-players.png"]
        tw, th, gap = 760, 470, 46
        cols, rows = 3, 3
        img = Image.new("RGBA", (cols * tw + (cols + 1) * gap, rows * th + (rows + 1) * gap), (0, 0, 0, 0))
        for k, f in enumerate(files):
            src = Image.open(SHOTS / f).convert("RGB")
            s = max(tw / src.width, th / src.height)
            src = src.resize((int(src.width * s), int(src.height * s)), Image.LANCZOS).crop((0, 0, tw, th))
            mask = Image.new("L", (tw, th), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, tw - 1, th - 1), 14, fill=255)
            x, y = gap + (k % cols) * (tw + gap), gap + (k // cols) * (th + gap)
            img.paste(src, (x, y), mask)
        return img

    # -- per-frame

    def background(self, t: float) -> Image.Image:
        x = 120 + 100 * math.sin(2 * math.pi * t / 19)
        y = 80 + 60 * math.sin(2 * math.pi * t / 13 + 1.3)
        box = (int(x), int(y), int(x) + W, int(y) + H)
        p = pulse_at(t)
        if t >= SLAM and t < SLAM + 0.6:
            p = max(p, 1 - (t - SLAM) / 0.6)
        bg = self.bg.crop(box)
        if p > 0.02:
            bg = Image.blend(bg, self.bg_hi.crop(box), 0.55 * p)
        return bg

    def frame(self, t: float) -> Image.Image:
        img = self.background(t)
        star_amount = 1.0 if t < bars(3) or t >= OUTRO else 0.45
        draw_stars(img, t, star_amount, pulse_at(t))
        if t < SLAM:
            self.intro(img, t)
        elif t < bars(3):
            self.slam(img, t)
        elif t < OUTRO:
            seg = next(s for s in SEGMENTS if s.start <= t < s.start + s.duration)
            self.segment(img, seg, t)
        else:
            self.outro(img, t)
        if SLAM <= t < SLAM + 0.35:
            flash = Image.new("RGBA", (W, H), (255, 236, 246, int(200 * (1 - (t - SLAM) / 0.35) ** 2)))
            img.alpha_composite(flash)
        if bars(12) <= t < bars(12) + 0.25:
            flash = Image.new("RGBA", (W, H), (255, 236, 246, int(110 * (1 - (t - bars(12)) / 0.25) ** 2)))
            img.alpha_composite(flash)
        if t > TOTAL - 0.7:
            img = Image.blend(Image.new("RGBA", (W, H), (0, 0, 0, 255)), img, clamp01((TOTAL - t) / 0.7))
        return img.convert("RGB")

    def kinetic_word(self, img, word: str, weight: str, size: float, color, cx: float, y: float,
                     t: float, at: float, out_at: float | None = None):
        if t < at:
            return
        k = back_out((t - at) / 0.32, 2.4)
        a = ease_out((t - at) / 0.12)
        s = 1.35 - 0.35 * k
        if out_at is not None and t > out_at:
            o = ease_in((t - out_at) / 0.25)
            s *= 1 + 0.6 * o
            a *= 1 - o
        im = scaled(text_img(word, weight, size, color), s)
        paste(img, im, cx - im.width / 2, y - im.height / 2, a)

    def intro(self, img, t: float):
        out = beats(6.5)
        self.kinetic_word(img, "Roblox.", "ExtraBold", 150, TEXT, W / 2, H / 2 - 170, t, 0.0, out)
        self.kinetic_word(img, "On Linux.", "ExtraBold", 150, TEXT, W / 2, H / 2, t, beats(2), out)
        self.kinetic_word(img, "Feels native.", "ExtraBold", 110, ACCENT, W / 2, H / 2 + 160, t, beats(4), out)
        if t > beats(6):
            r = 40 + 600 * ease_in((t - beats(6)) / beats(2))
            ring(img, W / 2, H / 2, r, 3, ACCENT, 0.25 * ease_in((t - beats(6)) / beats(2)))

    def slam(self, img, t: float):
        dt = t - SLAM
        draw_burst(img, dt, W / 2, H / 2 - 90)
        for k, delay in enumerate((0.0, 0.08, 0.16)):
            d = dt - delay
            if d > 0:
                ring(img, W / 2, H / 2 - 90, 160 + 900 * ease_out(d / 0.8), max(1, int(10 - 9 * d / 0.8)),
                     ACCENT_HI if k == 0 else ACCENT, 0.9 * (1 - clamp01(d / 0.8)))
        s = 1 + 1.8 * (1 - ease_out(dt / 0.14)) if dt < 0.14 else 1 + 0.04 * math.exp(-(dt - 0.14) / 0.1) \
            * math.sin((dt - 0.14) * 40)
        bob = 1 + 0.03 * pulse_at(t)
        icon = scaled(self.icon, s * bob)
        out = clamp01((t - (bars(3) - 0.22)) / 0.22)
        push = 1 + 0.5 * ease_in(out)
        icon = scaled(icon, push)
        paste(img, icon, W / 2 - icon.width / 2, H / 2 - 90 - icon.height / 2, 1 - ease_in(out))
        word = text_img("Roseblox", "ExtraBold", 120, TEXT)
        k = back_out((dt - beats(1)) / 0.35, 2.0)
        if dt > beats(1):
            im = scaled(word, (0.8 + 0.2 * k) * push)
            paste(img, im, W / 2 - im.width / 2, H / 2 + 110 - im.height / 2, ease_out((dt - beats(1)) / 0.15)
                  * (1 - ease_in(out)))
        tag = text_img("Roblox Player and Studio on Linux", "Bold", 44, ACCENT)
        if dt > beats(2):
            a = ease_out((dt - beats(2)) / 0.25) * (1 - ease_in(out))
            paste(img, scaled(tag, push), W / 2 - tag.width * push / 2, H / 2 + 200 + 30 * (1 - a), a)

    def segment(self, img, seg: Segment, gt: float):
        t = gt - seg.start
        card_obj = self.cards[id(seg.shot)]
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        card = card_obj.render(t, seg)
        cw, ch = card.size
        label_bottom = 250
        anchor = (W / 2, label_bottom + (H - label_bottom) / 2 + 10)
        fit = min(1480 / card_obj.size[0], (H - label_bottom - 90) / card_obj.size[1])
        center = (card_obj.ox + card_obj.size[0] / 2, card_obj.oy + card_obj.size[1] / 2)
        fx, fy, z = camera_at(seg, t, center)
        z *= 1 + 0.03 * pulse_at(gt)
        scale = fit * z
        fcx, fcy = card_obj.to_card(fx, fy)
        fill = clamp01((z - 1.0) / 0.3)
        if fill > 0:
            def keep(f, lo_e, hi_e, before, after):
                lo, hi = lo_e + before / scale, hi_e - after / scale
                return (lo + hi) / 2 if lo > hi else min(max(f, lo), hi)
            fcx += (keep(fcx, card_obj.pad, card_obj.pad + card_obj.size[0], anchor[0],
                         W - anchor[0]) - fcx) * fill

        def to_screen(x, y):
            return anchor[0] + (x - fcx) * scale, anchor[1] + (y - fcy) * scale

        x0c, y0c = max(0.0, fcx - (anchor[0] + 300) / scale), max(0.0, fcy - (anchor[1] + 300) / scale)
        x1c, y1c = min(cw, fcx + (W - anchor[0] + 300) / scale), min(ch, fcy + (H - anchor[1] + 300) / scale)
        region = card.crop((int(x0c), int(y0c), math.ceil(x1c), math.ceil(y1c)))
        region = region.resize((max(1, round(region.width * scale)), max(1, round(region.height * scale))),
                               Image.BICUBIC if scale > 1 else Image.LANCZOS)
        sx, sy = to_screen(int(x0c), int(y0c))
        paste(layer, region, sx, sy)
        self.cursor_and_clicks(layer, seg, card_obj, t, to_screen)

        rx, ry, rz = tilt_at(seg, t)
        enter_scale = 1.0
        tx = 0.0
        if seg.enter == "fly":
            k = ease_out(t / 0.75)
            enter_scale = 0.55 + 0.45 * k
            ty_off = 220 * (1 - k)
        else:
            ty_off = 0.0
            if t < 0.14:
                tx = W * 0.7 * (1 - ease_out(t / 0.14))
        exit_k = 0.0
        nxt = seg.start + seg.duration
        if nxt < OUTRO and gt > nxt - 0.14:
            exit_k = ease_in((gt - (nxt - 0.14)) / 0.14)
            tx -= W * 0.7 * exit_k
        elif nxt >= OUTRO and gt > nxt - 0.25:
            exit_k = ease_in((gt - (nxt - 0.25)) / 0.25)
            enter_scale *= 1 + 0.35 * exit_k
        warped = warp(layer, rx, ry, rz, enter_scale, tx, ty_off)
        blur = max(clamp01(1 - t / 0.14) if seg.enter == "whip" else 0.0, exit_k if nxt < OUTRO else 0.0)
        if blur > 0.05:
            f = max(2, int(14 * blur))
            warped = warped.resize((W // f, H), Image.BILINEAR).resize((W, H), Image.BILINEAR)
        fade = 1.0 if seg.enter == "whip" else ease_out(t / 0.3)
        if nxt >= OUTRO:
            fade *= 1 - exit_k
        img.alpha_composite(with_alpha(warped, fade))

        scrim = Image.new("L", (1, H), 0)
        for y in range(H):
            v = 1.0 if y < label_bottom - 40 else max(0.0, 1 - (y - label_bottom + 40) / 120)
            scrim.putpixel((0, y), int(235 * v))
        header = self.background(gt)
        header.putalpha(scrim.resize((W, H)))
        img.alpha_composite(header)
        self.labels(img, seg, t, gt)

    def labels(self, img, seg: Segment, t: float, gt: float):
        current = [lb for lb in seg.labels if lb[0] <= t]
        if not current:
            return
        at, eyebrow, title = current[-1]
        nxt = seg.start + seg.duration
        out_k = ease_in((gt - (nxt - 0.12)) / 0.12) if gt > nxt - 0.12 else 0.0
        first = at == seg.labels[0][0]
        eb = text_img(eyebrow, "Bold", 28, ACCENT, tracking=5)
        a_eb = ease_out((t - seg.labels[0][0]) / 0.2) * (1 - out_k)
        paste(img, eb, W / 2 - eb.width / 2, 62, a_eb)
        bar_w = 70 + 30 * pulse_at(gt)
        bar = Image.new("RGBA", (int(bar_w), 6), (*ACCENT, 255))
        paste(img, bar, W / 2 - bar_w / 2, 112, a_eb)
        words = title.split(" ")
        size = 92
        imgs = [text_img(wd, "ExtraBold", size, TEXT) for wd in words]
        space = font("ExtraBold", size).getlength(" ") - 8
        total = sum(i.width for i in imgs) + space * (len(imgs) - 1)
        x = W / 2 - total / 2
        for k, (wd, im) in enumerate(zip(words, imgs)):
            st = at + 0.05 * k
            if t >= st:
                kk = back_out((t - st) / 0.3, 2.6)
                a = ease_out((t - st) / 0.1) * (1 - out_k)
                s = 1.4 - 0.4 * kk
                if first and seg.enter == "fly":
                    s = 1.4 - 0.4 * kk
                sim = scaled(im, s)
                paste(img, sim, x + im.width / 2 - sim.width / 2, 185 - sim.height / 2 - 40 * out_k, a)
            x += im.width + space

    def cursor_and_clicks(self, layer, seg: Segment, card_obj: Card, t: float, to_screen):
        if not seg.clicks:
            return
        pts = [(at, *to_screen(*card_obj.to_card(x, y))) for at, x, y in seg.clicks]
        enter_at = max(0.15, pts[0][0] - 0.75)
        if t < enter_at:
            return
        sx, sy = pts[0][1] + 300, pts[0][2] + 240
        x, y = sx, sy
        prev_t, px0, py0 = enter_at, sx, sy
        for at, px, py in pts:
            move_start = max(prev_t + 0.1, at - 0.5)
            if t <= at:
                k = ease_in_out((t - move_start) / max(0.05, at - 0.06 - move_start))
                x, y = px0 + (px - px0) * k, py0 + (py - py0) * k
                break
            prev_t, px0, py0 = at, px, py
            x, y = px, py
        a = ease_out((t - enter_at) / 0.2)
        press = 1.0
        for at, px, py in pts:
            dt = t - at
            if -0.05 <= dt <= 0.1:
                press = 0.82
            if 0 <= dt <= 0.45:
                ring(layer, px, py, 12 + 70 * ease_out(dt / 0.45), 5, ACCENT_HI, 0.95 * (1 - dt / 0.45))
                ring(layer, px, py, 8 + 40 * ease_out(dt / 0.3), 3, (255, 255, 255), 0.7 * (1 - clamp01(dt / 0.3)))
        cur = scaled(self.cursor, press)
        paste(layer, cur, x - 6 * press, y - 6 * press, a)

    def outro(self, img, t: float):
        dt = t - OUTRO
        k = ease_out(dt / 1.2)
        mz = 0.62 + 0.1 * dt / (TOTAL - OUTRO)
        mosaic = warp(self.mosaic, 28, -14 + 6 * dt / 5, -8, mz, -40 + 30 * dt, 30 - 20 * dt)
        dim = with_alpha(mosaic, 0.42 * k)
        img.alpha_composite(dim)
        vign = Image.new("L", (W, H), 0)
        ImageDraw.Draw(vign).ellipse((W * 0.18, H * 0.05, W * 0.82, H * 0.95), fill=255)
        vign = vign.filter(ImageFilter.GaussianBlur(160))
        center_bg = self.background(t)
        center_bg.putalpha(vign.point(lambda v: int(v * 0.85)))
        img.alpha_composite(center_bg)
        ring(img, W / 2, H / 2 - 150, 150 + 700 * ease_out(dt / 1.0), max(1, int(8 - 7 * dt)), ACCENT,
             0.8 * (1 - clamp01(dt / 1.0)))
        s = back_out(dt / 0.45, 2.2)
        icon = scaled(self.icon, 0.8 * (0.6 + 0.4 * s) * (1 + 0.03 * pulse_at(t)))
        bob = 8 * math.sin(2 * math.pi * dt / BAR)
        paste(img, icon, W / 2 - icon.width / 2, H / 2 - 150 - icon.height / 2 + bob, ease_out(dt / 0.15))
        lines = [
            (beats(1), text_img("Roseblox", "ExtraBold", 128, TEXT), H / 2 + 50),
            (beats(2), text_img("Roblox Player and Studio on Linux", "Bold", 44, ACCENT), H / 2 + 145),
            (beats(3), text_img("github.com/MadnessMyth/RoseBlox", "Regular", 42, TEXT), H / 2 + 210),
        ]
        for at, im, y in lines:
            if dt >= at:
                kk = back_out((dt - at) / 0.35, 2.0)
                sim = scaled(im, 0.85 + 0.15 * kk)
                paste(img, sim, W / 2 - sim.width / 2, y - sim.height / 2, ease_out((dt - at) / 0.15))
        if dt >= bars(1):
            a = ease_out((dt - bars(1)) / 0.4)
            n1 = text_img("Free and open source  ·  Arch Linux and CachyOS", "Regular", 26, MUTED)
            n2 = text_img("Not affiliated with Roblox Corporation.", "Regular", 24, (142, 130, 142))
            paste(img, n1, W / 2 - n1.width / 2, H - 120, a)
            paste(img, n2, W / 2 - n2.width / 2, H - 82, a)


# ---------------------------------------------------------------- audio

SR = 44100


def hz(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def add(buf: np.ndarray, at: float, sig: np.ndarray) -> None:
    a = int(round(at * SR))
    if a >= len(buf) or a + len(sig) <= 0:
        return
    if a < 0:
        sig, a = sig[-a:], 0
    b = min(len(buf), a + len(sig))
    buf[a:b] += sig[: b - a]


def tone(m: float, dur: float, decay: float, harmonics=(1.0, 0.3, 0.12)) -> np.ndarray:
    t = np.arange(int(dur * SR)) / SR
    f = hz(m)
    sig = sum(amp * np.sin(2 * np.pi * f * (k + 1) * t) for k, amp in enumerate(harmonics))
    return sig * np.minimum(1, t / 0.004) * np.exp(-t / decay)


def onepole(x: np.ndarray, cutoff) -> np.ndarray:
    cut = np.broadcast_to(np.asarray(cutoff, float), x.shape)
    alpha = 1 - np.exp(-2 * np.pi * cut / SR)
    y = np.empty_like(x)
    acc = 0.0
    for k in range(len(x)):
        acc += alpha[k] * (x[k] - acc)
        y[k] = acc
    return y


def noise(dur: float, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).normal(0, 1, int(dur * SR))


def saw(f: float, t: np.ndarray) -> np.ndarray:
    return 2 * ((f * t) % 1.0) - 1


def make_audio(path: Path) -> None:
    n = int((TOTAL + 0.2) * SR)
    music = np.zeros(n)
    sfx = np.zeros(n)
    chords = [(57, 60, 64), (53, 57, 60), (48, 52, 55), (55, 59, 62)]
    roots = [45, 41, 48, 43]

    pad_len = int((BAR + 0.5) * SR)
    pt = np.arange(pad_len) / SR
    for b in range(BARS - 1):
        ch = chords[b % 4]
        env = np.minimum(1, pt / 0.15) * np.where(pt > BAR, np.exp(-(pt - BAR) / 0.15), 1)
        sig = sum(saw(hz(m), pt) + saw(hz(m) * 1.006, pt) for m in ch)
        sig = onepole(sig, 900 + 500 * (b >= 3)) * env
        add(music, bars(b), 0.03 * sig)

    arp = [0, 1, 2, 1, 2, 0, 1, 2]
    for step in range(int(bars(15) / (BEAT / 2))):
        at = step * BEAT / 2
        b = int(at // BAR)
        if b == 2 and at - bars(2) < beats(2):
            continue
        ch = chords[b % 4]
        add(music, at, 0.045 * tone(ch[arp[step % 8]] + 12 + (12 if b >= 12 else 0) * (step % 2), 0.4, 0.13))

    for b in DRUM_BARS:
        if b > 13:
            continue
        root = roots[b % 4]
        for k in range(4):
            at = bars(b) + beats(k) + BEAT / 2
            add(music, at, 0.16 * tone(root - 12, 0.22, 0.1, (1.0, 0.6, 0.3, 0.15)))

    kt = np.arange(int(0.35 * SR)) / SR
    kick = np.sin(2 * np.pi * np.cumsum(46 + 110 * np.exp(-kt / 0.03)) / SR) * np.exp(-kt / 0.13)
    hat = np.diff(noise(0.06, 3), prepend=0) * np.exp(-np.arange(int(0.06 * SR)) / SR / 0.015)
    ohat = np.diff(noise(0.2, 5), prepend=0) * np.exp(-np.arange(int(0.2 * SR)) / SR / 0.06)
    cl = noise(0.2, 4)
    clap = onepole(cl, 3000) * np.exp(-np.arange(len(cl)) / SR / 0.06)
    for b in sorted(DRUM_BARS):
        for k in range(4):
            at = bars(b) + beats(k)
            add(music, at, 0.5 * kick)
            add(music, at + BEAT / 2, 0.06 * ohat)
            if b >= 3 and k in (1, 3):
                add(music, at, 0.2 * clap)
            if b >= 5:
                for q in (0.25, 0.75):
                    add(music, at + BEAT * q, 0.025 * hat)

    duck = np.ones(n)
    tt = np.arange(n) / SR
    for b in DRUM_BARS:
        for k in range(4):
            at = bars(b) + beats(k)
            a = int(at * SR)
            m = min(n, a + int(BEAT * SR))
            seg = tt[a:m] - at
            duck[a:m] = np.minimum(duck[a:m], 1 - 0.55 * np.exp(-seg / 0.09))
    music *= duck

    for start in (bars(1), bars(11)):
        L = int(BAR * SR)
        rt = np.arange(L) / SR
        prog = rt / BAR
        sweep = onepole(noise(BAR, 8), 300 + 6000 * prog ** 2) * prog ** 2
        rise = np.sin(2 * np.pi * np.cumsum(hz(57) * 2 ** (prog * 2)) / SR) * prog ** 3
        add(music, start, 0.22 * sweep + 0.05 * rise)
        for k in range(8 if start == bars(11) else 0):
            add(music, start + k * BEAT / 2, 0.03 * tone(69 + k, 0.2, 0.08))

    for at, big in ((SLAM, True), (bars(12), True), (bars(13), True), (bars(3), False)):
        boom_t = np.arange(int(1.6 * SR)) / SR
        boom = np.sin(2 * np.pi * np.cumsum(34 + 60 * np.exp(-boom_t / 0.08)) / SR) * np.exp(-boom_t / 0.5)
        crash = onepole(np.diff(noise(2.0, 12), prepend=0), 7000) * np.exp(-np.arange(int(2.0 * SR)) / SR / 0.7)
        add(sfx, at, (0.5 if big else 0.25) * boom)
        add(sfx, at, (0.1 if big else 0.05) * crash)
    end = np.arange(int((TOTAL - bars(13) + 0.2) * SR)) / SR
    env = np.minimum(1, end / 0.01) * np.exp(-end / 2.2)
    add(music, bars(13), 0.05 * env * sum(np.sin(2 * np.pi * hz(m) * end) for m in (45, 57, 60, 64, 69, 72)))

    wh_len = 0.3
    wh = onepole(noise(wh_len, 9), np.linspace(800, 5000, int(wh_len * SR))) * \
        np.sin(np.linspace(0, np.pi, int(wh_len * SR))) ** 2
    for seg in SEGMENTS:
        add(sfx, seg.start - wh_len * 0.6, 0.35 * wh)
    add(sfx, OUTRO - wh_len * 0.6, 0.3 * wh)

    click = np.diff(noise(0.02, 11), prepend=0) * np.exp(-np.arange(int(0.02 * SR)) / SR / 0.003)
    for seg in SEGMENTS:
        for at, _, _ in seg.clicks:
            add(sfx, seg.start + at, 0.3 * click)
        for at, _ in seg.toggles:
            add(sfx, seg.start + at, 0.06 * tone(76, 0.15, 0.05))
            add(sfx, seg.start + at + 0.06, 0.06 * tone(81, 0.2, 0.07))
        for at, _ in seg.highlights:
            add(sfx, seg.start + at, 0.035 * tone(88, 0.25, 0.09, (1.0, 0.2)))
        if seg.reveal:
            for k, m in enumerate((69, 72, 76, 81)):
                add(sfx, seg.start + seg.reveal[0] + k * BEAT / 4, 0.05 * tone(m, 0.3, 0.1))
    for at in (0.0, beats(2), beats(4)):
        add(sfx, at, 0.35 * kick[: int(0.2 * SR)])
        add(sfx, at, 0.05 * tone(57 if at == 0 else 64 if at == beats(2) else 69, 0.5, 0.2))

    mix = music + sfx
    t = np.arange(n) / SR
    mix *= np.clip((TOTAL - t) / 0.7, 0, 1)
    mix = np.tanh(mix * 1.5) / np.tanh(1.5)
    mix *= 0.89 / max(1e-9, np.abs(mix).max())
    pcm = (mix * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(np.repeat(pcm[:, None], 2, axis=1).tobytes())


# ---------------------------------------------------------------- output

POSTER_T = bars(12) + beats(2.6)


def render() -> None:
    r = Renderer()
    poster = r.frame(POSTER_T)
    poster.save(HERE / "roseblox-sneak-peek.jpg", quality=92)
    audio = HERE / "work-music.wav"
    make_audio(audio)
    n = int(round(TOTAL * FPS))
    out = HERE / "roseblox-sneak-peek.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", str(audio), "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-t", f"{n / FPS:.3f}", "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin
    for k in range(n):
        img = poster if k == 0 else r.frame(k / FPS)
        proc.stdin.write(img.tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise SystemExit("ffmpeg failed")
    audio.unlink()


def stills(times: list[float] | None) -> None:
    r = Renderer()
    d = HERE / "stills"
    d.mkdir(exist_ok=True)
    if not times:
        times = [beats(1), beats(5), SLAM + 0.05, SLAM + beats(3)]
        for s in SEGMENTS:
            times += [s.start + 0.07, s.start + 0.5, s.start + bars(1) - 0.3, s.start + bars(2) - 0.3]
        times += [OUTRO + 0.3, OUTRO + bars(2)]
    for t in times:
        r.frame(t).save(d / f"t{t:06.2f}.jpg", quality=85)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stills", nargs="*", type=float)
    args = ap.parse_args()
    if args.stills is not None:
        stills(args.stills)
    else:
        render()


if __name__ == "__main__":
    main()
