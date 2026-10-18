#!/usr/bin/env python3
"""
detect_rom.py - work out which ROM/device a synced source tree is, so rombot
shows the right thing without you editing config.json for every ROM.

Run from inside the source tree (the `rb` wrapper does this automatically).
Prints one line of JSON:

    {"build_root": "...", "rom_name": "Shinkai", "device": "fogos",
     "variant": "userdebug", "release": "bp4a", "user_name": "saptak"}

Anything it can't work out is an empty string; rombot then falls back to
config.json, and to the values it reads from the build log itself.
"""

import glob
import json
import os
import sys

# vendor/ dirs that are a shared base, not the ROM's own name
BASE_VENDORS = {"lineage", "lineageos", "aosp", "aospa", "gms", "google",
                "partner_gms", "common", "aosip", "aokp", "derp", "infinity"}


def find_root(start):
    """Walk up until we find build/envsetup.sh - that's the source root."""
    d = os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(d, "build", "envsetup.sh")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return os.path.abspath(start)
        d = parent


def guess_rom_name(root):
    folder = os.path.basename(root)
    cands = set()
    patterns = (
        "vendor/*/config/common.mk",
        "vendor/*/config/common_full_phone.mk",
        "vendor/*/build/envsetup.sh",
        "vendor/*/config/version.mk",
        "vendor/*/vendorsetup.sh",
    )
    for pat in patterns:
        for f in glob.glob(os.path.join(root, pat)):
            parts = os.path.normpath(f).split(os.sep)
            if "vendor" in parts:
                i = parts.index("vendor")
                if i + 1 < len(parts):
                    cands.add(parts[i + 1])
    # prefer a vendor dir that matches the folder name (you sync Axion into ~/axion)
    if folder.lower() in cands:
        return folder.capitalize()
    rest = sorted(c for c in cands if c.lower() not in BASE_VENDORS)
    if rest:
        return rest[0].capitalize()
    if cands:
        return sorted(cands)[0].capitalize()
    return folder.capitalize()


def guess_device(root, args):
    tp = os.environ.get("TARGET_PRODUCT", "")
    if tp:
        return tp.split("_")[-1]
    td = os.environ.get("TARGET_DEVICE", "")
    if td:
        return td
    # a codename on the build command line, e.g. `rb brunch fogos`
    for a in args:
        if a and not a.startswith("-") and glob.glob(os.path.join(root, "device", "*", a)):
            return a
    # an already-built device under out/
    for p in glob.glob(os.path.join(root, "out", "target", "product", "*")):
        if not os.path.isdir(p):
            continue
        if glob.glob(os.path.join(p, "*.zip")) or os.path.isdir(os.path.join(p, "system")):
            return os.path.basename(p)
    return ""


def guess_variant(root):
    v = os.environ.get("TARGET_BUILD_VARIANT", "")
    return v if v in ("user", "userdebug", "eng") else ""


def guess_release(root, device):
    if not device:
        return ""
    bp = os.path.join(root, "out", "target", "product", device, "system", "build.prop")
    if os.path.isfile(bp):
        try:
            with open(bp, errors="ignore") as f:
                for line in f:
                    if line.startswith("ro.build.id="):
                        return line.split("=", 1)[1].strip().split(".")[0].lower()
        except OSError:
            pass
    return ""


def detect():
    argv = sys.argv[1:]
    write_target = None
    if "--write" in argv:
        i = argv.index("--write")
        write_target = argv[i + 1] if i + 1 < len(argv) else None
        del argv[i:i + 2]

    root = find_root(os.getcwd())
    device = guess_device(root, argv)
    info = {
        "build_root": root,
        "rom_name": guess_rom_name(root),
        "device": device,
        "variant": guess_variant(root),
        "release": guess_release(root, device),
        "user_name": os.environ.get("USER") or os.environ.get("LOGNAME") or "",
    }

    if write_target:
        try:
            with open(write_target) as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        for k, v in info.items():
            if v:
                cfg[k] = v
        try:
            with open(write_target, "w") as f:
                json.dump(cfg, f, indent=2)
        except OSError:
            pass

    return info


def main():
    try:
        info = detect()
    except Exception:
        info = {"build_root": os.getcwd(), "rom_name": "", "device": "",
                "variant": "", "release": "", "user_name": ""}
    print(json.dumps(info))


if __name__ == "__main__":
    main()
