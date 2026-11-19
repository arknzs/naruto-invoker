"""Gesture Invoker: hand gesture recognition from a webcam -> emulated key presses.

Windows only (key emulation uses SendInput with hardware scan codes, so it works
in games too). Run:  python app.py
"""
import ctypes
import itertools
import json
import os
import random
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import cv2
import mediapipe as mp
import numpy as np
from PIL import Image, ImageTk

CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gestures.json")

# --------------------------------------------------------------------------
# Keyboard emulation (SendInput, scan codes)
# --------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ULONG_PTR)]


class _U(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


KEYEVENTF_EXTENDED, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x1, 0x2, 0x8
SPECIAL_VK = {
    "enter": 0x0D, "return": 0x0D, "space": 0x20, "tab": 0x09, "esc": 0x1B,
    "escape": 0x1B, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21,
    "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "shift": 0xA0, "ctrl": 0xA2, "alt": 0xA4, "win": 0x5B, "capslock": 0x14,
}
SPECIAL_VK.update({f"f{i}": 0x6F + i for i in range(1, 13)})
EXTENDED_VK = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27, 0x5B}


def key_to_vk(name):
    name = name.lower()
    if name in SPECIAL_VK:
        return SPECIAL_VK[name]
    if len(name) == 1:
        r = user32.VkKeyScanW(ord(name))
        if r != -1:
            return r & 0xFF
    raise ValueError(f"Неизвестная клавиша: {name}")


def send_key(name, down):
    vk = key_to_vk(name)
    scan = user32.MapVirtualKeyW(vk, 0)
    flags = KEYEVENTF_SCANCODE
    if vk in EXTENDED_VK:
        flags |= KEYEVENTF_EXTENDED
    if not down:
        flags |= KEYEVENTF_KEYUP
    inp = INPUT(type=1, u=_U(ki=KEYBDINPUT(0, scan, flags, 0, 0)))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def jitter(ms, percent):
    """Pseudo-random delay around ms: spread is ±percent%."""
    if not percent:
        return ms
    spread = ms * percent / 100.0
    return max(0, ms + random.uniform(-spread, spread))


def run_sequence(keys, interval_ms, hold_ms, stop=None, jitter_pct=0, hold_jitter_pct=0):
    """keys: 'q w a s d', 'ctrl+c', 'wait:500'. Each token is tapped in order.
    jitter_pct/hold_jitter_pct add pseudo-random spread to each delay/hold time."""
    for token in keys.split():
        if stop and stop.is_set():
            return
        if token.lower().startswith("wait:"):
            time.sleep(int(token[5:]) / 1000)
            continue
        combo = token.split("+") if len(token) > 1 and "+" in token.strip("+") else [token]
        for k in combo:
            send_key(k, True)
        time.sleep(jitter(hold_ms, hold_jitter_pct) / 1000)
        for k in reversed(combo):
            send_key(k, False)
        time.sleep(jitter(interval_ms, jitter_pct) / 1000)


def validate_keys(keys):
    for token in keys.split():
        if token.lower().startswith("wait:"):
            int(token[5:])
            continue
        combo = token.split("+") if len(token) > 1 and "+" in token.strip("+") else [token]
        for k in combo:
            key_to_vk(k)


# --------------------------------------------------------------------------
# Gestures
# --------------------------------------------------------------------------
FINGER_NAMES = ["большой", "указ.", "средний", "безым.", "мизинец"]


def fp(*s):  # finger pattern helper: "01100"
    return {"type": "fingers", "pattern": [c == "1" for c in s[0]]}


BUILTIN = [
    ("Кулак", "00000"), ("Открытая ладонь", "11111"),
    ("Указательный", "01000"), ("Мир (V)", "01100"), ("Три пальца", "01110"),
    ("Четыре пальца", "01111"), ("Лайк", "10000"), ("Рок", "01001"),
    ("Телефон (шака)", "10001"), ("Пистолет", "11000"), ("Три (большой+2)", "11100"),
    ("Мизинец", "00001"), ("Средний", "00100"),
]
BUILTIN_TWO = [
    ("2 кулака", "00000", "00000"), ("2 ладони", "11111", "11111"),
    ("2 указательных", "01000", "01000"), ("2 мира", "01100", "01100"),
    ("2 лайка", "10000", "10000"), ("Ладонь + кулак", "11111", "00000"),
    ("Мир + кулак", "01100", "00000"), ("Указ. + ладонь", "01000", "11111"),
    ("2 рока", "01001", "01001"), ("2 по три пальца", "01110", "01110"),
]


def default_gestures():
    base = {"keys": "", "interval": 100, "hold": 50, "repeat": False, "builtin": True}
    out = [dict(base, name=n, hands=[fp(p)]) for n, p in BUILTIN]
    out += [dict(base, name=n, hands=[fp(a), fp(b)]) for n, a, b in BUILTIN_TWO]
    return out


def load_gestures():
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default_gestures()


def save_gestures(g):
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(g, f, ensure_ascii=False, indent=1)


def finger_state(pts):
    """pts: 21x2 pixel array -> tuple of 5 bools (extended)."""
    w = pts[0]
    d = lambda i: np.linalg.norm(pts[i] - w)
    res = [np.linalg.norm(pts[4] - pts[17]) > np.linalg.norm(pts[3] - pts[17]) * 1.05
           and np.linalg.norm(pts[4] - pts[5]) > np.linalg.norm(pts[3] - pts[5]) * 0.9]
    for tip, pip in ((8, 6), (12, 10), (16, 14), (20, 18)):
        res.append(d(tip) > d(pip) * 1.05)
    return tuple(bool(x) for x in res)


def normalize(pts):
    p = pts - pts[0]
    s = np.linalg.norm(pts[9] - pts[0]) or 1.0
    return p / s


def spec_score(spec, hand, thr):
    """Returns score (lower is better) or None when not matching."""
    if spec["type"] == "fingers":
        return 0.25 if tuple(spec["pattern"]) == hand["fingers"] else None
    d = float(np.mean(np.linalg.norm(hand["norm"] - np.array(spec["pts"]), axis=1)))
    return d if d < thr else None


def match_gesture(g, hands, thr):
    n = len(g["hands"])
    if len(hands) < n:
        return None
    best = None
    for combo in itertools.permutations(hands, n):
        scores = [spec_score(s, h, thr) for s, h in zip(g["hands"], combo)]
        if None in scores:
            continue
        sc = sum(scores) / n
        best = sc if best is None else min(best, sc)
    return best


def recognize(gestures, hands, thr):
    best, key = None, None
    for g in gestures:
        sc = match_gesture(g, hands, thr)
        if sc is None:
            continue
        k = (-len(g["hands"]), sc)
        if key is None or k < key:
            best, key = g, k
    return best


# --------------------------------------------------------------------------
# Camera / recognition worker
# --------------------------------------------------------------------------
def list_cameras(max_idx=8):
    found = []
    for i in range(max_idx):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        if cap.isOpened():
            found.append(i)
        cap.release()
    return found


class Engine:
    def __init__(self, app):
        self.app = app
        self.thread = None
        self.stop = threading.Event()
        self.frame = None
        self.lock = threading.Lock()
        self.last_norms = []      # normalized templates of the latest frame
        self.current = None       # name of recognised gesture
        self.fps = 0.0
        self.error = None

    def start(self, cam):
        self.shutdown()
        self.stop = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self.loop, args=(cam, self.stop), daemon=True)
        self.thread.start()

    def shutdown(self):
        if self.thread:
            self.stop.set()
            self.thread.join(timeout=3)
            self.thread = None

    def loop(self, cam, stop):
        cap = cv2.VideoCapture(cam, cv2.CAP_DSHOW)
        if not cap.isOpened():
            self.error = f"Не удалось открыть камеру {cam}"
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 960)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 540)
        mph = mp.solutions.hands
        draw, styles = mp.solutions.drawing_utils, mp.solutions.drawing_styles
        hands_model = mph.Hands(max_num_hands=2, min_detection_confidence=0.6,
                                min_tracking_confidence=0.5)
        cand, cand_n, fired, idle, last_t = None, 0, None, 0, time.time()
        while not stop.is_set():
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.01)
                continue
            frame = cv2.flip(frame, 1)
            h, w = frame.shape[:2]
            res = hands_model.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            hands = []
            if res.multi_hand_landmarks:
                for lm in res.multi_hand_landmarks:
                    pts = np.array([[p.x * w, p.y * h] for p in lm.landmark])
                    hands.append({"fingers": finger_state(pts), "norm": normalize(pts)})
                    if self.app.show_points.get():
                        draw.draw_landmarks(frame, lm, mph.HAND_CONNECTIONS,
                                            styles.get_default_hand_landmarks_style(),
                                            styles.get_default_hand_connections_style())
            self.last_norms = [x["norm"] for x in hands]

            g = recognize(self.app.gestures, hands, self.app.thr.get()) if hands else None
            name = g["name"] if g else None
            if name == cand and name is not None:
                cand_n += 1
            else:
                cand, cand_n = name, 1 if name else 0
            if name is None:
                idle += 1
                if idle > 6:
                    fired = None
            else:
                idle = 0
            self.current = name if cand_n >= self.app.hold_frames.get() else None
            if self.current and (fired != self.current or
                                 (g.get("repeat") and not self.app.is_busy(g))):
                fired = self.current
                self.app.trigger(g)

            now = time.time()
            self.fps = 0.9 * self.fps + 0.1 / max(now - last_t, 1e-3)
            last_t = now
            label = (self.current or (f"... {name}" if name else "нет жеста"))
            cv2.putText(frame, label, (10, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                        (0, 255, 0) if self.current else (0, 200, 255), 2)
            with self.lock:
                self.frame = frame
        cap.release()
        hands_model.close()


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Gesture Invoker")
        self.geometry("1280x720")
        self.gestures = load_gestures()
        self.busy = {}
        self.show_points = tk.BooleanVar(value=True)
        self.emulate = tk.BooleanVar(value=False)
        self.thr = tk.DoubleVar(value=0.30)
        self.hold_frames = tk.IntVar(value=5)
        self.engine = Engine(self)
        self.cams = []
        self.build()
        self.refresh_cameras()
        self.refresh_tree()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(30, self.update_view)

    # ---- layout
    def build(self):
        left = ttk.Frame(self)
        left.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.video = ttk.Label(left, background="black")
        self.video.pack(fill="both", expand=True)
        self.status = ttk.Label(left, text="Камера остановлена")
        self.status.pack(fill="x")

        right = ttk.Frame(self, width=480)
        right.pack(side="right", fill="y", padx=6, pady=6)

        cam = ttk.LabelFrame(right, text="Камера")
        cam.pack(fill="x")
        self.cam_box = ttk.Combobox(cam, state="readonly", width=24)
        self.cam_box.pack(side="left", padx=4, pady=4)
        ttk.Button(cam, text="Обновить", command=self.refresh_cameras).pack(side="left")
        ttk.Button(cam, text="Старт", command=self.start_cam).pack(side="left", padx=2)
        ttk.Button(cam, text="Стоп", command=self.engine.shutdown).pack(side="left")

        opt = ttk.LabelFrame(right, text="Настройки")
        opt.pack(fill="x", pady=6)
        ttk.Checkbutton(opt, text="Показывать точки захвата", variable=self.show_points).grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(opt, text="ЭМУЛЯЦИЯ КЛАВИШ ВКЛЮЧЕНА", variable=self.emulate).grid(
            row=1, column=0, columnspan=2, sticky="w")
        ttk.Label(opt, text="Допуск шаблона (меньше = строже)").grid(row=2, column=0, sticky="w")
        ttk.Scale(opt, from_=0.1, to=0.8, variable=self.thr, length=140).grid(row=2, column=1)
        ttk.Label(opt, text="Удержание жеста (кадров)").grid(row=3, column=0, sticky="w")
        ttk.Spinbox(opt, from_=1, to=60, width=5, textvariable=self.hold_frames).grid(row=3, column=1)

        box = ttk.LabelFrame(right, text="Жесты и бинды")
        box.pack(fill="both", expand=True)
        cols = ("hands", "keys", "int")
        self.tree = ttk.Treeview(box, columns=cols, show="tree headings", height=18)
        self.tree.heading("#0", text="Жест")
        self.tree.heading("hands", text="Рук")
        self.tree.heading("keys", text="Клавиши")
        self.tree.heading("int", text="Интервал")
        self.tree.column("#0", width=150)
        self.tree.column("hands", width=40, anchor="center")
        self.tree.column("keys", width=130)
        self.tree.column("int", width=60, anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.edit_binding())
        bar = ttk.Frame(box)
        bar.pack(fill="x")
        ttk.Button(bar, text="Бинд...", command=self.edit_binding).pack(side="left")
        ttk.Button(bar, text="Новый жест...", command=self.new_gesture).pack(side="left", padx=2)
        ttk.Button(bar, text="Тест", command=self.test_binding).pack(side="left")
        ttk.Button(bar, text="Удалить", command=self.delete_gesture).pack(side="left", padx=2)
        ttk.Label(right, foreground="gray", justify="left", wraplength=470,
                  text="Клавиши: через пробел, по порядку: q w a s d. Комбинации: ctrl+c. "
                       "Пауза: wait:500 (мс). Спец: space enter tab esc up down left right f1..f12.").pack(
            fill="x", pady=4)

    # ---- camera
    def refresh_cameras(self):
        self.cams = list_cameras()
        self.cam_box["values"] = [f"Камера {i}" for i in self.cams] or ["(не найдено)"]
        self.cam_box.current(0)

    def start_cam(self):
        if not self.cams:
            messagebox.showwarning("Камера", "Камеры не найдены")
            return
        self.engine.start(self.cams[self.cam_box.current()])

    def update_view(self):
        e = self.engine
        with e.lock:
            frame = None if e.frame is None else e.frame.copy()
        if e.error:
            self.status.config(text=e.error)
        elif frame is not None:
            w, h = max(self.video.winfo_width(), 200), max(self.video.winfo_height(), 200)
            fh, fw = frame.shape[:2]
            s = min(w / fw, h / fh)
            frame = cv2.resize(frame, (int(fw * s), int(fh * s)))
            img = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
            self.video.configure(image=img)
            self.video.image = img
            self.status.config(text=f"FPS {e.fps:.0f} | Жест: {e.current or '-'}")
        self.after(30, self.update_view)

    # ---- triggering
    def is_busy(self, g):
        return self.busy.get(g["name"], False)

    def trigger(self, g):
        if not self.emulate.get() or not g.get("keys") or self.is_busy(g):
            return
        self.run_async(g)

    def run_async(self, g):
        def work():
            self.busy[g["name"]] = True
            try:
                run_sequence(g["keys"], g.get("interval", 100), g.get("hold", 50),
                             jitter_pct=g.get("jitter", 0),
                             hold_jitter_pct=g.get("hold_jitter", 0))
            except Exception as ex:
                print("send error:", ex)
            finally:
                self.busy[g["name"]] = False
        threading.Thread(target=work, daemon=True).start()

    def test_binding(self):
        g = self.selected()
        if g and g.get("keys"):
            self.after(2000, lambda: self.run_async(g))
            messagebox.showinfo("Тест", "Через 2 секунды клавиши будут нажаты — переключитесь в нужное окно.")

    # ---- gesture list
    def refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i, g in enumerate(self.gestures):
            self.tree.insert("", "end", iid=str(i), text=g["name"] + ("" if g.get("builtin") else " *"),
                             values=(len(g["hands"]),
                                     g.get("keys", "") + (" ~" if g.get("jitter") else ""),
                                     g.get("interval", 100)))

    def selected(self):
        s = self.tree.selection()
        return self.gestures[int(s[0])] if s else None

    def edit_binding(self):
        g = self.selected()
        if g:
            BindDialog(self, g)

    def delete_gesture(self):
        g = self.selected()
        if g and messagebox.askyesno("Удалить", f"Удалить жест «{g['name']}»?"):
            self.gestures.remove(g)
            save_gestures(self.gestures)
            self.refresh_tree()

    def new_gesture(self):
        if self.engine.thread is None:
            messagebox.showinfo("Жест", "Сначала запустите камеру.")
            return
        RecordDialog(self)

    def on_close(self):
        self.engine.shutdown()
        self.destroy()


class BindDialog(tk.Toplevel):
    def __init__(self, app, g):
        super().__init__(app)
        self.app, self.g = app, g
        self.title(f"Бинд: {g['name']}")
        self.resizable(False, False)
        self.grab_set()
        self.keys = tk.StringVar(value=g.get("keys", ""))
        self.interval = tk.IntVar(value=g.get("interval", 100))
        self.hold = tk.IntVar(value=g.get("hold", 50))
        self.repeat = tk.BooleanVar(value=g.get("repeat", False))
        f = ttk.Frame(self, padding=10)
        f.pack()
        ttk.Label(f, text="Последовательность клавиш (q w a s d):").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.keys, width=40).grid(row=1, column=0, columnspan=2, pady=2)
        ttk.Label(f, text="Интервал между нажатиями, мс:").grid(row=2, column=0, sticky="w")
        ttk.Spinbox(f, from_=0, to=60000, width=8, textvariable=self.interval).grid(row=2, column=1)
        ttk.Label(f, text="Удержание каждой клавиши, мс:").grid(row=3, column=0, sticky="w")
        ttk.Spinbox(f, from_=1, to=60000, width=8, textvariable=self.hold).grid(row=3, column=1)
        ttk.Checkbutton(f, text="Повторять, пока жест удерживается", variable=self.repeat).grid(
            row=4, column=0, columnspan=2, sticky="w")
        self.rand_on = tk.BooleanVar(value=g.get("rand_on", False))
        self.jitter = tk.IntVar(value=g.get("jitter", 15))
        self.hold_jitter = tk.IntVar(value=g.get("hold_jitter", 0))
        ttk.Checkbutton(f, text="Псевдослучайный разброс интервалов", variable=self.rand_on).grid(
            row=5, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="Разброс интервала, ±%:").grid(row=6, column=0, sticky="w")
        ttk.Spinbox(f, from_=0, to=90, width=8, textvariable=self.jitter).grid(row=6, column=1)
        ttk.Label(f, text="Разброс удержания, ±%:").grid(row=7, column=0, sticky="w")
        ttk.Spinbox(f, from_=0, to=90, width=8, textvariable=self.hold_jitter).grid(row=7, column=1)
        ttk.Button(f, text="Сохранить", command=self.ok).grid(row=8, column=0, columnspan=2, pady=6)

    def ok(self):
        try:
            validate_keys(self.keys.get())
            self.g.update(keys=self.keys.get().strip(), interval=int(self.interval.get()),
                          hold=int(self.hold.get()), repeat=self.repeat.get(),
                          rand_on=self.rand_on.get(),
                          jitter=int(self.jitter.get()) if self.rand_on.get() else 0,
                          hold_jitter=int(self.hold_jitter.get()) if self.rand_on.get() else 0)
        except Exception as ex:
            messagebox.showerror("Ошибка", str(ex), parent=self)
            return
        save_gestures(self.app.gestures)
        self.app.refresh_tree()
        self.destroy()


class RecordDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Новый жест")
        self.grab_set()
        self.name = tk.StringVar()
        self.n = tk.IntVar(value=1)
        self.captured = None
        f = ttk.Frame(self, padding=10)
        f.pack()
        ttk.Label(f, text="Название:").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.name, width=28).grid(row=0, column=1)
        ttk.Radiobutton(f, text="Одна рука", variable=self.n, value=1).grid(row=1, column=0)
        ttk.Radiobutton(f, text="Две руки", variable=self.n, value=2).grid(row=1, column=1)
        self.info = ttk.Label(f, text="Покажите жест и нажмите «Записать» (запись через 3 с).",
                              wraplength=300)
        self.info.grid(row=2, column=0, columnspan=2, pady=6)
        ttk.Button(f, text="Записать", command=self.countdown).grid(row=3, column=0)
        ttk.Button(f, text="Сохранить", command=self.save).grid(row=3, column=1)

    def countdown(self, t=3):
        if t > 0:
            self.info.config(text=f"Замрите... {t}")
            self.after(1000, lambda: self.countdown(t - 1))
            return
        # average several frames for a stable template
        self.samples = []
        self.collect(10)

    def collect(self, left):
        norms = list(self.app.engine.last_norms)
        if len(norms) == self.n.get():
            self.samples.append(norms)
        if left > 1:
            self.after(60, lambda: self.collect(left - 1))
            return
        if len(self.samples) < 5:
            self.info.config(text=f"Не вижу {self.n.get()} рук(и). Повторите.")
            return
        cnt = self.n.get()
        if cnt == 1:
            tmpl = [np.mean([s[0] for s in self.samples], axis=0)]
        else:
            # keep hand order consistent with the first sample (by x of wrist is lost -> use closeness)
            ref = self.samples[0]
            acc = [[ref[0]], [ref[1]]]
            for s in self.samples[1:]:
                straight = np.mean(np.linalg.norm(s[0] - ref[0], axis=1)) + \
                    np.mean(np.linalg.norm(s[1] - ref[1], axis=1))
                swapped = np.mean(np.linalg.norm(s[1] - ref[0], axis=1)) + \
                    np.mean(np.linalg.norm(s[0] - ref[1], axis=1))
                a, b = (s[0], s[1]) if straight <= swapped else (s[1], s[0])
                acc[0].append(a)
                acc[1].append(b)
            tmpl = [np.mean(x, axis=0) for x in acc]
        self.captured = [{"type": "template", "pts": t.tolist()} for t in tmpl]
        self.info.config(text="Записано ✓. Нажмите «Сохранить».")

    def save(self):
        name = self.name.get().strip()
        if not name or not self.captured:
            messagebox.showwarning("Жест", "Введите название и запишите жест.", parent=self)
            return
        if any(g["name"] == name for g in self.app.gestures):
            messagebox.showwarning("Жест", "Такое название уже есть.", parent=self)
            return
        g = {"name": name, "hands": self.captured, "keys": "", "interval": 100,
             "hold": 50, "repeat": False, "builtin": False}
        self.app.gestures.insert(0, g)
        save_gestures(self.app.gestures)
        self.app.refresh_tree()
        self.destroy()
        BindDialog(self.app, g)


if __name__ == "__main__":
    App().mainloop()
