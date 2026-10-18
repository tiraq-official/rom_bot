# rombot — Telegram build monitor for custom ROM builds

A small background bot for your GCP build VM. It watches your ROM build and
posts to Telegram:

- a **live "Build In Progress" card** — progress bar, %, actions `X/Y`, elapsed
  time, ETA, and the config block (variant / type / release), just like the
  screenshot you shared
- a **success message** when the build finishes cleanly (with the output zip name)
- a **failure message** plus the **error log attached as a file**

It only needs `requests`. No heavy libraries, no database.

---

## How it hooks into your build (the one small change)

To show live progress the bot has to see the build's output stream. Because your
build command changes from ROM to ROM (`ax -br`, `brunch fogos`, `mka bacon`,
`m derp`, …), it does **not** hardcode any command. Instead you prefix your normal
command with `rb`:

```bash
rb ax -br -j16
rb brunch fogos
rb mka bacon
```

`rb` is a small shell **function**, not a script. That distinction matters: ROM
build commands like `ax`, `m`, `brunch` and `mka` are themselves shell functions
created by `. build/envsetup.sh`, and a separate script cannot call them (and
`stdbuf` cannot exec a function). So `rb` has to live inside your shell — see
step 8. It records a START marker, streams the build output into `build.log`
(which the bot watches), and records an END marker with the real exit code.
Same command, just `rb` in front.

> If you'd rather not use `rb`, the bot still works if you tee the build yourself:
> `ax -br -j16 2>&1 | tee -a ~/rombot/build.log`. It auto-detects a build the
> moment progress lines appear in the log.

---

## Dynamic ROM detection (works across ROMs)

You do **not** edit `config.json` for each ROM. When you run `rb <command>` the
wrapper:

1. looks at the source tree you're in (walks up to find `build/envsetup.sh`),
2. works out the **ROM name** (from `vendor/<rom>/`, else the folder name),
   **codename**, **variant**, **release**, `build_root` and your user, then
3. writes them into `config.json` and hands them to the bot.

The bot also reads the values the build itself prints in its config banner and
prefers those, so the card stays correct even if detection guessed wrong:

- `TARGET_PRODUCT=shinkai_fogos` → device `fogos`
- `TARGET_BUILD_VARIANT=userdebug` → variant `userdebug`
- `BUILD_ID=CP2A.260605.016` → release `cp2a`
- `SHINKAI_VERSION=…` / `SHINKAI_MAINTAINER=…` → ROM name `Shinkai`

(The `<ROM>_VERSION=` line only names the ROM when the prefix isn't a shared base
like `LINEAGE`/`AOSP`, so Lineage-based ROMs that reuse `LINEAGE_VERSION` fall
back to the source-tree name instead.)

So switching ROMs is just: sync into a folder, `cd` into it, `rb <build cmd>` —
the status message picks up the right name and device by itself. The
`rom_name` / `device` / `variant` / `release` fields in `config.json` become
fallbacks (used only when detection and the banner find nothing).

See what it detects for the current tree:

```bash
python3 ~/rombot/detect_rom.py
```

ROM name comes from `vendor/<rom>/` if that matches the folder you synced into,
otherwise from the folder name — so sync Axion into `~/axion`, Shinkai into
`~/shinkai`, and it just works. If a name comes out wrong, rename the source
folder to match the ROM (or tweak `guess_rom_name()` in `detect_rom.py`).

---

## Where to run it — recommendation

**Run it on the build VM itself**, in the same account that runs the build. That
way it reads `build.log` and `out/error.log` locally with zero network plumbing.
Running it on a separate box would mean shipping logs across (rsync/ssh), which is
more moving parts for no benefit.

On a **spot VM** this is fine: if the VM is preempted the build dies too, and when
the VM comes back the bot restarts and resumes (it remembers its read position, so
it won't re-report old builds).

---

## Setup

### 1. Create the bot and get a token
- In Telegram, message **@BotFather** → `/newbot` → follow prompts.
- Copy the token it gives you (looks like `123456789:AA...`).

### 2. Put the files on the VM
Copy this folder to the VM, e.g. to `~/rombot`:

```
~/rombot/
├── rombot.py          # the Telegram bot daemon
├── rb                 # the build wrapper (source it, it's a shell function)
├── post_build.py      # upload + auto-shutdown after a build
├── setup_sudoers.sh   # one-time: passwordless poweroff
├── config.json
├── rombot.service
└── README.md
```

### 3. Install the one dependency
```bash
sudo apt install -y python3-requests
# or:  pip install requests
```

### 4. Edit `config.json`
At minimum set:
- `bot_token` — from BotFather
- `chat_id` — see step 5
- `log_file`, `state_file` — absolute paths under `~/rombot`
- `rom_name`, `device` (already `fogos`), `user_name`
- `build_root` — your ROM source root (e.g. `/home/you/android`)
- `variant` / `build_type` / `release` — shown in the card
- `output_glob` — where your flashable zip lands (default
  `out/target/product/{device}/*.zip`)

### 5. Get your `chat_id`
Leave `chat_id` as the placeholder for this step. Open Telegram, find your bot and
send it anything (e.g. `/start`). Then, back in the terminal:

```bash
cd ~/rombot
python3 rombot.py --getid
```

It prints your chat id, e.g. `123456789 (Saptak)`. Paste that into `config.json`
as `chat_id`.

> Alternative: run `python3 rombot.py` (it is normal for it to sit there silently
> — it is waiting for messages) and send the bot `/id`. Then Ctrl-C to stop it.

### 5b. Set the chat_id and start
```bash
nano config.json     # put the number in "chat_id", save (Ctrl-O, Enter, Ctrl-X)
python3 rombot.py    # you should now get "rombot is online" in Telegram
```

### 6. Test it
```bash
cd ~/rombot
python3 rombot.py
```
You should get "rombot is online". In another terminal:
```bash
rb echo "#### build completed successfully (00:00:05 (hh:mm:ss)) ####"
```
…and a success message should pop up in Telegram.

### 7. Run it in the background (always on)
**Option A — systemd (recommended):**
```bash
sudo cp ~/rombot/rombot.service /etc/systemd/system/rombot.service
# edit User= and the paths inside first
sudo nano /etc/systemd/system/rombot.service
sudo systemctl daemon-reload
sudo systemctl enable --now rombot
sudo systemctl status rombot        # check it's running
journalctl -u rombot -f             # live logs
```

**Option B — inside byobu/tmux** (no sudo):
```bash
cd ~/rombot
python3 rombot.py
# then detach with Ctrl-A D (byobu) — it keeps running
```

### 8. Make `rb` available in your shell

`rb` is a function, so you **source** it (once per shell). Add it to `~/.bashrc`
so every new shell has it:

```bash
echo 'source ~/rombot/rb' >> ~/.bashrc
source ~/.bashrc
```

Then, in your build shell (after `. build/envsetup.sh`), build as usual:

```bash
rb ax -br -j16
```

> If you previously added an alias, or a `/usr/local/bin/rb` launcher, remove it
> (`sudo rm -f /usr/local/bin/rb`) — the sourced function replaces it.

---

## Post-build automation (upload + auto-shutdown)

When a build started with `rb` finishes, `rb` runs `post_build.py`, which:

- **On success:** uploads the ROM via
  [rom_upload_script](https://github.com/tiraq-official/rom_upload_script),
  then waits `grace_seconds` and powers the server off (same grace as a failure
  — cancel by typing in the terminal or sending any Telegram message).

  The upload repo is cloned **inside your ROM source tree**
  (`<build_root>/rom_upload_script`, e.g. `~/axion/rom_upload_script`), then
  `python3 release.py` is run from there. That location matters: `release.py`
  looks for the ROM at `../out/target/product/fogos`, so it has to sit one level
  below the folder that contains `out/`.

  `release.py` needs `rsync` (`sudo apt install -y rsync`) and a SourceForge SSH
  key registered on the machine; without the key it just skips the SourceForge
  mirror and still uploads to GoFile/Pixeldrain.
- **On failure:** posts a warning, waits `grace_seconds` (default 600 = 10 min),
  then powers the server off.

Both paths use the same `grace_seconds`, so success and failure behave the same
way: upload (or fail), wait, then power off unless you interact.

**Cancelling the shutdown.** During the grace period the shutdown is cancelled if
EITHER of these happens:

- you **type anything in the build terminal**, or
- you **send any Telegram message** to the bot (e.g. `/cancel`, or even `/status`).

The bot replies "⛔ Auto-shutdown cancelled". Handy when you're away from the
terminal.

**Two guards, so it never shuts down by accident:**

- The success path only acts if it actually finds an output zip. A quick test
  build (`rb echo ...`) produces no zip, so nothing is uploaded and nothing is
  powered off.
- Set `"post_build": { "enabled": false }` in `config.json` to switch the whole
  thing off.

**One-time setup: passwordless poweroff.** `post_build.py` runs
`sudo -n systemctl poweroff`, so your user needs to be allowed to power off
without a password prompt (otherwise the shutdown hangs when you're not there):

```bash
bash ~/rombot/setup_sudoers.sh
```

**Tuning** (in the `post_build` block of `config.json`):

| Key | Meaning |
|-----|---------|
| `enabled` | master on/off |
| `grace_seconds` | how long to wait before shutdown on failure (default 600) |
| `upload_on_success` | run the upload step at all |
| `upload_repo` | the git repo to clone for uploading |
| `upload_dir` | where to clone it — must be inside the ROM source tree (default `<build_root>/rom_upload_script`) |
| `release_cmd` | the command run inside `upload_dir` (default `python3 release.py`) |
| `poweroff_on_success` / `poweroff_on_failure` | whether to power off in each case |

> Because this runs from the shell where you launched `rb`, keep that session
> alive (byobu is ideal) until the build finishes. If you close it, the build's
> post-build steps won't run.

---

## Telegram commands

| Command   | What it does                          |
|-----------|---------------------------------------|
| `/status` | Current build status (the live card)  |
| `/last`   | Result of the last build              |
| `/log`    | Tail of the build log (60 lines)      |
| `/errors` | Error log tail + the file attached    |
| `/id`     | Show the chat id (for setup)          |
| `/help`   | List commands                         |

---

## Notes & tuning

- **Update rate:** `update_interval` (seconds, default 60). The bot edits a single
  message in place, so it doesn't spam your chat.
- **Parallel jobs line:** the card shows `Parallel jobs` from `jobs` in config
  (`0` = auto = CPU count). It is *not* the same as the reference bot's
  "1199 tasks running" figure, which is ninja's in-flight edge count and isn't
  printed to the log — so I show the honest number instead.
- **Quotes:** the fun line under the card comes from the `quotes` list; edit or
  empty it.
- **Progress parsing:** it reads standard ninja/soong status lines
  (`[ 55% 126308/227214] ...`) and Android's result lines
  (`#### build completed successfully` / `#### failed to build some targets`).
  It also handles a build that prints an ETA inside the brackets
  (`[  3% 915/27036 40m3s remaining] ...`) and shows ninja's own estimate
  when it's available.
  Two settings keep the bar honest on multi-phase Android builds:
  - `min_total` (default 100) — progress lines whose total is smaller are
    ignored, so the tiny soong "bootstrap" step that reports `[100% 1/1]` no
    longer parks the bar at 100% before ninja starts.
  - `min_build_seconds` (default 60) — a `#### build completed successfully
    (00:00:04 …)` line shorter than this is ignored, because sub-steps like
    `m installclean` print that exact line a few seconds into the build.
  Raise `min_total` (e.g. to 10000) if your build has other small phases you'd
  rather not show.
- **Stale / cancelled builds:** if a running build stops writing to the log for
  `stale_seconds` (default 900 = 15 min) — e.g. you cancelled it with Ctrl-C —
  the bot posts one "build looks stopped or cancelled" note and stops showing
  "in progress". Lower it if you want it to give up sooner.
- **No duplicate spam:** the bot edits one message in place, and a Telegram
  "message is not modified" reply (which happens when nothing changed) is
  ignored rather than posted again.
- **`build.log` grows large** over time (it's the full build output). Rotate or
  delete it occasionally; the bot resets its read position automatically if the
  file shrinks.
- **`rb` uses `stdbuf`** on real executables so progress appears live rather
  than in buffered chunks; shell functions (like `ax`) are run directly.

---

## Troubleshooting

- **No progress messages, only start/finish.** Your build may not print ninja
  status lines, or output is buffered. Make sure you launch with `rb` (sourced as
  a function). Check `build.log` — you should see `[ X% Y/Z]` lines.
- **`stdbuf: failed to run command 'ax': No such file or directory`.** You are
  using the old script version of `rb`. `ax`/`m`/`brunch` are shell functions, so
  `rb` must be the sourced function (step 8), not an executable script. Also make
  sure `. build/envsetup.sh` has been run in that shell first.
- **Bot doesn't respond.** Token/chat id wrong, or the process isn't running
  (`systemctl status rombot`). Try `/id`.
- **"state save failed" on startup.** The folder in `state_file` / `log_file`
  doesn't exist. Create it (`mkdir -p ~/rombot`).
- **Wrong zip name on success.** Fix `output_glob` to match your tree.
