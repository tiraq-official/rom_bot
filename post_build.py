#!/usr/bin/env python3
"""
post_build.py - runs automatically after a build started with `rb`.

    Success : upload the ROM (rom_upload_script, cloned inside the ROM source
              tree), then power the server off.
    Failure : warn, wait a grace period (cancel by typing in the terminal OR by
              sending any Telegram message to the bot), then power off.

Called by the `rb` shell function with the build's exit code:

    python3 post_build.py <exit_code>

Everything is controlled from the "post_build" block in config.json.
Add --dry-run (or set POST_BUILD_DRY=1) to see what it would do without
uploading or powering off.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PENDING = os.path.join(BASE, ".poweroff_pending")   # bot watches this
CANCEL = os.path.join(BASE, ".poweroff_cancel")     # bot writes this

DRY = ("--dry-run" in sys.argv) or os.environ.get("POST_BUILD_DRY") == "1"

CFG = {}
try:
    with open(os.path.join(BASE, "config.json")) as f:
        CFG = json.load(f)
except Exception as e:
    print("post_build: could not read config.json:", e, flush=True)

PB = CFG.get("post_build", {}) or {}
ENABLED = PB.get("enabled", True)
GRACE = int(PB.get("grace_seconds", 600))
if DRY:
    GRACE = min(GRACE, 3)


def log(*a):
    print("post_build:", *a, flush=True)


def notify(text):
    if DRY:
        log("[telegram]", text)
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{CFG['bot_token']}/sendMessage",
            data={"chat_id": str(CFG["chat_id"]), "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=30,
        )
    except Exception as e:
        log("notify failed:", e)


def find_zip():
    pattern = CFG.get("output_glob", "out/target/product/{device}/*.zip").format(
        device=CFG.get("device", ""))
    if not os.path.isabs(pattern):
        pattern = os.path.join(CFG.get("build_root", os.path.expanduser("~")), pattern)
    zips = [z for z in glob.glob(pattern) if "ota" not in os.path.basename(z).lower()]
    zips.sort(key=os.path.getmtime, reverse=True)
    return zips[0] if zips else None


def run(cmd, cwd=None):
    log("$", cmd)
    if DRY:
        return 0
    return subprocess.call(cmd, shell=True, cwd=cwd)


def do_upload():
    repo = PB.get("upload_repo")
    # release.py uses PRODUCT_DIR = "../out/target/product/fogos" (relative), so
    # the clone must sit INSIDE the ROM source tree - its parent is where out/ is.
    build_root = CFG.get("build_root", os.path.expanduser("~"))
    dest = PB.get("upload_dir") or os.path.join(build_root, "rom_upload_script")
    if not repo:
        log("no upload_repo configured")
        return False
    if os.path.isdir(os.path.join(dest, ".git")):
        run(f"git -C '{dest}' pull --ff-only")
    else:
        if os.path.isdir(dest):
            shutil.rmtree(dest, ignore_errors=True)
        if run(f"git clone '{repo}' '{dest}'") != 0:
            return False
    return run(PB.get("release_cmd", "python3 release.py"), cwd=dest) == 0


def cancel_requested():
    if os.path.exists(CANCEL):
        return True
    if sys.stdin.isatty():
        import select
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if r:
            try:
                sys.stdin.readline()
            except Exception:
                pass
            return True
    return False


def wait_grace():
    """Return True if cancelled, False if the grace period elapsed."""
    try:
        os.remove(CANCEL)
    except OSError:
        pass
    open(PENDING, "w").close()
    log(f"waiting {GRACE}s for interaction - type here or send any Telegram message to cancel")
    end = time.time() + GRACE
    cancelled = False
    while time.time() < end:
        if cancel_requested():
            cancelled = True
            break
        time.sleep(1)
    try:
        os.remove(PENDING)
    except OSError:
        pass
    return cancelled


def poweroff():
    log("powering off")
    if DRY:
        return
    for cmd in ("sudo -n systemctl poweroff", "sudo -n poweroff", "sudo systemctl poweroff"):
        if subprocess.call(cmd, shell=True) == 0:
            return
    log("poweroff FAILED - set up passwordless sudo (see setup_sudoers.sh)")


def main():
    code = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 1
    if not ENABLED:
        log("post-build disabled in config.json")
        return

    if code == 0:
        z = find_zip()
        if not z:
            log("build succeeded but no output zip found - test/partial build, doing nothing")
            return
        notify(f"\U0001F4E4 Build succeeded. Uploading <code>{os.path.basename(z)}</code> ...")
        ok = do_upload() if PB.get("upload_on_success", True) else True
        if ok:
            # Same grace period as a failure: wait, then power off unless you
            # interact (type in the terminal or send any Telegram message).
            notify(f"\U0001F4E4 Upload finished. Powering off in {GRACE // 60} min unless you cancel "
                   "(type here or send any Telegram message).")
            if PB.get("poweroff_on_success", True) and not wait_grace():
                poweroff()
        else:
            notify(f"\u26A0\uFE0F Upload failed. Powering off in {GRACE // 60} min unless you cancel.")
            if PB.get("poweroff_on_failure", True) and not wait_grace():
                poweroff()
    else:
        notify(f"\u274C Build failed (exit {code}). Powering off in {GRACE // 60} min - "
               "type in the terminal or send any Telegram message to cancel.")
        if PB.get("poweroff_on_failure", True) and not wait_grace():
            poweroff()


if __name__ == "__main__":
    main()
