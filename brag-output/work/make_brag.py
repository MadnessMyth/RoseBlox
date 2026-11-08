#!/usr/bin/env python3
"""Render the Roseblox brag video (see ../brag-plan.md).

Usage: python3 brag-output/work/make_brag.py [--stills] [--format landscape|vertical]

Needs Pillow, NumPy, ffmpeg and the Cantarell fonts. Every frame is a pure
function of time. Writes brag-output/brag.mp4 (or brag-vertical.mp4) and
brag.jpg; --stills writes preview frames to work/stills/ instead.
"""

from __future__ import annotations

import argparse
import math
import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

WORK = Path(__file__).resolve().parent
OUT = WORK.parent
ROOT = OUT.parent
SHOTS = ROOT / "docs" / "screenshots"
FONT_DIR = Path("/usr/share/fonts/opentype/cantarell")

FPS = 30
BPM = 120
BEAT = 60 / BPM
DIP = 0.25
ACCENT = (226, 92, 152)
BG_DARK = (22, 17, 26)
TEXT = (246, 238, 244)
MUTED = (190, 176, 188)
PAGE_BG = (29, 26, 32)


@dataclass
class Click:
    at: float
    x: int
    y: int


@dataclass
class Scene:
    beats: int
    eyebrow: str
    title: str
    subtitle: str = ""
    shot: str | None = None
    kind: str = "shot"
    highlights: list[tuple[float, tuple[int, int, int, int]]] = field(default_factory=list)
    clicks: list[Click] = field(default_factory=list)
    reveal: tuple[float, list[tuple[int, int, int, int]]] | None = None
    crop: tuple[int, int, int, int] | None = None
    portrait_crop: tuple[int, int, int, int] | None = None

    @property
    def duration(self) -> float:
        return self.beats * BEAT


# Times are local to the scene, in seconds; boxes are screenshot pixels.
SCENES = [
    Scene(4, "", "Roblox on Linux", "that feels native.", kind="hook"),
    Scene(6, "ROSEBLOX", "Player and Studio, one click away", "", "07-home.png",
          clicks=[Click(2.0, 296, 335)], crop=(0, 0, 1292, 660), portrait_crop=(170, 60, 1292, 700)),
    Scene(7, "PERFORMANCE", "Tune it like a native game",
          "Refresh rate  ·  GameMode  ·  MangoHud", "14-performance.png",
          highlights=[(1.0, (231, 501, 1212, 553)), (1.5, (231, 611, 1212, 663)),
                      (2.0, (231, 665, 1212, 717))], portrait_crop=(205, 200, 1245, 750)),
    Scene(7, "MODS", "Make Roblox yours",
          "Fonts  ·  cursors  ·  skyboxes  ·  the classic oof", "17-mods.png",
          highlights=[(1.0, (231, 273, 1231, 326)), (1.5, (231, 326, 1231, 380)),
                      (2.0, (231, 380, 1231, 434)), (2.5, (231, 631, 1231, 683))],
          crop=(0, 0, 1264, 730), portrait_crop=(205, 80, 1264, 720)),
    Scene(7, "WHILE PLAYING", "Show friends what you're playing",
          "Discord presence  ·  Join Server  ·  server location", "16-while-playing.png",
          highlights=[(1.0, (231, 455, 1224, 509)), (1.5, (231, 509, 1224, 563)),
                      (2.0, (231, 563, 1224, 617))], portrait_crop=(205, 300, 1257, 750)),
    Scene(7, "NEW IN 0.4.2", "Two Players, side by side",
          "Duplicate an installation, launch both.", "22-player-duplicate.png",
          clicks=[Click(1.0, 1221, 358), Click(2.5, 1113, 453)],
          reveal=(1.5, [(230, 404, 1396, 502), (228, 278, 1010, 298), (228, 200, 1040, 226)]),
          crop=(0, 0, 1428, 560), portrait_crop=(200, 60, 1428, 520)),
    Scene(8, "", "Roseblox", "github.com/MadnessMyth/RoseBlox", kind="cta"),
]


def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / f"Cantarell-{weight}.otf"), size)


def clamp01(x: float) -> float:
    return min(max(x, 0.0), 1.0)


def ease_out(x: float) -> float:
    return 1 - (1 - clamp01(x)) ** 3


def ease_in_out(x: float) -> float:
    x = clamp01(x)
    return 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2


def background(w: int, h: int) -> Image.Image:
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    base = np.array(BG_DARK, np.float32)
    glow1 = np.exp(-(((xx - w * 0.15) / (w * 0.55)) ** 2 + ((yy - h * 0.05) / (h * 0.6)) ** 2))
    glow2 = np.exp(-(((xx - w * 0.95) / (w * 0.45)) ** 2 + ((yy - h * 1.0) / (h * 0.5)) ** 2))
    pink = np.array((120, 34, 78), np.float32)
    violet = np.array((60, 30, 90), np.float32)
    img = base + glow1[..., None] * (pink - base) * 0.85 + glow2[..., None] * (violet - base) * 0.7
    img += np.random.default_rng(7).normal(0, 1.6, img.shape).astype(np.float32)
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")


def with_alpha(img: Image.Image, a: float) -> Image.Image:
    if a >= 1:
        return img
    r, g, b, al = img.split()
    al = al.point(lambda v: int(v * max(a, 0.0)))
    return Image.merge("RGBA", (r, g, b, al))


def wrap(text: str, f: ImageFont.FreeTypeFont, width: int) -> list[str]:
    out: list[str] = []
    for para in text.split("\n"):
        line = ""
        for word in para.split(" "):
            trial = f"{line} {word}" if line else word
            if f.getlength(trial) <= width or not line:
                line = trial
            else:
                out.append(line)
                line = word
        out.append(line)
    return out


def text_lines(text: str, f: ImageFont.FreeTypeFont, color, width: int) -> Image.Image:
    lines = wrap(text, f, width)
    asc, desc = f.getmetrics()
    lh = asc + desc
    img = Image.new("RGBA", (width, lh * len(lines) + 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for k, ln in enumerate(lines):
        d.text(((width - d.textlength(ln, font=f)) / 2, k * lh), ln, font=f, fill=color)
    return img


def eyebrow_img(text: str, size: int, width: int) -> Image.Image:
    f = font("Bold", size)
    asc, desc = f.getmetrics()
    img = Image.new("RGBA", (width, asc + desc + 22), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    tracking = size * 0.12
    total = sum(f.getlength(c) for c in text) + tracking * (len(text) - 1)
    x = (width - total) / 2
    for c in text:
        d.text((x, 0), c, font=f, fill=ACCENT)
        x += f.getlength(c) + tracking
    bx = (width - 56) / 2
    d.rounded_rectangle((bx, asc + desc + 10, bx + 56, asc + desc + 15), 3, fill=ACCENT)
    return img


class ShotCard:
    """A screenshot card whose overlays (highlights, reveal masks) depend on time."""

    def __init__(self, scene: Scene, max_w: int, max_h: int, crop):
        src = Image.open(SHOTS / scene.shot).convert("RGB")
        self.ox, self.oy = 0, 0
        if crop:
            self.ox, self.oy = crop[0], crop[1]
            src = src.crop(crop)
        self.scale = min(max_w / src.width, max_h / src.height)
        self.src = src
        self.size = (round(src.width * self.scale), round(src.height * self.scale))
        self.base = src.resize(self.size, Image.LANCZOS)
        self.scene = scene
        if scene.reveal:
            masked = src.copy()
            d = ImageDraw.Draw(masked)
            for box in scene.reveal[1]:
                d.rectangle(self.local(box), fill=PAGE_BG)
            self.masked = masked.resize(self.size, Image.LANCZOS)
        w, h = self.size
        self.radius = max(10, w // 70)
        self.mask = Image.new("L", self.size, 0)
        ImageDraw.Draw(self.mask).rounded_rectangle((0, 0, w - 1, h - 1), self.radius, fill=255)
        self.pad = 60
        frame = Image.new("RGBA", (w + self.pad * 2, h + self.pad * 2), (0, 0, 0, 0))
        shadow = Image.new("L", frame.size, 0)
        ImageDraw.Draw(shadow).rounded_rectangle(
            (self.pad, self.pad + 18, self.pad + w, self.pad + h + 18), self.radius, fill=170)
        frame.paste((0, 0, 0, 255), (0, 0), shadow.filter(ImageFilter.GaussianBlur(26)))
        self.frame = frame

    def local(self, box):
        x0, y0, x1, y1 = box
        return (x0 - self.ox, y0 - self.oy, x1 - self.ox, y1 - self.oy)

    def to_card(self, x: float, y: float) -> tuple[float, float]:
        return (self.pad + (x - self.ox) * self.scale, self.pad + (y - self.oy) * self.scale)

    def render(self, t: float) -> Image.Image:
        s = self.scene
        if s.reveal:
            a = ease_out((t - s.reveal[0]) / 0.35)
            shot = Image.blend(self.masked, self.base, a)
        else:
            shot = self.base.copy()
        shot = shot.convert("RGBA")
        overlay = Image.new("RGBA", self.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        for at, box in s.highlights:
            a = ease_out((t - at) / 0.25)
            if a <= 0:
                continue
            x0, y0, x1, y1 = [v * self.scale for v in self.local(box)]
            pulse = 1 - clamp01((t - at) / 0.5)
            d.rounded_rectangle((x0, y0, x1, y1), 8, fill=(*ACCENT, int(34 * a + 40 * pulse)))
            d.rounded_rectangle((x0, y0, x1, y1), 8, outline=(*ACCENT, int(255 * a)), width=3)
        shot.alpha_composite(overlay)
        card = self.frame.copy()
        card.paste(shot, (self.pad, self.pad), self.mask)
        w, h = self.size
        ImageDraw.Draw(card).rounded_rectangle(
            (self.pad, self.pad, self.pad + w - 1, self.pad + h - 1), self.radius,
            outline=(255, 255, 255, 40), width=2)
        return card


def cursor_sprite(scale: float) -> Image.Image:
    pts = [(0, 0), (0, 34), (9, 26), (15, 40), (21, 37), (15, 24), (27, 24)]
    s = 3
    img = Image.new("RGBA", (40 * s, 48 * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    p = [(x * s + 6, y * s + 6) for x, y in pts]
    d.polygon(p, fill=(255, 255, 255, 255), outline=(20, 16, 22, 255))
    d.line(p + [p[0]], fill=(20, 16, 22, 255), width=s * 2, joint="curve")
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).polygon([(x + 4, y + 6) for x, y in p], fill=(0, 0, 0, 110))
    shadow = shadow.filter(ImageFilter.GaussianBlur(5))
    shadow.alpha_composite(img)
    size = (round(40 * scale), round(48 * scale))
    return shadow.resize(size, Image.LANCZOS)


class Renderer:
    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.portrait = h > w
        self.bg = background(w, h).convert("RGBA")
        icon = Image.open(WORK / "icon.png").convert("RGBA")
        self.icon_big = icon.resize((280, 280), Image.LANCZOS)
        self.cursor = cursor_sprite(1.15 if self.portrait else 1.0)
        self.cache: dict[int, dict] = {}

    def prepare(self, i: int) -> dict:
        if i in self.cache:
            return self.cache[i]
        s, w, h = SCENES[i], self.w, self.h
        p: dict = {}
        if s.kind == "shot":
            tw = w - 160 if self.portrait else 1500
            sizes = (34, 76, 38) if self.portrait else (26, 64, 32)
            p["eyebrow"] = eyebrow_img(s.eyebrow, sizes[0], tw)
            p["title"] = text_lines(s.title, font("ExtraBold", sizes[1]), TEXT, tw)
            p["sub"] = text_lines(s.subtitle, font("Regular", sizes[2]), MUTED, tw) if s.subtitle else None
            if self.portrait:
                p["card"] = ShotCard(s, w - 40, int(h * 0.55), s.portrait_crop)
            else:
                p["card"] = ShotCard(s, 1480, int(h * 0.64), s.crop)
        elif s.kind == "hook":
            big = 150 if self.portrait else 132
            p["title"] = text_lines(s.title, font("ExtraBold", big), TEXT, w - 120)
            p["sub"] = text_lines(s.subtitle, font("Bold", int(big * 0.62)), ACCENT, w - 120)
        else:
            p["title"] = text_lines(s.title, font("ExtraBold", 140 if self.portrait else 128), TEXT, w - 120)
            p["tag"] = text_lines("Roblox Player and Studio on Linux", font("Bold", 46 if self.portrait else 40),
                                  ACCENT, w - 120)
            p["url"] = text_lines(s.subtitle, font("Regular", 44 if self.portrait else 38), TEXT, w - 120)
            small = font("Regular", 30 if self.portrait else 24)
            p["note1"] = text_lines("Free and open source  ·  Arch Linux and CachyOS", small, MUTED, w - 80)
            p["note2"] = text_lines("Not affiliated with Roblox Corporation.", small, (140, 128, 140), w - 80)
        self.cache[i] = p
        return p

    def place(self, img, layer, x, y, a):
        if layer is not None and a > 0:
            img.alpha_composite(with_alpha(layer, a), (int(x), int(y)))

    def frame(self, i: int, t: float) -> Image.Image:
        s = SCENES[i]
        p = self.prepare(i)
        w, h = self.w, self.h
        img = self.bg.copy()

        def rise(start: float, dur: float = 0.45) -> tuple[float, int]:
            a = ease_out((t - start) / dur)
            return a, int((1 - a) * 34)

        if s.kind == "hook":
            ia = ease_out(t / 0.35)
            pop = 1 + 0.18 * math.sin(math.pi * clamp01(t / 0.35)) if t < 0.35 else 1.0
            size = int(280 * (0.7 + 0.3 * ia) * pop)
            ib = self.icon_big.resize((size, size), Image.LANCZOS)
            gap = 50
            block = 280 + gap + p["title"].height + p["sub"].height
            y0 = (h - block) // 2
            self.place(img, ib, (w - size) // 2, y0 + (280 - size) // 2, ia)
            a, dy = rise(0.1)
            ty = y0 + 280 + gap
            self.place(img, p["title"], (w - p["title"].width) // 2, ty + dy, a)
            a, dy = rise(0.75)
            self.place(img, p["sub"], (w - p["sub"].width) // 2, ty + p["title"].height + dy, a)
        elif s.kind == "shot":
            eb, ti, su = p["eyebrow"], p["title"], p["sub"]
            top = int(h * 0.09) if self.portrait else 46
            a, dy = rise(0.05)
            self.place(img, eb, (w - eb.width) // 2, top + dy, a)
            a, dy = rise(0.12)
            ty = top + eb.height + 6
            self.place(img, ti, (w - ti.width) // 2, ty + dy, a)
            text_bottom = ty + ti.height
            if su is not None:
                a, dy = rise(0.3)
                self.place(img, su, (w - su.width) // 2, text_bottom + 4 + dy, a)
                text_bottom += su.height + 4
            sc: ShotCard = p["card"]
            card = sc.render(t)
            zoom = 1.0 + 0.03 * (t / s.duration)
            cw, ch = int(card.width * zoom), int(card.height * zoom)
            card = card.resize((cw, ch), Image.BILINEAR)
            ca = ease_out((t - 0.15) / 0.5)
            area_top = text_bottom - 20
            area_h = h - area_top - (int(h * 0.05) if self.portrait else 0)
            cx = (w - cw) // 2
            cy = area_top + (area_h - ch) // 2 + int((1 - ca) * 70)
            self.place(img, card, cx, cy, ca)
            self.draw_cursor(img, s, sc, t, cx, cy, zoom)
        else:
            ia = ease_out(t / 0.4)
            pop = 1 + 0.12 * math.sin(math.pi * clamp01(t / 0.4)) if t < 0.4 else 1.0
            breathe = 1 + 0.015 * math.sin(2 * math.pi * t / (BEAT * 4))
            size = int(240 * (0.7 + 0.3 * ia) * pop * breathe)
            ib = self.icon_big.resize((size, size), Image.LANCZOS)
            layers = [p["title"], p["tag"], p["url"]]
            block = 240 + 40 + sum(l.height for l in layers) + 30
            y0 = (h - block) // 2 - (80 if self.portrait else 50)
            self.place(img, ib, (w - size) // 2, y0 + (240 - size) // 2, ia)
            y = y0 + 240 + 40
            for k, layer in enumerate(layers):
                a, dy = rise(0.15 + 0.25 * k)
                self.place(img, layer, (w - layer.width) // 2, y + dy, a)
                y += layer.height + (12 if k == 1 else 0)
            a, _ = rise(1.0, 0.6)
            ny = h - (260 if self.portrait else 130)
            self.place(img, p["note1"], (w - p["note1"].width) // 2, ny, a)
            self.place(img, p["note2"], (w - p["note2"].width) // 2, ny + p["note1"].height + 6, a)

        if i < len(SCENES) - 1 and t > s.duration - DIP:
            k = ease_in_out((t - (s.duration - DIP)) / DIP)
            img = Image.blend(img, self.bg, k)
        return img.convert("RGB")

    def draw_cursor(self, img, s: Scene, sc: ShotCard, t: float, cx: int, cy: int, zoom: float):
        if not s.clicks:
            return
        pts = []
        for c in s.clicks:
            x, y = sc.to_card(c.x, c.y)
            pts.append((c.at, cx + x * zoom, cy + y * zoom))
        enter_at = max(0.4, pts[0][0] - 0.8)
        if t < enter_at:
            return
        sx, sy = pts[0][1] + 260, pts[0][2] + 200
        x, y = sx, sy
        prev_t, prev_x, prev_y = enter_at, sx, sy
        for at, px, py in pts:
            move_start = max(prev_t + 0.15, at - 0.65)
            if t <= at:
                k = ease_in_out((t - move_start) / (at - 0.08 - move_start))
                x, y = prev_x + (px - prev_x) * k, prev_y + (py - prev_y) * k
                break
            prev_t, prev_x, prev_y = at, px, py
            x, y = px, py
        a = ease_out((t - enter_at) / 0.25)
        last = pts[-1][0]
        if t > last + 0.6:
            a *= 1 - clamp01((t - last - 0.6) / 0.3)
        press = 1.0
        for at, px, py in pts:
            dt = t - at
            if -0.06 <= dt <= 0.12:
                press = 0.86
            if 0 <= dt <= 0.5:
                r = 10 + 46 * ease_out(dt / 0.5)
                ring = Image.new("RGBA", img.size, (0, 0, 0, 0))
                ImageDraw.Draw(ring).ellipse((px - r, py - r, px + r, py + r),
                                             outline=(*ACCENT, int(230 * (1 - dt / 0.5))), width=4)
                img.alpha_composite(ring)
        cur = self.cursor
        if press != 1.0:
            cur = cur.resize((int(cur.width * press), int(cur.height * press)), Image.LANCZOS)
        self.place(img, cur, x - 6 * press, y - 6 * press, a)


def starts() -> list[float]:
    out, t = [], 0.0
    for s in SCENES:
        out.append(t)
        t += s.duration
    return out


def total_duration() -> float:
    return sum(s.duration for s in SCENES)


def frame_at(r: Renderer, t: float, st: list[float]) -> Image.Image:
    i = max(j for j, v in enumerate(st) if v <= t + 1e-9)
    img = r.frame(i, t - st[i])
    total = total_duration()
    if t > total - 0.5:
        img = Image.blend(Image.new("RGB", img.size, (0, 0, 0)), img, clamp01((total - t) / 0.5))
    return img


# ---------------------------------------------------------------- audio

SR = 44100


def hz(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def add(buf: np.ndarray, at: float, sig: np.ndarray) -> None:
    a = int(round(at * SR))
    if a >= len(buf):
        return
    b = min(len(buf), a + len(sig))
    buf[a:b] += sig[: b - a]


def tone(m: float, dur: float, decay: float, harmonics=(1.0, 0.3, 0.12)) -> np.ndarray:
    t = np.arange(int(dur * SR)) / SR
    f = hz(m)
    sig = sum(amp * np.sin(2 * np.pi * f * (k + 1) * t) for k, amp in enumerate(harmonics))
    env = np.minimum(1, t / 0.004) * np.exp(-t / decay)
    return sig * env


def lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    rc = 1 / (2 * np.pi * cutoff)
    alpha = (1 / SR) / (rc + 1 / SR)
    y = np.empty_like(x)
    acc = 0.0
    for k in range(len(x)):
        acc += alpha * (x[k] - acc)
        y[k] = acc
    return y


def highpass_noise(dur: float, seed: int) -> np.ndarray:
    n = np.random.default_rng(seed).normal(0, 1, int(dur * SR))
    return np.diff(n, prepend=0) * 0.5


def make_audio(path: Path) -> None:
    total = total_duration()
    n = int((total + 0.1) * SR)
    music = np.zeros(n)
    sfx = np.zeros(n)
    bar = BEAT * 4
    chords = [(57, 60, 64), (53, 57, 60), (48, 52, 55), (55, 59, 62)]  # Am F C G
    roots = [45, 41, 48, 43]
    st = starts()
    outro = st[-1]
    reveal = st[1]
    groove = st[2]

    pad_t = np.arange(int((bar + 0.6) * SR)) / SR
    for k in range(int(math.ceil(outro / bar))):
        ch = chords[k % 4]
        env = np.minimum(1, pad_t / 0.3) * np.where(pad_t > bar, np.exp(-(pad_t - bar) / 0.2), 1)
        sig = sum(np.sin(2 * np.pi * hz(m) * pad_t) + 0.35 * np.sin(2 * np.pi * hz(m) * 2.004 * pad_t)
                  for m in ch)
        add(music, k * bar, 0.035 * env * sig)

    arp = [0, 1, 2, 1, 0, 2, 1, 2]
    for step in range(int(outro / (BEAT / 2))):
        at = step * BEAT / 2
        ch = chords[int(at // bar) % 4]
        m = ch[arp[step % 8]] + 12
        add(music, at, 0.05 * tone(m, 0.45, 0.16))

    for step in range(int((outro - reveal) / (BEAT / 2))):
        at = reveal + step * BEAT / 2
        root = roots[int(at // bar) % 4]
        add(music, at, 0.11 * tone(root, 0.24, 0.12, (1.0, 0.5, 0.25, 0.1)))

    kick_t = np.arange(int(0.3 * SR)) / SR
    kick_f = 48 + 90 * np.exp(-kick_t / 0.025)
    kick = np.sin(2 * np.pi * np.cumsum(kick_f) / SR) * np.exp(-kick_t / 0.11)
    for b in range(int(round((outro - reveal) / BEAT))):
        add(music, reveal + b * BEAT, 0.42 * kick)

    hat = highpass_noise(0.05, 3) * np.exp(-np.arange(int(0.05 * SR)) / SR / 0.012)
    clap_n = highpass_noise(0.18, 4)
    clap = lowpass(clap_n, 3500) * np.exp(-np.arange(len(clap_n)) / SR / 0.05)
    for b in range(int(round((outro - groove) / BEAT))):
        at = groove + b * BEAT
        add(music, at + BEAT / 2, 0.05 * hat)
        if b % 2 == 1:
            add(music, at, 0.16 * clap)

    end_ch = (45, 57, 60, 64, 69)
    ring = np.arange(int((total - outro + 0.1) * SR)) / SR
    env = np.minimum(1, ring / 0.01) * np.exp(-ring / 1.4)
    sig = sum(np.sin(2 * np.pi * hz(m) * ring) + 0.25 * np.sin(2 * np.pi * hz(m) * 2 * ring) for m in end_ch)
    add(music, outro, 0.07 * env * sig)
    add(music, outro, 0.5 * kick)
    add(music, outro, 0.14 * tone(33, 1.6, 0.7, (1.0, 0.4)))

    whoosh_n = highpass_noise(0.35, 9)
    wt = np.arange(len(whoosh_n)) / SR
    whoosh = lowpass(whoosh_n, 2500) * (wt / wt[-1]) ** 2
    for k, s0 in enumerate(st[1:], start=1):
        add(sfx, s0 - len(whoosh) / SR, 0.12 * whoosh)

    click_n = highpass_noise(0.02, 11) * np.exp(-np.arange(int(0.02 * SR)) / SR / 0.003)
    for i, s in enumerate(SCENES):
        for c in s.clicks:
            add(sfx, st[i] + c.at, 0.25 * click_n)
            add(sfx, st[i] + c.at, 0.06 * tone(88, 0.12, 0.04))
        for at, _ in s.highlights:
            add(sfx, st[i] + at, 0.035 * tone(81, 0.25, 0.08, (1.0, 0.2)))
        if s.reveal:
            add(sfx, st[i] + s.reveal[0], 0.05 * tone(76, 0.35, 0.12, (1.0, 0.3)))
            add(sfx, st[i] + s.reveal[0] + BEAT / 4, 0.05 * tone(81, 0.4, 0.14, (1.0, 0.3)))
    add(sfx, 0.0, 0.05 * tone(81, 0.5, 0.2, (1.0, 0.3)))

    mix = music + sfx
    t = np.arange(n) / SR
    mix *= np.clip(np.minimum(1, (total - t) / 0.5), 0, 1)
    mix = np.tanh(mix * 1.6) / np.tanh(1.6)
    mix *= 0.89 / max(1e-9, np.abs(mix).max())
    pcm = (mix * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(np.repeat(pcm[:, None], 2, axis=1).tobytes())


# ---------------------------------------------------------------- output

def render(w: int, h: int, out: Path, poster_t: float, poster_path: Path) -> None:
    r = Renderer(w, h)
    st = starts()
    poster = frame_at(r, poster_t, st)
    poster.save(poster_path, quality=92)
    audio = WORK / "music.wav"
    make_audio(audio)
    n = int(round(total_duration() * FPS))
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(FPS), "-i", "-",
        "-i", str(audio),
        "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-t", f"{n / FPS:.3f}", "-movflags", "+faststart", str(out),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin
    for k in range(n):
        img = poster if k == 0 else frame_at(r, k / FPS, st)
        proc.stdin.write(img.tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise SystemExit(f"ffmpeg failed for {out}")


def stills(w: int, h: int, tag: str) -> None:
    r = Renderer(w, h)
    st = starts()
    d = WORK / "stills"
    d.mkdir(exist_ok=True)
    for i, s in enumerate(SCENES):
        for label, lt in (("settled", s.duration - DIP - 0.05), ("early", 0.3)):
            frame_at(r, st[i] + lt, st).save(d / f"{tag}-{i}-{label}.jpg", quality=85)
        if i:
            frame_at(r, st[i] - 0.1, st).save(d / f"{tag}-{i}-cut.jpg", quality=85)
    for i, s in enumerate(SCENES):
        for c in s.clicks:
            frame_at(r, st[i] + c.at + 0.1, st).save(d / f"{tag}-{i}-click{c.at}.jpg", quality=85)


POSTER_T = starts()[5] + 3.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stills", action="store_true")
    ap.add_argument("--format", choices=["landscape", "vertical"], default="landscape")
    args = ap.parse_args()
    w, h = (1920, 1080) if args.format == "landscape" else (1080, 1920)
    if args.stills:
        stills(w, h, args.format)
        return
    name = "brag" if args.format == "landscape" else "brag-vertical"
    render(w, h, OUT / f"{name}.mp4", POSTER_T, OUT / f"{name}.jpg")


if __name__ == "__main__":
    main()
