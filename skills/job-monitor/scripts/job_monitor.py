r"""
job_monitor.py - a small desktop window for long-running jobs.

The window can be dragged anywhere on the desktop and shows:
  - a countdown to the estimated finish time, and how far along the job is
  - how hard the hardware is working: GPU, GPU memory, processor, memory
  - "Project complete" and a "Close window" button when the job ends

It opens straight away for a job expected to take more than 10 minutes, and otherwise
only once a job has been running for 10 minutes. Closing the window never stops the job.

Start a job and watch it (everything after -- is the command to run):
    python job_monitor.py --title "Model training" --minutes 90 --log train.log -- python train.py
Watch a job that is already running:
    python job_monitor.py --title "Video export" --minutes 45 --pid 12345 --log export.log
See what the window looks like (a one-minute pretend job):
    python job_monitor.py --demo

    --minutes     your estimate for the whole job; leave it out if you have none
    --log         the file the job's output goes to. Lines containing "[25/300]" are used
                  for a live estimate, which replaces --minutes once the job has made progress.
    --pattern     how progress looks in the log, if not "[done/total]": a regular expression
                  with two groups, e.g. "step (\d+) of (\d+)"
    --show-after  seconds a job must run before the window opens (default 600)
    --show-now    open the window immediately, whatever the estimate
    --theme       dark, light or auto (default: follow the system)

The command returns when the job ends and prints how it went. The job keeps running even
if this command or the window is closed. Needs Python 3.8+ with Tk; nothing else.
GPU readings need an NVIDIA card (nvidia-smi); without one they show "n/a".
"""

import argparse
import ctypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback

__version__ = "0.1.0"

WINDOWS = os.name == "nt"
MAC = sys.platform == "darwin"
SHOW_AFTER = 600                      # seconds a job runs before the window opens by itself
PROGRESS_RE = r"\[\s*(\d+)\s*/\s*(\d+)\s*\]"
NO_WINDOW, NEW_GROUP, BREAKAWAY = 0x08000000, 0x00000200, 0x01000000
DEMO = "import time\nfor i in range(1, 21):\n    print(f'  [{i}/20] pretend step', flush=True)\n    time.sleep(3)\n"

DARK = {"bg": "#201f1d", "text": "#f3f1ea", "muted": "#a5a299", "track": "#3b3a36",
        "accent": "#5b9bd5", "ok": "#84c871", "err": "#e8806c", "button": "#2f6fb5", "on_button": "#ffffff"}
LIGHT = {"bg": "#faf9f5", "text": "#1f1e1c", "muted": "#6a6862", "track": "#e2dfd6",
         "accent": "#2f6fb5", "ok": "#3b7d2a", "err": "#b3402e", "button": "#2f6fb5", "on_button": "#ffffff"}

if WINDOWS:
    from ctypes import wintypes
    K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    K32.OpenProcess.restype = wintypes.HANDLE
    K32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    K32.WaitForSingleObject.restype = wintypes.DWORD
    K32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    K32.GetExitCodeProcess.restype = wintypes.BOOL
    K32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

    class FILETIME(ctypes.Structure):
        _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD)] + \
                   [(n, ctypes.c_uint64) for n in ("total", "avail", "total_page", "avail_page",
                                                   "total_virtual", "avail_virtual", "avail_extended")]
else:
    try:
        import psutil                 # optional: processor and memory readings on macOS
    except Exception:
        psutil = None


# ----------------------------------------------------------------------------- the job

def exit_note(pid):
    """Where the command that started a job leaves its exit code for the window to read."""
    return os.path.join(tempfile.gettempdir(), f"job_monitor_{pid}.exit")


class Job:
    """A running process, watched without touching it."""

    def __init__(self, pid):
        self.pid = pid
        # Windows: a handle that may only be waited on and asked for the exit code
        self.handle = K32.OpenProcess(0x00100000 | 0x1000, False, pid) if WINDOWS else None

    def running(self):
        if WINDOWS:
            return bool(self.handle) and K32.WaitForSingleObject(self.handle, 0) == 0x102
        try:
            os.kill(self.pid, 0)                              # signal 0 only asks whether it exists
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        try:                                                  # ended but not yet collected by its parent
            with open(f"/proc/{self.pid}/stat") as f:
                return f.read().rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, IndexError):
            return True

    def exit_code(self):
        if WINDOWS and self.handle:
            code = wintypes.DWORD()
            if K32.GetExitCodeProcess(self.handle, ctypes.byref(code)):
                return code.value
        try:
            with open(exit_note(self.pid)) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None


def detached(cmd, **kw):
    """Start a process that keeps running whatever happens to this one."""
    if not WINDOWS:
        return subprocess.Popen(cmd, start_new_session=True, close_fds=True, stdin=subprocess.DEVNULL, **kw)
    err = None
    for flags in (NO_WINDOW | NEW_GROUP | BREAKAWAY, NO_WINDOW | NEW_GROUP):
        try:
            return subprocess.Popen(cmd, creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL, **kw)
        except OSError as e:
            err = e
    raise err


def log_progress(path, pattern):
    """(done, total) from the last progress line of the log, or None."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8192))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    last = None
    for last in pattern.finditer(tail):
        pass
    try:
        if last and int(last.group(2)) > 0:
            return int(last.group(1)), int(last.group(2))
    except (IndexError, ValueError):
        pass
    return None


def tail_lines(path, n=3):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4096))
            lines = [l.strip() for l in f.read().decode("utf-8", "replace").splitlines() if l.strip()]
        return lines[-n:]
    except OSError:
        return []


# ----------------------------------------------------------------------------- hardware

def cpu_counters():
    """(idle, total) processor time so far, or None where it cannot be read."""
    if WINDOWS:
        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        K32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        v = lambda t: (t.hi << 32) | t.lo
        return v(idle), v(kernel) + v(user)                  # kernel time includes idle time
    try:
        with open("/proc/stat") as f:                        # Linux
            n = [int(x) for x in f.readline().split()[1:9]]
        return n[3] + n[4], sum(n)
    except (OSError, ValueError, IndexError):
        return None


def memory_load():
    """Share of memory in use, in percent, or None."""
    if WINDOWS:
        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(m)
        return float(m.dwMemoryLoad) if K32.GlobalMemoryStatusEx(ctypes.byref(m)) else None
    if psutil:
        return float(psutil.virtual_memory().percent)
    try:
        with open("/proc/meminfo") as f:                     # Linux
            kb = {l.split(":")[0]: int(l.split()[1]) for l in f if ":" in l}
        return 100.0 * (1 - kb["MemAvailable"] / kb["MemTotal"])
    except (OSError, KeyError, ValueError, IndexError):
        return None


def gpu_reading():
    """Load, memory, temperature and power of the first NVIDIA GPU; empty if there is none."""
    def num(x):
        try:
            return float(x)
        except ValueError:
            return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6,
            creationflags=NO_WINDOW if WINDOWS else 0).stdout.strip().splitlines()[0]
        load, used, total, temp, watts = [num(x.strip()) for x in out.split(",")]
    except Exception:
        return {}
    return {"gpu": load, "vram": 100 * used / total if used is not None and total else None,
            "temp": temp, "watts": watts}


class Hardware(threading.Thread):
    """Reads the hardware load (and the job's log) every two seconds, off the window's thread."""

    def __init__(self, log, pattern):
        super().__init__(daemon=True)
        self.log, self.pattern = log, re.compile(pattern)
        self.now = {}
        self.has_gpu = bool(shutil.which("nvidia-smi"))
        self._cpu = cpu_counters()
        if not WINDOWS and psutil:
            psutil.cpu_percent(None)                         # start its own counter

    def cpu_load(self):
        if not WINDOWS and psutil:
            return float(psutil.cpu_percent(None))
        new = cpu_counters()
        old, self._cpu = self._cpu, new
        if not new or not old or new[1] <= old[1]:
            return None
        return 100.0 * (1 - (new[0] - old[0]) / (new[1] - old[1]))

    def run(self):
        while True:
            r = gpu_reading() if self.has_gpu else {}
            r["cpu"] = self.cpu_load()
            r["ram"] = memory_load()
            r["progress"] = log_progress(self.log, self.pattern) if self.log else None
            self.now = r
            time.sleep(2)


# ----------------------------------------------------------------------------- estimate and wording

def estimate(now, started, minutes, progress):
    """-> (seconds left or None, share done or None, short note)"""
    elapsed = max(0.0, now - started)
    note = f"{progress[0]:,} of {progress[1]:,}" if progress else ""
    share = min(1.0, progress[0] / progress[1]) if progress else None
    if progress and progress[0] >= 3 and elapsed >= 30:                   # enough progress: go by the real pace
        return (progress[1] - progress[0]) * elapsed / progress[0], share, note
    if minutes:
        if share is None:
            share, note = min(0.99, elapsed / (minutes * 60)), "estimated from the clock"
        return started + minutes * 60 - now, share, note
    return None, share, note


def fmt_left(sec):
    sec = int(max(0, sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h} h {m:02d} min" if h else f"{m}:{s:02d}"


def fmt_dur(sec):
    m = int(round(sec / 60))
    if m >= 60:
        return f"{m // 60} h {m % 60:02d} min"
    return f"{m} min" if m >= 1 else f"{int(sec)} s"


def clock(t):
    return time.strftime("%I:%M %p", time.localtime(t)).lstrip("0")


def dark_theme(choice):
    if choice in ("dark", "light"):
        return choice == "dark"
    try:
        if WINDOWS:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
        if MAC:
            out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                 capture_output=True, text=True, timeout=3).stdout
            return "dark" in out.lower()
    except Exception:
        pass
    return True


# ----------------------------------------------------------------------------- the window

def run_window(a):
    job = Job(a.pid)
    started = a.started or time.time()
    while time.time() < a.show_at:                    # a short job never gets a window
        if not job.running():
            return
        time.sleep(1)

    if WINDOWS:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)      # sharp text on high-resolution screens
        except Exception:
            pass
    import tkinter as tk
    import tkinter.font as tkfont

    hw = Hardware(a.log, a.pattern)
    hw.start()

    root = tk.Tk()
    root.withdraw()
    scale = 1.0 if MAC else root.winfo_fpixels("1i") / 96.0
    px = lambda n: int(round(n * scale))
    pt = lambda n: int(round(n * 1.3)) if MAC else n            # macOS draws a point smaller
    C = DARK if dark_theme(a.theme) else LIGHT
    F = "Segoe UI" if WINDOWS else tkfont.nametofont("TkDefaultFont").actual("family")
    W = px(330)
    root.title("Job monitor")
    root.configure(bg=C["bg"])
    root.resizable(False, False)

    def frame_handle():
        return ctypes.windll.user32.GetParent(root.winfo_id())

    def label(parent, text="", size=10, color="text", weight="normal", **kw):
        return tk.Label(parent, text=text, bg=C["bg"], fg=C[color], font=(F, pt(size), weight),
                        anchor="w", justify="left", bd=0, **kw)

    def bar(parent, width, height=8):
        c = tk.Canvas(parent, width=width, height=px(height), bg=C["track"], highlightthickness=0, bd=0)
        item = c.create_rectangle(0, 0, 0, px(height), fill=C["accent"], width=0)

        def set_share(share, color=None):
            share = max(0.0, min(1.0, share or 0.0))
            c.coords(item, 0, 0, int(round(width * share)), px(height))
            if color:
                c.itemconfigure(item, fill=C[color])
        c.set_share = set_share
        return c

    body = tk.Frame(root, bg=C["bg"], padx=px(18), pady=px(14))
    body.pack()
    label(body, a.title, 11, weight="bold", wraplength=W).pack(fill="x")
    big = label(body, "", 28)
    big.pack(fill="x", pady=(px(2), 0))
    small = label(body, "", 10, "muted", wraplength=W)
    small.pack(fill="x")
    pbar = bar(body, W)
    pbar.pack(pady=(px(12), px(4)))
    ptext = label(body, "", 9, "muted")
    ptext.pack(fill="x")

    tk.Frame(body, bg=C["track"], height=max(1, px(1))).pack(fill="x", pady=(px(12), px(10)))
    label(body, "Hardware in use", 9, "muted").pack(fill="x", pady=(0, px(4)))
    grid = tk.Frame(body, bg=C["bg"])
    grid.pack(fill="x")
    grid.columnconfigure(1, weight=1)
    rows = {}
    for i, (key, name) in enumerate((("gpu", "GPU"), ("vram", "GPU memory"), ("cpu", "Processor"), ("ram", "Memory"))):
        label(grid, name, 10, width=11).grid(row=i, column=0, sticky="w", pady=px(3), padx=(0, px(10)))
        b = bar(grid, W - px(160), 8)
        b.grid(row=i, column=1, sticky="w")
        v = label(grid, "", 10, width=5)
        v.configure(anchor="e")
        v.grid(row=i, column=2, sticky="e")
        rows[key] = (b, v)
    detail = label(body, "", 9, "muted")
    detail.pack(fill="x", pady=(px(4), 0))

    tk.Frame(body, bg=C["track"], height=max(1, px(1))).pack(fill="x", pady=(px(12), px(8)))
    on_top = [True]
    top_box = label(body, "☑  Keep on top of other windows", 10, "muted", cursor="hand2")
    top_box.pack(fill="x")

    def toggle_on_top(_event=None):
        on_top[0] = not on_top[0]
        root.attributes("-topmost", on_top[0])
        top_box.config(text=("☑" if on_top[0] else "☐") + "  Keep on top of other windows")
    top_box.bind("<Button-1>", toggle_on_top)
    note = label(body, "Closing this window does not stop the job.", 9, "muted")
    note.pack(fill="x", pady=(px(2), 0))
    close = tk.Button(body, text="Close window", command=root.destroy, font=(F, pt(10)), relief="flat", bd=0,
                      bg=C["button"], fg=C["on_button"], activebackground=C["accent"],
                      activeforeground=C["on_button"], padx=px(16), pady=px(6), cursor="hand2")

    def snap(tag):                                    # a picture of this window, for testing
        if not (a.snapshot and WINDOWS):
            return
        try:
            from PIL import ImageGrab
            root.update()
            ImageGrab.grab(window=frame_handle()).save(f"{a.snapshot}_{tag}.png")
        except Exception:
            report_error()

    state = {"done": False, "gone": None}

    def finish(now, code):
        state["done"] = True
        ok = code in (0, None)
        big.config(text="Project complete" if code == 0 else "Job finished" if code is None else "Job stopped early",
                   fg=C["ok"] if ok else C["err"])
        small.config(text=f"{'Finished' if ok else 'Stopped'} at {clock(now)}, after {fmt_dur(now - started)}"
                          + ("" if ok else f" (exit code {code})"))
        if ok:
            pbar.set_share(1.0, "ok")
            ptext.config(text="100%")
        note.pack_forget()
        close.pack(anchor="w", pady=(px(10), 0))
        root.deiconify()
        root.lift()
        if WINDOWS:
            try:
                ctypes.windll.user32.FlashWindow(frame_handle(), True)
            except Exception:
                pass
        try:
            os.remove(exit_note(a.pid))
        except OSError:
            pass
        if a.snapshot:
            root.after(1500, lambda: snap("complete"))
        if a.auto_close:
            root.after(int(a.auto_close * 1000), root.destroy)

    def tick():
        now = time.time()
        r = hw.now
        if not state["done"]:
            if job.running():
                left, share, how = estimate(now, started, a.minutes, r.get("progress"))
                if left is None:
                    big.config(text=fmt_left(now - started))
                    small.config(text="running so far (no estimate available)")
                elif left > 0:
                    big.config(text=fmt_left(left))
                    small.config(text=f"left (estimate), done about {clock(now + left)}")
                else:
                    big.config(text="Any minute now")
                    small.config(text=f"{fmt_dur(-left)} past the estimate" if -left >= 60 else "finishing up")
                pbar.set_share(share)
                ptext.config(text=(f"{share:.0%}" if share is not None else "progress not known")
                                  + (f"  ·  {how}" if how else ""))
            else:
                state["gone"] = state["gone"] or now
                code = job.exit_code()
                if code is not None or WINDOWS or now - state["gone"] >= 3:     # elsewhere the starter reports it
                    finish(state["gone"], code)
        for key, (b, v) in rows.items():
            value = r.get(key)
            b.set_share((value or 0) / 100)
            v.config(text=f"{value:.0f}%" if value is not None else "n/a")
        extra = [f"GPU {r['temp']:.0f} °C" if r.get("temp") is not None else "",
                 f"{r['watts']:.0f} W" if r.get("watts") is not None else ""]
        detail.config(text="  ·  ".join(x for x in extra if x))
        root.after(500, tick)

    tick()
    root.update_idletasks()
    x = root.winfo_screenwidth() - root.winfo_reqwidth() - px(28)
    y = root.winfo_screenheight() - root.winfo_reqheight() - px(130)
    root.geometry(f"+{max(0, x)}+{max(0, y)}")
    if WINDOWS and C is DARK:
        try:
            on = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(frame_handle(), 20, ctypes.byref(on), ctypes.sizeof(on))
        except Exception:
            pass
    root.attributes("-topmost", True)
    root.deiconify()
    if a.snapshot:
        root.after(7000, lambda: snap("running"))
    root.mainloop()


def error_log():
    return os.path.join(tempfile.gettempdir(), "job_monitor_error.log")


def report_error():
    """The window has no console, so its problems go to a file."""
    with open(error_log(), "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S\n") + traceback.format_exc() + "\n")


def open_window(a, pid, started, show_at):
    exe = sys.executable
    if WINDOWS:                                       # pythonw: no console window behind the monitor
        quiet = os.path.join(os.path.dirname(exe), "pythonw.exe")
        exe = quiet if os.path.exists(quiet) else exe
    cmd = [exe, os.path.abspath(__file__), "--window", "--pid", str(pid), "--title", a.title,
           "--started", repr(started), "--show-at", repr(show_at), "--pattern", a.pattern, "--theme", a.theme]
    if a.minutes:
        cmd += ["--minutes", str(a.minutes)]
    if a.log:
        cmd += ["--log", os.path.abspath(a.log)]
    if a.snapshot:
        cmd += ["--snapshot", a.snapshot]
    if a.auto_close:
        cmd += ["--auto-close", str(a.auto_close)]
    return detached(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ----------------------------------------------------------------------------- the command

def main():
    argv, command = sys.argv[1:], []
    if "--" in argv:
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description="Desktop window with a countdown and hardware load for a long job")
    ap.add_argument("--title", default="Background job")
    ap.add_argument("--minutes", type=float, default=0, help="estimated length of the whole job")
    ap.add_argument("--pid", type=int, default=0, help="watch this running process instead of starting one")
    ap.add_argument("--log", default="", help="file with the job's output")
    ap.add_argument("--started", type=float, default=0, help="when the watched job began (seconds since 1970)")
    ap.add_argument("--pattern", default=PROGRESS_RE, help="how progress looks in the log: (done) and (total)")
    ap.add_argument("--show-after", type=float, default=SHOW_AFTER, help="seconds before the window opens")
    ap.add_argument("--show-now", action="store_true", help="open the window immediately")
    ap.add_argument("--theme", default="auto", choices=["auto", "dark", "light"])
    ap.add_argument("--cwd", default=None, help="folder to run the command in")
    ap.add_argument("--demo", action="store_true", help="show the window with a one-minute pretend job")
    ap.add_argument("--version", action="version", version=f"job_monitor {__version__}")
    ap.add_argument("--window", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--show-at", type=float, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--snapshot", default="", help=argparse.SUPPRESS)
    ap.add_argument("--auto-close", type=float, default=0, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    try:
        re.compile(a.pattern)
    except re.error as e:
        ap.error(f"--pattern is not a valid regular expression: {e}")

    if a.window:
        try:
            run_window(a)
        except Exception:
            report_error()
        return 0

    if a.demo:
        command = [sys.executable, "-u", "-c", DEMO]
        a.title = "Demo job (takes one minute)" if a.title == "Background job" else a.title
        a.minutes, a.show_now = a.minutes or 1, True
        a.log = a.log or os.path.join(tempfile.gettempdir(), "job_monitor_demo.log")
        open(a.log, "w").close()

    if command:
        if not a.log:
            a.log = os.path.join(tempfile.gettempdir(), f"job_{int(time.time())}.log")
        out = open(a.log, "a", encoding="utf-8")
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        try:
            proc = detached(command, cwd=a.cwd, env=env, stdout=out, stderr=subprocess.STDOUT)
        except OSError as e:
            print(f"could not start the command: {e}")
            return 1
        pid, started = proc.pid, time.time()
        try:
            os.remove(exit_note(pid))                 # left by an earlier job that had the same number
        except OSError:
            pass
        running = lambda: proc.poll() is None
        exit_code = lambda: proc.returncode
    elif a.pid:
        job = Job(a.pid)
        if not job.running():
            print(f"no running process with id {a.pid}")
            return 1
        pid, started = a.pid, a.started or time.time()
        running, exit_code = job.running, job.exit_code
    else:
        ap.error("give a command after --, or --pid of a running job, or --demo")

    now_open = a.show_now or a.minutes * 60 > a.show_after
    show_at = started if now_open else started + a.show_after
    print(f"job started {clock(started)}: pid {pid}" + (f", output in {a.log}" if a.log else ""), flush=True)
    window = open_window(a, pid, started, show_at)
    print("monitor window: " + ("open now" if now_open else f"opens at {clock(show_at)} if the job is still running"),
          flush=True)

    checked = not now_open
    while running():
        if not checked and time.time() - started > 4:
            checked = True
            if window.poll() is not None:             # it should still be there: say why it is not
                print(f"the monitor window did not stay open; details, if any, in {error_log()}", flush=True)
        time.sleep(1)

    code = exit_code()
    if command and code is not None and not WINDOWS:  # Windows lets the window read the exit code itself
        try:
            with open(exit_note(pid), "w") as f:
                f.write(str(code))
        except OSError:
            pass
    print(f"job ended {clock(time.time())} after {fmt_dur(time.time() - started)}, exit code {code}")
    for line in tail_lines(a.log) if a.log else []:
        print("  " + line[:200])
    return 0 if code in (0, None) else 1


if __name__ == "__main__":
    sys.exit(main())
