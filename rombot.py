#!/usr/bin/env python3
"""
rombot.py - Telegram build monitor for custom Android ROM builds.

It runs in the background on your build VM. A tiny wrapper (`rb`) launches your
build and streams the output into a log file. rombot tails that log, parses the
ninja/soong progress lines, and reports to Telegram:

  * a live "Build In Progress" card (progress bar, %, actions X/Y, elapsed, ETA)
  * a success message when the build finishes cleanly
  * a failure message + the error log attached when it does not

Only dependency: `requests`  (pip install requests)

Usage:
    python3 rombot.py             # run the daemon
    python3 rombot.py --selftest  # parse a sample log and print the messages
"""

from __future__ import annotations

import html
import json
import os
import random
import re
import sys
import threading
import time
from collections import deque

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# --------------------------------------------------------------------------- #
# Defaults (config.json overrides these; missing keys fall back here)
# --------------------------------------------------------------------------- #
DEFAULTS = {
    "bot_token": "PUT_YOUR_BOT_TOKEN_HERE",
    "chat_id": "PUT_YOUR_CHAT_ID_HERE",

    # Where the `rb` wrapper writes the build output
    "log_file": os.path.join(BASE_DIR, "build.log"),
    # Where rombot remembers its read position + last result (survives restarts)
    "state_file": os.path.join(BASE_DIR, "state.json"),

    # Shown in the messages
    "rom_name": "My Custom ROM",
    "device": "fogos",
    "user_name": "maintainer",

    # "Configuration" block, like the reference screenshot
    "variant": "user",
    "build_type": "release",
    "release": "cp2a",

    # Parallel jobs shown in the config block. 0 = auto (os.cpu_count()).
    "jobs": 0,

    # Root of your ROM source tree (so rombot can find out/error.log + the zip)
    "build_root": os.path.join(os.path.expanduser("~"), "android"),
    "output_glob": "out/target/product/{device}/*.zip",
    "error_log": "out/error.log",

    # How often to refresh the live message (seconds)
    "update_interval": 60,
    "progress_bar_width": 12,
    "max_error_lines": 40,

    # Progress-accuracy tuning.
    #   min_total: ignore progress lines whose total is below this. Skips the
    #     tiny soong "bootstrap" step that reports [100% 1/1], which would
    #     otherwise park the bar at 100% before the real (ninja) compile starts.
    #   min_build_seconds: ignore a "#### build completed successfully (H:M:S)"
    #     line shorter than this many seconds. Sub-steps like `m installclean`
    #     print that same line in a few seconds; a real build runs far longer.
    "min_total": 100,
    "min_build_seconds": 60,
    # If a running build stops writing to the log for this many seconds
    # (e.g. you cancelled it with Ctrl-C), stop showing "in progress".
    "stale_seconds": 900,

    "quotes": [
        "Proofreading your code... with a very judgmental compiler. \U0001F9D0",
        "Teaching the linker some manners. \U0001F517",
        "Convincing the CPU to try a little harder. \U0001F525",
        "Bribing ninja with extra parallelism. \U0001F977",
        "Whispering sweet nothings to soong. \U0001F92B",
    ],
}

# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Ninja/soong progress. The counts are NOT always immediately followed by
# "]": some builds print an ETA inside the bracket, e.g.
#   [  3% 915/27036 40m3s remaining] //packages/apps/Updater/...
PROG_RE = re.compile(r"\[\s*(\d+)%\s+(\d+)/(\d+)")
# Ninja's own ETA, when present ("40m3s remaining", "1h2m3s remaining").
ETA_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?\s*remaining")
START_MARK = re.compile(r"^###ROMBOT_START\s+(\d+)\s*(.*)$")
END_MARK = re.compile(r"^###ROMBOT_END\s+(\d+)\s+(\d+)\s*$")
SUCCESS_RE = re.compile(r"####\s+build completed successfully")
FAIL_RE = re.compile(r"####\s+failed to build some targets")
NINJA_FAIL_RE = re.compile(r"ninja:\s+build stopped")
ERROR_LINE_RE = re.compile(r"(^|\s)(FAILED:|error:|fatal error:|ninja: error)")


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def human(seconds: float) -> str:
    """9 minutes, 37 seconds  (matches the reference screenshot style)."""
    seconds = int(max(0, round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} hour" + ("s" if h != 1 else ""))
    if m:
        parts.append(f"{m} minute" + ("s" if m != 1 else ""))
    parts.append(f"{s} second" + ("s" if s != 1 else ""))
    return ", ".join(parts)


def progress_bar(pct: float, width: int) -> str:
    width = max(4, int(width))
    filled = int(round(pct / 100.0 * width))
    filled = min(width, max(0, filled))
    return "\u2588" * filled + "\u2591" * (width - filled)


# --------------------------------------------------------------------------- #
# The bot
# --------------------------------------------------------------------------- #
class RomBot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.api = f"https://api.telegram.org/bot{cfg['bot_token']}"
        self.chat_id = str(cfg["chat_id"])
        self.lock = threading.Lock()

        state = self._load_state()
        self.log_pos = int(state.get("log_pos", 0))
        self.last_result = state.get("last_result")  # text, for /last
        self._partial = ""                            # holds a half-read line

        # live build state
        self.building = False
        self.start_ts = None
        self.done = 0
        self.total = 0
        self.eta_seconds = None
        self.cmdline = ""
        self.progress_msg_id = None
        self.last_sent = 0.0
        self.last_state_save = 0.0
        self.last_line_ts = time.time()
        self.recent = deque(maxlen=300)   # ring buffer of recent log lines

    # ---------------- state persistence ---------------- #
    def _load_state(self) -> dict:
        try:
            with open(self.cfg["state_file"]) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self):
        try:
            tmp = self.cfg["state_file"] + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"log_pos": self.log_pos, "last_result": self.last_result}, f)
            os.replace(tmp, self.cfg["state_file"])
        except Exception as e:
            print("state save failed:", e)

    # ---------------- Telegram plumbing ---------------- #
    def _tg(self, method: str, **params) -> dict:
        try:
            r = requests.post(f"{self.api}/{method}", data=params, timeout=60)
            return r.json()
        except Exception as e:
            print(f"{method} failed:", e)
            return {"ok": False}

    def send_message(self, text: str) -> dict:
        return self._tg("sendMessage", chat_id=self.chat_id, text=text,
                        parse_mode="HTML", disable_web_page_preview=True)

    def send_to(self, chat_id, text: str) -> dict:
        """Send to an explicit chat (used before chat_id is configured)."""
        return self._tg("sendMessage", chat_id=str(chat_id), text=text,
                        parse_mode="HTML", disable_web_page_preview=True)

    def edit_message(self, msg_id: int, text: str) -> dict:
        return self._tg("editMessageText", chat_id=self.chat_id, message_id=msg_id,
                        text=text, parse_mode="HTML", disable_web_page_preview=True)

    def send_document(self, path: str, caption: str = "") -> bool:
        try:
            with open(path, "rb") as f:
                r = requests.post(
                    f"{self.api}/sendDocument",
                    data={"chat_id": self.chat_id, "caption": caption,
                          "parse_mode": "HTML"},
                    files={"document": (os.path.basename(path), f)},
                    timeout=180,
                )
            return r.json().get("ok", False)
        except Exception as e:
            print("sendDocument failed:", e)
            return False

    # ---------------- paths ---------------- #
    def _error_log_path(self) -> str:
        p = self.cfg["error_log"]
        if not os.path.isabs(p):
            p = os.path.join(self.cfg["build_root"], p)
        return p

    def _output_zips(self):
        import glob
        pattern = self.cfg["output_glob"].format(device=self.cfg["device"])
        if not os.path.isabs(pattern):
            pattern = os.path.join(self.cfg["build_root"], pattern)
        return sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)

    # ---------------- reading the log ---------------- #
    def _read_new_lines(self):
        path = self.cfg["log_file"]
        if not os.path.exists(path):
            return []
        size = os.path.getsize(path)
        if self.log_pos > size:        # log was truncated/rotated
            self.log_pos = 0
            self._partial = ""
        if self.log_pos == size:
            return []
        with open(path, "r", errors="replace") as f:
            f.seek(self.log_pos)
            data = f.read()
            self.log_pos = f.tell()
        data = (self._partial + data).replace("\r", "\n")
        if data.endswith("\n"):
            self._partial = ""
            raw_lines = data.splitlines()
        else:
            parts = data.split("\n")
            self._partial = parts[-1]
            raw_lines = parts[:-1]
        return [strip_ansi(l) for l in raw_lines]

    # ---------------- the monitor loop ---------------- #
    def monitor_loop(self):
        while True:
            try:
                self.tick()
            except Exception as e:
                print("tick error:", e)
            time.sleep(2)

    def tick(self):
        lines = self._read_new_lines()
        now = time.time()
        for line in lines:
            self._handle_line(line)
        if lines:
            self.last_line_ts = now
        # Persist our read position regularly, so restarting the bot does not
        # replay old log lines (which caused stray "started"/"succeeded" posts).
        if now - self.last_state_save > 10:
            self.last_state_save = now
            self._save_state()
        # A cancelled build stops writing to the log. Don't sit on a stale
        # "in progress" card forever.
        if self.building and (now - self.last_line_ts) > self.cfg.get("stale_seconds", 900):
            self._end_build(int(now), 1, stalled=True)
        if self.building and (now - self.last_sent) >= self.cfg["update_interval"]:
            self._send_progress()

    def _handle_line(self, line: str):
        self.recent.append(line)

        m = START_MARK.match(line)
        if m:
            self._start_build(int(m.group(1)), m.group(2).strip())
            return

        m = END_MARK.match(line)
        if m:
            self._end_build(int(m.group(1)), int(m.group(2)))
            return

        pm = PROG_RE.search(line)
        # Only "meaningful" progress lines count - see min_total in DEFAULTS.
        meaningful = bool(pm) and int(pm.group(3)) >= self.cfg.get("min_total", 100)

        # Detect a build that was launched outside the `rb` wrapper
        if not self.building and meaningful:
            self._start_build(int(time.time()), "(started outside the rb wrapper)")

        if not self.building:
            return

        if meaningful:
            self.done = int(pm.group(2))
            self.total = int(pm.group(3))
            em = ETA_RE.search(line)
            if em and any(em.groups()):
                self.eta_seconds = (int(em.group(1) or 0) * 3600
                                    + int(em.group(2) or 0) * 60
                                    + int(em.group(3) or 0))
            return

        if SUCCESS_RE.search(line):
            # Sub-steps (e.g. `m installclean`) print this line too, with a very
            # short duration. Only a genuinely long build counts as the success.
            dur = 0
            dm = re.search(r"\((\d+):(\d+):(\d+)", line)
            if dm:
                dur = int(dm.group(1)) * 3600 + int(dm.group(2)) * 60 + int(dm.group(3))
            if dur >= self.cfg.get("min_build_seconds", 60):
                self._end_build(int(time.time()), 0, success_hint=True)
        elif FAIL_RE.search(line) or NINJA_FAIL_RE.search(line):
            self._end_build(int(time.time()), 1, success_hint=False)

    # ---------------- build lifecycle ---------------- #
    def _start_build(self, ts: int, cmdline: str):
        self.building = True
        self.start_ts = ts or time.time()
        self.done = 0
        self.total = 0
        self.eta_seconds = None
        self.cmdline = cmdline
        self.progress_msg_id = None
        self.last_sent = 0.0
        self.last_line_ts = time.time()
        self.recent.clear()

        head = (
            "\U0001F680 <b>Build started</b> \u2014 "
            f"<b>{esc(self.cfg['rom_name'])}</b> on <code>{esc(self.cfg['device'])}</code>\n"
            f"\u25B6\uFE0F <code>{esc(cmdline or 'build')}</code>"
        )
        self.send_message(head)
        self._send_progress()

    def _progress_text(self) -> str:
        cfg = self.cfg
        now = time.time()
        elapsed = now - (self.start_ts or now)
        pct = (self.done / self.total * 100.0) if self.total else 0.0
        bar = progress_bar(pct, cfg["progress_bar_width"])
        jobs = cfg["jobs"] or (os.cpu_count() or 1)

        out = [
            f"\u2699\uFE0F <b>Build In Progress</b> for <b>{esc(cfg['rom_name'])}</b> "
            f"on <code>{esc(cfg['device'])}</code>",
            f"\U0001F464 User: {esc(cfg['user_name'])}",
            f"\U0001F4CA Progress: <code>[{bar}]</code> {pct:.2f}%",
            f"Actions: {self.done}/{self.total}",
            f"\u231B Elapsed: {human(elapsed)}",
        ]
        if self.eta_seconds:
            # Prefer ninja's own estimate when it prints one.
            out.append(f"\u23F1\uFE0F ETA: {human(self.eta_seconds)}")
        elif 0 < self.done < self.total:
            eta = elapsed * (self.total - self.done) / self.done
            out.append(f"\u23F1\uFE0F ETA: {human(eta)}")
        out += [
            "",
            "\U0001F527 <b>Configuration:</b>",
            f"Variant: {esc(cfg['variant'])}",
            f"Type: {esc(cfg['build_type'])}",
            f"Release: {esc(cfg['release'])}",
            f"Parallel jobs: {jobs}",
        ]
        if cfg.get("quotes"):
            out += ["", f"<i>{esc(random.choice(cfg['quotes']))}</i>"]
        return "\n".join(out)

    def _send_progress(self):
        text = self._progress_text()
        with self.lock:
            if self.progress_msg_id:
                r = self.edit_message(self.progress_msg_id, text)
                if r.get("ok"):
                    self.last_sent = time.time()
                    return
                # Telegram rejects an edit that changes nothing ("message is not
                # modified"). That is fine - do NOT post a duplicate, which is
                # what used to spam the chat when a build stalled or was cancelled.
                if "not modified" in str(r.get("description", "")).lower():
                    self.last_sent = time.time()
                    return
            r = self.send_message(text)
            if r.get("ok"):
                self.progress_msg_id = r["result"]["message_id"]
            self.last_sent = time.time()

    def _error_tail(self, n: int) -> str:
        # Prefer the dedicated error log, else the error-ish lines we buffered
        p = self._error_log_path()
        if os.path.exists(p):
            try:
                with open(p, "r", errors="replace") as f:
                    lines = f.read().splitlines()
                if lines:
                    return "\n".join(lines[-n:])
            except Exception:
                pass
        hits = [l for l in self.recent if ERROR_LINE_RE.search(l)]
        return "\n".join(hits[-n:])

    def _end_build(self, ts: int, code: int, success_hint=None, stalled=False):
        if not self.building:
            return
        self.building = False
        elapsed = (ts or time.time()) - (self.start_ts or time.time())

        if stalled:
            self.send_message(
                "\u26A0\uFE0F No build output for a while \u2014 the build looks "
                "stopped or cancelled. Standing by.")
            self.last_result = "\u26A0\uFE0F Build stopped (no output)."
            self.progress_msg_id = None
            self._save_state()
            return

        success = (code == 0) if success_hint is None else success_hint

        if success:
            zips = self._output_zips()
            lines = [
                f"\u2705 <b>Build Succeeded</b> \u2014 <b>{esc(self.cfg['rom_name'])}</b> "
                f"on <code>{esc(self.cfg['device'])}</code>",
                f"\u231B Total time: {human(elapsed)}",
            ]
            if zips:
                lines.append("\U0001F4E6 Output:")
                lines += [f"<code>{esc(os.path.basename(z))}</code>" for z in zips[:5]]
            else:
                lines.append("(no output zip found yet \u2014 check your output_glob)")
            text = "\n".join(lines)
            self.send_message(text)
            self.last_result = text
        else:
            tail = self._error_tail(self.cfg["max_error_lines"]) or "(no error log captured)"
            text = "\n".join([
                f"\u274C <b>Build Failed</b> \u2014 <b>{esc(self.cfg['rom_name'])}</b> "
                f"on <code>{esc(self.cfg['device'])}</code>",
                f"\u231B After: {human(elapsed)}",
                f"Exit code: {code}",
                "",
                "<b>Last error lines:</b>",
                f"<pre>{esc(tail[-3500:])}</pre>",
            ])
            self.send_message(text)
            self.last_result = text

            # Attach the full error log file, as requested
            p = self._error_log_path()
            if os.path.exists(p):
                self.send_document(p, caption="\U0001F4C4 Build error log")
            else:
                # fall back to shipping the tail of the build log
                bl = self.cfg["log_file"]
                if os.path.exists(bl):
                    self.send_document(bl, caption="\U0001F4C4 Build log (tail)")

        self.progress_msg_id = None
        self._save_state()

    # ---------------- commands ---------------- #
    def _help_text(self) -> str:
        return "\n".join([
            "\U0001F916 <b>rombot</b> \u2014 commands:",
            "/status \u2014 current build status",
            "/last \u2014 result of the last build",
            "/log \u2014 tail of the build log",
            "/errors \u2014 the error log (tail + file)",
            "/id \u2014 show this chat id",
        ])

    def _status_text(self) -> str:
        if not self.building:
            return "\U0001F4A4 No build is running right now. Launch one with <code>rb &lt;your build command&gt;</code>."
        return self._progress_text()

    def _log_tail(self, n=60) -> str:
        p = self.cfg["log_file"]
        if not os.path.exists(p):
            return "(no build log yet)"
        try:
            with open(p, "r", errors="replace") as f:
                lines = f.read().splitlines()
            return "\n".join(strip_ansi(l) for l in lines[-n:])
        except Exception as e:
            return f"(could not read log: {e})"

    def handle_message(self, chat_id, text: str):
        # Any message from the owner cancels a pending auto-shutdown.
        if str(chat_id) == self.chat_id and os.path.exists(os.path.join(BASE_DIR, ".poweroff_pending")):
            try:
                open(os.path.join(BASE_DIR, ".poweroff_cancel"), "w").close()
            except Exception:
                pass
            self.send_to(chat_id, "\u26D4 Auto-shutdown cancelled \u2014 the server will stay up.")
        if text.startswith("/"):
            self.handle_command(chat_id, text)

    def handle_command(self, chat_id, text: str):
        cmd = text.strip().split()[0].lstrip("/").split("@")[0].lower()

        if cmd == "id":
            # reply to the SENDER, not the configured chat (which may still be a placeholder)
            self.send_to(chat_id, f"\U0001F194 Your chat id: <code>{esc(chat_id)}</code>")
            return

        if str(chat_id) != self.chat_id:
            return  # ignore strangers

        if cmd in ("start", "help"):
            self.send_message(self._help_text())
        elif cmd == "status":
            self.send_message(self._status_text())
        elif cmd == "last":
            self.send_message(self.last_result or "\U0001F4A4 No builds recorded yet.")
        elif cmd == "log":
            self.send_message(f"<b>Build log (last 60 lines):</b>\n<pre>{esc(self._log_tail(60)[-3500:])}</pre>")
        elif cmd == "errors":
            tail = self._error_tail(self.cfg["max_error_lines"]) or "(no error log)"
            self.send_message(f"<b>Error log (tail):</b>\n<pre>{esc(tail[-3500:])}</pre>")
            p = self._error_log_path()
            if os.path.exists(p):
                self.send_document(p, caption="\U0001F4C4 Error log")
        else:
            self.send_message("Unknown command. Try /help")

    # ---------------- telegram long-poll ---------------- #
    def announce_online(self):
        self._tg("setMyCommands", commands=json.dumps([
            {"command": "status", "description": "Current build status"},
            {"command": "last", "description": "Last build result"},
            {"command": "log", "description": "Tail of the build log"},
            {"command": "errors", "description": "Error log"},
            {"command": "id", "description": "Show this chat id"},
        ]))
        self.send_message(
            "\U0001F7E2 <b>rombot is online.</b>\n"
            f"Watching <code>{esc(self.cfg['log_file'])}</code> for "
            f"<b>{esc(self.cfg['rom_name'])}</b> on <code>{esc(self.cfg['device'])}</code>.\n"
            "Start a build with <code>rb &lt;your build command&gt;</code>."
        )

    def poll_loop(self):
        offset = None
        while True:
            try:
                r = requests.get(
                    f"{self.api}/getUpdates",
                    params={"offset": offset, "timeout": 25},
                    timeout=40,
                )
                for u in r.json().get("result", []):
                    offset = u["update_id"] + 1
                    msg = u.get("message") or u.get("edited_message")
                    if not msg or "text" not in msg:
                        continue
                    self.handle_message(msg["chat"]["id"], msg["text"])
            except Exception as e:
                print("poll error:", e)
                time.sleep(5)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"warning: could not parse {CONFIG_PATH}: {e}")
    return cfg


# --------------------------------------------------------------------------- #
# Self-test: feed sample logs and print the messages (no Telegram)
# --------------------------------------------------------------------------- #
def selftest(cfg: dict):
    class Fake(RomBot):
        def __init__(self, c):
            super().__init__(c)
            self.out = []

        def _tg(self, method, **params):
            if method in ("sendMessage", "editMessageText"):
                self.out.append(params["text"])
            return {"ok": True, "result": {"message_id": 1}}

        def send_document(self, path, caption=""):
            self.out.append(f"[document] {os.path.basename(path)} :: {caption}")
            return True

    cfg = dict(cfg)
    cfg["state_file"] = "/tmp/rombot_selftest_state.json"
    cfg["log_file"] = "/tmp/rombot_selftest.log"
    now = int(time.time())

    sample_ok = f"""\
###ROMBOT_START {now - 577} brunch fogos
[  1% 2272/227214] some/target
[ 25% 56803/227214] build foo
[ 55% 126308/227214] build bar
#### build completed successfully (00:09:37 (hh:mm:ss)) ####
###ROMBOT_END {now} 0
"""
    sample_fail = f"""\
###ROMBOT_START {now - 300} mka bacon
[ 10% 20000/200000] build x
FAILED: out/target/product/fogos/obj/FOO/bar.o
error: undefined reference to 'baz'
ninja: build stopped: subcommand failed.
#### failed to build some targets (00:05:00 (hh:mm:ss)) ####
###ROMBOT_END {now} 1
"""

    for name, sample in (("SUCCESS", sample_ok), ("FAILURE", sample_fail)):
        print("=" * 60)
        print(name)
        print("=" * 60)
        bot = Fake(cfg)
        for line in sample.splitlines():
            bot._handle_line(line)
        for m in bot.out:
            print("-" * 60)
            print(m)
        print()

    # Show the live mid-build card (the one matching the reference screenshot)
    print("=" * 60)
    print("LIVE PROGRESS CARD (mid-build)")
    print("=" * 60)
    bot = Fake(cfg)
    bot._start_build(now - 577, "brunch fogos")
    bot.done, bot.total = 126308, 227214
    print("-" * 60)
    print(bot._progress_text())


# --------------------------------------------------------------------------- #
# --getid: print the chat id(s) that have messaged the bot
# --------------------------------------------------------------------------- #
def get_id(cfg: dict):
    token = str(cfg["bot_token"])
    if "PUT_YOUR" in token:
        print("Set bot_token in config.json first (from @BotFather).")
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"timeout": 0}, timeout=30)
        data = r.json()
    except Exception as e:
        print("Could not reach Telegram:", e)
        return
    if not data.get("ok"):
        print("Telegram said:", data.get("description", data))
        return
    seen = []
    for u in data.get("result", []):
        m = u.get("message") or u.get("edited_message") or {}
        c = m.get("chat")
        if c and c.get("id") not in [s[0] for s in seen]:
            seen.append((c.get("id"), c.get("first_name") or c.get("username") or c.get("title")))
    if not seen:
        print("No messages found yet.")
        print("Open Telegram, message your bot (send /start), then run:  python3 rombot.py --getid")
        return
    print("Your chat id(s):")
    for cid, name in seen:
        print(f"  {cid}   ({name})")
    print("\nPaste that number into config.json as chat_id.")


# --------------------------------------------------------------------------- #
def main():
    cfg = load_config()

    if "--selftest" in sys.argv:
        selftest(cfg)
        return

    if "--getid" in sys.argv:
        get_id(cfg)
        return

    if "PUT_YOUR" in str(cfg["bot_token"]):
        print("Please set bot_token in config.json first (from @BotFather).")
        sys.exit(1)
    if "PUT_YOUR" in str(cfg["chat_id"]):
        print("chat_id is not set yet. Message your bot with /id to get it,"
              " then paste it into config.json and restart.")

    bot = RomBot(cfg)
    bot.announce_online()
    threading.Thread(target=bot.monitor_loop, daemon=True).start()
    try:
        bot.poll_loop()
    except KeyboardInterrupt:
        print("\nrombot stopped.")


if __name__ == "__main__":
    main()
