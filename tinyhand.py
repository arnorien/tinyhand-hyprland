#!/usr/bin/env python3
"""TinyHand pour Hyprland.

Portage de https://github.com/nilbuild/tinyhand (MIT, Kamran Ahmed).

Usage :
    tinyhand.py toggle        active/désactive la main (démarre le démon au besoin)
    tinyhand.py through       clic réel : les clics atteignent les applications, la main reste
    tinyhand.py clear         efface l'encre
    tinyhand.py pick          choisit la main et les options dans rofi
    tinyhand.py hand NOM      change de main
    tinyhand.py quit          arrête le démon
    tinyhand.py run [--activate] [--demo]   lance le démon au premier plan

Réglages : ~/.config/tinyhand/config.toml (créé au premier lancement, relu à chaud).

Fonctionnement :
- un overlay layer-shell transparent aux clics dessine la main ;
- quand la main est active, des binds Hyprland (Lua) capturent le clic gauche sans
  modificateur et ajoutent une lettre dans un fichier d'événements que le démon relit
  à chaque image ;
- la position du curseur est lue sur le socket Hyprland (`cursorpos`).
- à l'activation, la main s'ouvre sur l'écran externe (projecteur) s'il y en a un, avec focus et
  curseur au centre ; elle suit ensuite le curseur d'un écran à l'autre.

Sécurités :
- un minuteur Lua dans Hyprland surveille un fichier de battement de cœur : si le démon
  ne l'actualise plus pendant 3 s, Hyprland retire lui-même les binds et réaffiche le curseur ;
- Super+Ctrl+Alt+Échap fait la même chose immédiatement, sans dépendre du démon ;
- la main se coupe quand l'écran est verrouillé (hyprlock).
"""

import json
import math
import os
import re
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HANDS_DIR = ROOT / "assets" / "hands"
THUMBS_DIR = ROOT / "assets" / "thumbs"
SOUNDS_DIR = ROOT / "assets" / "sounds"
RUN_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "tinyhand"
EVENTS = RUN_DIR / "events"
HEARTBEAT = RUN_DIR / "heartbeat"
PIDFILE = RUN_DIR / "pid"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tinyhand" / "config.toml"

HANDS = ["glove", "blue", "choco", "chrome", "coral", "glass", "jelly", "lavender", "lemon", "mint"]
TITLES = {
    "glove": "Cursor glove", "blue": "Sky", "choco": "Chocolate", "chrome": "Chrome", "coral": "Coral vinyl",
    "glass": "Glass", "jelly": "Jelly", "lavender": "Lavender toy", "lemon": "Lemon", "mint": "Mint",
}
INK = {  # couleurs d'encre de l'original (PointerStyle.swift)
    "glove": 0x8754F2, "blue": 0x2F86F0, "choco": 0x9A5E2C, "chrome": 0x6B7380, "coral": 0xFF291F,
    "glass": 0x1597E5, "jelly": 0xA33FE6, "lavender": 0x8754F2, "lemon": 0xF2B705, "mint": 0x16A87A,
}
SOUNDS = ["thock", "click", "pop", "boop", "squeak", "wood"]

DEFAULT_CONFIG = """\
# TinyHand — réglages (relus à chaud par le démon)

hand = "glove"          # glove, blue, choco, chrome, coral, glass, jelly, lavender, lemon, mint
size = 0.13             # hauteur de la main, en fraction de la hauteur de l'écran (0.06 à 0.25)
shadow = true           # ombre portée sous la main
sound = true            # son à chaque tape
tap_sound = "thock"     # thock, click, pop, boop, squeak, wood
ink = "match"           # "match" (couleur de la main) ou une couleur "#RRGGBB"
fade_seconds = 0        # 0 = l'encre reste jusqu'à Échap ou la fermeture ; sinon délai d'effacement en s
"""
DEFAULTS = tomllib.loads(DEFAULT_CONFIG)

# Constantes de l'original (ArmView.swift), réglées par tick à 120 Hz.
MIN_ROD = 0.05
GRIP_LEAN = 0.30
GRIP_DEPTH = 0.12
GRIP_FOLLOW = 0.08
PRESS_DECAY = 0.18
LIFT = 0.06
FADE_DURATION = 0.6


# --------------------------------------------------------------------------- réglages

def load_config() -> dict:
    if not CONFIG.exists():
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(DEFAULT_CONFIG)
    cfg = dict(DEFAULTS)
    try:
        cfg.update(tomllib.loads(CONFIG.read_text()))
    except (tomllib.TOMLDecodeError, OSError) as e:
        print(f"tinyhand : {CONFIG} illisible ({e}), réglages par défaut", file=sys.stderr)
    if cfg["hand"] not in HANDS:
        cfg["hand"] = DEFAULTS["hand"]
    if cfg["tap_sound"] not in SOUNDS:
        cfg["tap_sound"] = DEFAULTS["tap_sound"]
    try:
        cfg["size"] = min(0.25, max(0.06, float(cfg["size"])))
        cfg["fade_seconds"] = max(0.0, float(cfg["fade_seconds"]))
    except (TypeError, ValueError):
        cfg["size"], cfg["fade_seconds"] = DEFAULTS["size"], DEFAULTS["fade_seconds"]
    return cfg


def set_config(key: str, value) -> None:
    """Modifie une clé du fichier en gardant les commentaires."""
    load_config()  # crée le fichier au besoin
    text = CONFIG.read_text()
    literal = json.dumps(value) if isinstance(value, str) else str(value).lower()
    pattern = re.compile(rf"^{key}\s*=\s*[^#\n]*?(\s*#.*)?$", re.M)
    if pattern.search(text):
        text = pattern.sub(lambda m: f"{key} = {literal}" + (m.group(1) or ""), text, count=1)
    else:
        text = text.rstrip("\n") + f"\n{key} = {literal}\n"
    CONFIG.write_text(text)


# --------------------------------------------------------------------------- IPC Hyprland

def hypr_socket() -> str:
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not sig:
        sys.exit("tinyhand : Hyprland introuvable (HYPRLAND_INSTANCE_SIGNATURE absent)")
    return f"{os.environ['XDG_RUNTIME_DIR']}/hypr/{sig}/.socket.sock"


def hypr(msg: str) -> str:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(hypr_socket())
        s.sendall(msg.encode())
        out = b""
        while chunk := s.recv(4096):
            out += chunk
        return out.decode(errors="replace")
    finally:
        s.close()


def lua(code: str) -> None:
    out = hypr("eval " + code)
    if out.strip() != "ok":
        print("tinyhand : erreur Lua :", out.strip(), file=sys.stderr)


def lua_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def daemon_alive() -> bool:
    try:
        pid = int(PIDFILE.read_text())
        return b"tinyhand" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return False


def send(cmd: str) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    with open(EVENTS, "a") as f:
        f.write(cmd + "\n")


def notify(text: str) -> None:
    subprocess.Popen(["notify-send", "-t", "1800", "-r", "4243", "-a", "TinyHand", "TinyHand", text],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


LUA_SETUP = """
TH = TH or {{}}
TH.ev, TH.hb = {ev}, {hb}
function TH.emit(s) local f = io.open(TH.ev, "a") if f then f:write(s, "\\n") f:close() end end
function TH.release()
  TH.active = false
  pcall(hl.unbind, "mouse:272")
  pcall(hl.unbind, "Escape")
  hl.config({{cursor = {{invisible = false}}}})
end
function TH.capture(demo, through)
  local fresh = not TH.active
  pcall(hl.unbind, "mouse:272")
  pcall(hl.unbind, "Escape")
  TH.active = true
  if not demo then
    local opts = through and {{non_consuming = true}} or {{}}
    local up = through and {{non_consuming = true, release = true}} or {{release = true}}
    hl.bind("mouse:272", function() TH.emit("d") end, opts)
    hl.bind("mouse:272", function() TH.emit("u") end, up)
    hl.config({{cursor = {{invisible = true}}}})
  end
  hl.bind("Escape", function() TH.emit("x") end)  -- Échap ferme la main, même en clic réel
  if not fresh then return end
  TH.gen = (TH.gen or 0) + 1
  local gen, last, stale = TH.gen, nil, 0
  local function check()
    if not TH.active or TH.gen ~= gen then return end
    local f = io.open(TH.hb, "r")
    local v = f and f:read("a")
    if f then f:close() end
    if v == last then stale = stale + 1 else stale, last = 0, v end
    if stale >= 3 then TH.release() TH.emit("w") return end
    hl.timer(check, {{timeout = 1000, type = "oneshot"}})
  end
  hl.timer(check, {{timeout = 1000, type = "oneshot"}})
end
pcall(hl.unbind, "SUPER + CTRL + ALT + Escape")
hl.bind("SUPER + CTRL + ALT + Escape", function() TH.release() TH.emit("k") end)
if not TH.layerRule then
  TH.layerRule = hl.layer_rule({{ name = "tinyhand-no-anim", match = {{ namespace = "^tinyhand$" }}, no_anim = true }})
end
"""

LUA_TEARDOWN = """
if TH then TH.release() end
pcall(hl.unbind, "SUPER + CTRL + ALT + Escape")
"""


# --------------------------------------------------------------------------- mesure des mains

_ALPHA = bytes(1 if b > 128 else 0 for b in range(256))


def measure(pb):
    """Port de HandAsset.measure : repère le bout du doigt, le poignet et la tige."""
    w, h, rs = pb.get_width(), pb.get_height(), pb.get_rowstride()
    px = pb.get_pixels()
    rows = [px[y * rs + 3: y * rs + 4 * w: 4].translate(_ALPHA) for y in range(h)]
    counts = [r.count(1) for r in rows]

    def center(y):
        r = rows[y]
        lo, hi = r.find(1), r.rfind(1)
        return 0.5 if lo < 0 else (lo + hi) / 2 / w

    top = next(y for y in range(h) if counts[y] > 0)
    max_w = max(counts[top:min(h, top + int(0.45 * h))])
    fist = next(y for y in range(top, h) if counts[y] > 0.6 * max_w)
    wrist = next(y for y in range(fist, h) if counts[y] < 0.35 * max_w)
    rod_w = counts[min(h - 1, wrist + h // 50)]
    last_row = max(y for y in range(h) if counts[y] > 0)
    start = min(h - 1, wrist + h // 33)
    rod_end = next((y for y in range(start, h) if counts[y] > 1.8 * rod_w), last_row)
    return {
        "aspect": w / h,
        "hand_top": max(0, top - 0.004 * h) / h,
        "tip": top / h,
        "wrist": wrist / h,
        "rod_end": rod_end / h,
        "tip_x": center(min(h - 1, top + h // 200)),
        "rod_x": center((wrist + rod_end) // 2),
    }


# --------------------------------------------------------------------------- rofi

def pick() -> None:
    cfg = load_config()
    rows, actions = [], []
    for h in HANDS:
        mark = "● " if h == cfg["hand"] else "   "
        rows.append(f"{mark}{TITLES[h]}\0icon\x1f{THUMBS_DIR / f'{h}.png'}")
        actions.append(("hand", h))
    options = [
        (f"Son : {'activé' if cfg['sound'] else 'désactivé'}", ("sound", not cfg["sound"])),
        (f"Bruit de tape : {cfg['tap_sound']}", ("tap_sound", SOUNDS[(SOUNDS.index(cfg['tap_sound']) + 1) % len(SOUNDS)])),
        (f"Ombre : {'activée' if cfg['shadow'] else 'désactivée'}", ("shadow", not cfg["shadow"])),
        ("Taille : plus grande", ("size", round(min(0.25, cfg["size"] + 0.02), 2))),
        ("Taille : plus petite", ("size", round(max(0.06, cfg["size"] - 0.02), 2))),
        ("Ouvrir le fichier de réglages", ("edit", None)),
    ]
    for label, act in options:
        rows.append(f"{label}\0icon\x1fpreferences-system")
        actions.append(act)
    r = subprocess.run(["rofi", "-dmenu", "-i", "-show-icons", "-format", "i", "-p", "TinyHand"],
                       input="\n".join(rows), capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return
    key, value = actions[int(r.stdout.strip())]
    if key == "edit":
        subprocess.Popen(["xdg-open", str(CONFIG)])
        return
    set_config(key, value)
    if key == "tap_sound":
        subprocess.Popen(["pw-play", str(SOUNDS_DIR / f"{value}.wav")])
    if key in ("sound", "tap_sound", "shadow", "size"):
        pick()  # rouvrir pour enchaîner les réglages


# --------------------------------------------------------------------------- démon

def run(args):
    if "libgtk4-layer-shell" not in os.environ.get("LD_PRELOAD", ""):
        # gtk4-layer-shell doit être chargé avant libwayland-client.
        env = dict(os.environ, LD_PRELOAD="/usr/lib/libgtk4-layer-shell.so")
        os.execve(sys.executable, [sys.executable] + sys.argv, env)

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gsk", "4.0")
    gi.require_version("Graphene", "1.0")
    gi.require_version("GdkPixbuf", "2.0")
    gi.require_version("GLibUnix", "2.0")
    gi.require_version("Gtk4LayerShell", "1.0")
    import cairo
    from gi.repository import Gdk, GdkPixbuf, GLib, GLibUnix, Graphene, Gsk, Gtk
    from gi.repository import Gtk4LayerShell as LS

    demo = args.get("demo", False)

    def rgba(hexv, a=1.0):
        c = Gdk.RGBA()
        c.red, c.green, c.blue, c.alpha = ((hexv >> 16) & 255) / 255, ((hexv >> 8) & 255) / 255, (hexv & 255) / 255, a
        return c

    class Hand:
        def __init__(self, name, screen_h, size):
            pb = GdkPixbuf.Pixbuf.new_from_file(str(HANDS_DIR / f"{name}.png"))
            self.name = name
            self.m = measure(pb)
            # Texture réduite à la taille d'affichage (comme updateDisplayImage), arrondie à 64 px.
            rest = size * screen_h / (self.m["wrist"] - self.m["hand_top"])
            th = min(pb.get_height(), max(64, (int(math.ceil(rest)) + 63) // 64 * 64))
            tw = max(1, round(pb.get_width() * th / pb.get_height()))
            _, png = pb.scale_simple(tw, th, GdkPixbuf.InterpType.HYPER).save_to_bufferv("png", [], [])
            self.texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png))

    class Stroke:
        def __init__(self, p, color, width):
            self.points, self.color, self.width, self.ended = [p], color, width, None

    class HandView(Gtk.Widget):
        def __init__(self):
            super().__init__()
            self.hand = None
            self.cfg = DEFAULTS
            self.cursor = None
            self.grip_x = None
            self.press = 0.0
            self.held = False
            self.down = None
            self.strokes = []
            self.current = None
            self.last_t = None

        def ink_color(self):
            ink = str(self.cfg["ink"])
            if re.fullmatch(r"#[0-9a-fA-F]{6}", ink):
                return rgba(int(ink[1:], 16))
            return rgba(INK[self.hand.name])

        # --- logique (repère y vers le haut, comme l'original) ---
        def step(self, cursor, now):
            W, H = self.get_width(), self.get_height()
            dt = 1 / 60 if self.last_t is None else min(0.1, now - self.last_t)
            self.last_t = now
            ticks = dt * 120
            self.cursor = cursor
            target = min(max(cursor[0] + GRIP_LEAN * W, 0.15 * W), 1.05 * W)
            if self.grip_x is None:
                self.grip_x = target
            self.grip_x += (target - self.grip_x) * (1 - (1 - GRIP_FOLLOW) ** ticks)
            if not self.held:
                self.press *= (1 - PRESS_DECAY) ** ticks
            if self.held and self.down:
                d = math.dist(cursor, self.down)
                if self.current is None and d >= 4:
                    self.current = Stroke(self.down, self.ink_color(), max(3, H * 0.0055))
                    self.strokes.append(self.current)
                if self.current and math.dist(cursor, self.current.points[-1]) >= 1.5:
                    self.current.points.append(cursor)
            fade = self.cfg["fade_seconds"]
            if fade > 0:
                self.strokes = [s for s in self.strokes
                                if s.ended is None or now - s.ended < max(0.5, fade) + FADE_DURATION]

        def press_down(self, ink=True):
            self.press, self.held = 1.0, True
            self.down = self.cursor if ink else None

        def press_up(self, now):
            self.held, self.down = False, None
            if self.current:
                self.current.ended = now
                self.current = None

        def clear(self):
            self.strokes, self.current = [], None

        # --- rendu ---
        def do_snapshot(self, snap):
            if self.cursor is None or self.hand is None:
                return
            now = time.monotonic()
            for s in self.strokes:
                self.draw_stroke(snap, s, now)
            self.draw_hand(snap)

        def draw_stroke(self, snap, s, now):
            alpha = 1.0
            fade = self.cfg["fade_seconds"]
            if fade > 0 and s.ended is not None and now - s.ended > max(0.5, fade):
                t = min(1.0, (now - s.ended - max(0.5, fade)) / FADE_DURATION)
                alpha = 1 - t * t
            pts = s.points
            b = Gsk.PathBuilder.new()
            b.move_to(*pts[0])
            if len(pts) == 1:
                b.line_to(*pts[0])
            for prev, p in zip(pts, pts[1:]):
                b.quad_to(prev[0], prev[1], (prev[0] + p[0]) / 2, (prev[1] + p[1]) / 2)
            if len(pts) > 1:
                b.line_to(*pts[-1])
            stroke = Gsk.Stroke.new(s.width)
            stroke.set_line_cap(Gsk.LineCap.ROUND)
            stroke.set_line_join(Gsk.LineJoin.ROUND)
            snap.push_opacity(alpha)
            snap.append_stroke(b.to_path(), stroke, s.color)
            snap.pop()

        def draw_hand(self, snap):
            H = self.get_height()
            m = self.hand.m
            cx, cy = self.cursor[0], H - self.cursor[1]  # y vers le haut
            gx, gy = self.grip_x, -GRIP_DEPTH * H
            reach = math.hypot(cx - gx, cy - gy)
            aim = math.atan2(cy - gy, cx - gx)
            hand_pts = self.cfg["size"] * H * (1 - 0.06 * self.press)
            s = hand_pts / (m["wrist"] - m["hand_top"])  # hauteur de l'image entière
            width = s * m["aspect"]
            tip_pts = (m["wrist"] - m["tip"]) * s
            tip_off = (m["tip_x"] - m["rod_x"]) * width
            along = math.sqrt(max(0.0, reach * reach - tip_off * tip_off))
            rod_len = max(MIN_ROD * H, along - tip_pts)
            rotation = aim - math.atan2(rod_len + tip_pts, tip_off)
            lift = LIFT * H * (1 - 0.8 * self.press)

            shadow_on = self.cfg["shadow"]
            if shadow_on:
                shadow = Gsk.Shadow()
                shadow.color = rgba(0x000000, 0.26 + 0.02 * self.press)
                shadow.dx, shadow.dy = lift * 0.6, lift
                shadow.radius = max(1.0, lift * 0.35) + 5 * self.press
                snap.push_shadow([shadow])

            snap.save()
            snap.translate(Graphene.Point().init(0, H))
            snap.scale(1, -1)  # repère y vers le haut
            snap.translate(Graphene.Point().init(gx, gy))
            snap.rotate(math.degrees(rotation))
            snap.translate(Graphene.Point().init(-m["rod_x"] * width, 0))
            # Tige : lignes poignet → fin de tige de l'image, étirées sur rod_len.
            self.draw_part(snap, 0, rod_len, width, m["wrist"], m["rod_end"])
            # Main : lignes haut de main → poignet, posée au bout de la tige.
            self.draw_part(snap, rod_len, hand_pts, width, m["hand_top"], m["wrist"])
            snap.restore()
            if shadow_on:
                snap.pop()

        def draw_part(self, snap, y0, height, width, row_a, row_b):
            """Dessine les lignes [row_a, row_b] de l'image dans la bande [y0, y0+height] (repère y vers le haut)."""
            full = height / (row_b - row_a)
            snap.save()
            snap.translate(Graphene.Point().init(0, y0 + height))
            snap.scale(1, -1)  # l'image se dessine à l'endroit
            snap.push_clip(Graphene.Rect().init(0, 0, width, height))
            snap.append_scaled_texture(self.hand.texture, Gsk.ScalingFilter.TRILINEAR,
                                       Graphene.Rect().init(0, -row_a * full, width, full))
            snap.pop()
            snap.restore()

    class App:
        def __init__(self):
            self.mon = None
            focused = next(m for m in monitors() if m["focused"])
            self.mon_h = focused["height"] / focused["scale"]
            self.active = False
            self.through = False
            self.offset = 0
            self.beat = 0
            self.tick_id = None
            self.cfg_mtime = None

            RUN_DIR.mkdir(parents=True, exist_ok=True)
            EVENTS.write_text("")
            HEARTBEAT.write_text("0")
            PIDFILE.write_text(str(os.getpid()))

            css = Gtk.CssProvider()
            css.load_from_string("window.tinyhand { background: transparent; }")
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.win = Gtk.Window()
            self.win.add_css_class("tinyhand")
            LS.init_for_window(self.win)
            LS.set_layer(self.win, LS.Layer.OVERLAY)
            LS.set_namespace(self.win, "tinyhand")
            for edge in (LS.Edge.TOP, LS.Edge.BOTTOM, LS.Edge.LEFT, LS.Edge.RIGHT):
                LS.set_anchor(self.win, edge, True)
            LS.set_exclusive_zone(self.win, -1)
            LS.set_keyboard_mode(self.win, LS.KeyboardMode.NONE)
            self.view = HandView()
            self.win.set_child(self.view)
            self.win.connect("map", self.on_map)
            self.reload_config()

            lua(LUA_SETUP.format(ev=lua_str(str(EVENTS)), hb=lua_str(str(HEARTBEAT))))
            GLib.timeout_add(50, self.idle_poll)
            GLib.timeout_add(1000, self.heartbeat)
            print(f"tinyhand : prêt (main {self.view.hand.name}{', démo' if demo else ''}).")

        def on_map(self, win):
            # Surface transparente aux clics : région d'entrée vide.
            win.get_surface().set_input_region(cairo.Region())

        def reload_config(self):
            try:
                mtime = CONFIG.stat().st_mtime_ns
            except FileNotFoundError:
                mtime = None
            if mtime == self.cfg_mtime and self.view.hand is not None:
                return
            self.cfg_mtime = mtime
            old = self.view.cfg
            cfg = load_config()
            self.view.cfg = cfg
            if self.view.hand is None or cfg["hand"] != self.view.hand.name or cfg["size"] != old["size"]:
                self.view.hand = Hand(cfg["hand"], self.mon_h, cfg["size"])

        # --- événements ---
        def poll_events(self):
            try:
                size = EVENTS.stat().st_size
            except FileNotFoundError:
                return
            if size < self.offset:
                self.offset = 0
            if size == self.offset:
                return
            with open(EVENTS, "rb") as f:
                f.seek(self.offset)
                data = f.read()
            self.offset += len(data)
            for line in data.decode(errors="replace").splitlines():
                self.handle(line.strip())

        def handle(self, ev):
            now = time.monotonic()
            if ev == "t":
                self.deactivate() if self.active else self.activate()
            elif ev == "x":
                # Échap : efface le dessin s'il y en a un, sinon ferme la main.
                if self.view.strokes:
                    self.view.press_up(now)
                    self.view.clear()
                else:
                    self.deactivate()
            elif ev == "k":  # urgence : Hyprland a déjà tout relâché
                self.deactivate(lua_release=False)
            elif ev == "w":
                print("tinyhand : le chien de garde Hyprland a coupé la capture", file=sys.stderr)
                self.deactivate(lua_release=False)
            elif ev == "c":
                self.view.clear()
            elif ev == "p":
                self.set_through(not self.through)
            elif ev == "q":
                self.quit()
            elif self.active and ev == "d":
                self.view.press_down(ink=not self.through)
                cfg = self.view.cfg
                if cfg["sound"] and not self.through:
                    subprocess.Popen(["pw-play", str(SOUNDS_DIR / f"{cfg['tap_sound']}.wav")],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif self.active and ev == "u":
                self.view.press_up(now)

        def idle_poll(self):
            if not self.active:
                self.poll_events()
                self.reload_config()
            return True

        def heartbeat(self):
            self.beat += 1
            HEARTBEAT.write_text(str(self.beat))
            if self.active and screen_locked():
                print("tinyhand : écran verrouillé, main coupée", file=sys.stderr)
                self.deactivate()
            return True

        # --- écrans ---
        def use_monitor(self, m):
            """Place l'overlay sur l'écran m (dict de `hyprctl monitors`)."""
            self.mon = m
            gdk_mon = next((g for g in Gdk.Display.get_default().get_monitors()
                            if g.get_connector() == m["name"]), None)
            LS.set_monitor(self.win, gdk_mon)
            self.view.grip_x = None
            self.view.press_up(time.monotonic())
            self.view.clear()
            h = m["height"] / m["scale"]
            if h != self.mon_h:
                self.mon_h = h
                self.view.hand = Hand(self.view.cfg["hand"], h, self.view.cfg["size"])

        def follow_cursor(self, x, y):
            """Comme l'original : si le curseur change d'écran, la main le suit."""
            m = self.mon
            if m["x"] <= x < m["x"] + m["width"] / m["scale"] and m["y"] <= y < m["y"] + m["height"] / m["scale"]:
                return
            for m in monitors():
                if m["x"] <= x < m["x"] + m["width"] / m["scale"] and m["y"] <= y < m["y"] + m["height"] / m["scale"]:
                    self.use_monitor(m)
                    return

        # --- activation ---
        def activate(self):
            if self.active or screen_locked():
                return
            self.active = True
            self.through = False
            self.view.last_t = None
            mons = monitors()
            target = projected(mons) or next(m for m in mons if m["focused"])
            self.use_monitor(target)
            if not target["focused"]:
                # Écran projeté : on y met le focus et le curseur, au centre.
                cx = round(target["x"] + target["width"] / target["scale"] / 2)
                cy = round(target["y"] + target["height"] / target["scale"] / 2)
                hypr(f'dispatch hl.dsp.focus({{ monitor = {lua_str(target["name"])} }})')
                hypr(f"dispatch hl.dsp.cursor.move({{ x = {cx}, y = {cy} }})")
            self.heartbeat()
            lua(f"TH.capture({'true' if demo else 'false'}, false)")
            self.view.cursor = self.cursor()
            self.win.set_visible(True)
            self.tick_id = self.view.add_tick_callback(self.tick)

        def set_through(self, on):
            if not self.active or on == self.through:
                return
            self.through = on
            self.view.press_up(time.monotonic())
            lua(f"TH.capture({'true' if demo else 'false'}, {'true' if on else 'false'})")
            notify("Clic réel : les clics atteignent les applications" if on
                   else "Retour aux tapes : les clics n'atteignent plus les applications")

        def deactivate(self, lua_release=True):
            if not self.active:
                return
            self.active = False
            self.through = False
            if lua_release:
                lua("TH.release()")
            if self.tick_id is not None:
                self.view.remove_tick_callback(self.tick_id)
                self.tick_id = None
            self.view.press_up(time.monotonic())
            self.view.clear()
            self.win.set_visible(False)

        def tick(self, widget, clock):
            try:
                self.poll_events()
                self.reload_config()
                if self.active:
                    self.view.step(self.cursor(), time.monotonic())
                    self.view.queue_draw()
            except Exception as e:  # ne jamais laisser la capture active sur une erreur
                print("tinyhand : erreur, désactivation :", e, file=sys.stderr)
                self.deactivate()
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        def cursor(self):
            x, y = (float(v) for v in hypr("cursorpos").split(","))
            if self.active:
                self.follow_cursor(x, y)
            return (x - self.mon["x"], y - self.mon["y"])

        def quit(self):
            self.deactivate()
            lua(LUA_TEARDOWN)
            loop.quit()

    def monitors():
        return [m for m in json.loads(hypr("j/monitors")) if not m.get("disabled")]

    def projected(mons):
        """Écran externe (vidéoprojecteur, moniteur) s'il y en a un et qu'il n'est pas en miroir."""
        internal = ("eDP", "LVDS", "DSI")
        ext = [m for m in mons if not m["name"].startswith(internal) and m.get("mirrorOf", "none") == "none"]
        return ext[0] if ext else None

    def screen_locked():
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    if Path(f"/proc/{pid}/comm").read_bytes() == b"hyprlock\n":
                        return True
                except OSError:
                    pass
        return False

    loop = GLib.MainLoop()
    Gtk.init()
    app = App()
    if args.get("activate"):
        GLib.idle_add(lambda: (app.activate(), GLib.SOURCE_REMOVE)[1])
    for sig in (2, 15):
        GLibUnix.signal_add(GLib.PRIORITY_HIGH, sig, lambda *_: (app.quit(), GLib.SOURCE_REMOVE)[1])
    try:
        loop.run()
    finally:
        try:
            lua(LUA_TEARDOWN)
        except OSError:
            pass
        try:
            if PIDFILE.read_text() == str(os.getpid()):
                PIDFILE.unlink()
        except OSError:
            pass


def spawn_daemon(activate: bool) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    log = open(RUN_DIR / "daemon.log", "a")
    cmd = [sys.executable, str(Path(__file__).resolve()), "run"] + (["--activate"] if activate else [])
    subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)


def main():
    argv = sys.argv[1:]
    cmd = argv[0] if argv and not argv[0].startswith("-") else "run"
    if cmd == "toggle":
        send("t") if daemon_alive() else spawn_daemon(activate=True)
    elif cmd in ("through", "clear", "quit"):
        if daemon_alive():
            send({"through": "p", "clear": "c", "quit": "q"}[cmd])
    elif cmd == "pick":
        pick()
    elif cmd == "hand":
        if len(argv) < 2 or argv[1] not in HANDS:
            sys.exit("mains disponibles : " + ", ".join(HANDS))
        set_config("hand", argv[1])
    elif cmd == "run":
        args = {}
        for a in argv[1:] if argv and argv[0] == "run" else argv:
            if a in ("--demo", "--activate"):
                args[a[2:]] = True
            else:
                sys.exit(f"option inconnue : {a}")
        if daemon_alive():
            sys.exit("tinyhand : le démon tourne déjà")
        run(args)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
