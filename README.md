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
build command changes from ROM to ROM (`brunch fogos`, `mka bacon`, `m derp`,
`ax -br`, …), it does **not** hardcode any command. Instead you prefix your normal
command with `rb`:

```bash
rb brunch fogos
rb mka bacon
rb m derp
```

`rb` is a 30-line wrapper: it records a START marker, streams the build output
into `build.log` (which the bot watches), and records an END marker with the real
exit code. That's the whole workflow change — same command, just `rb` in front.

> If you'd rather not use `rb`, the bot still works if you tee the build yourself:
> `brunch fogos 2>&1 | tee -a ~/rombot/build.log`. It auto-detects a build the
> moment progress lines appear in the log.

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
├── rombot.py
├── rb
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
You can leave `chat_id` as the placeholder for this step.
```bash
cd ~/rombot
python3 rombot.py          # run it once in the foreground
```
Now open Telegram, find your bot, and send it `/id`. It replies with your chat id.
Paste that into `config.json` as `chat_id` and stop the bot (Ctrl-C).

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

### 8. Make `rb` easy to type
```bash
chmod +x ~/rombot/rb
# add an alias so you can call it from anywhere:
echo 'alias rb="~/rombot/rb"' >> ~/.bashrc && source ~/.bashrc
```
Then build as usual:
```bash
rb brunch fogos
```

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
- **`build.log` grows large** over time (it's the full build output). Rotate or
  delete it occasionally; the bot resets its read position automatically if the
  file shrinks.
- **`rb` uses `stdbuf`** so progress appears live rather than in buffered chunks.

---

## Troubleshooting

- **No progress messages, only start/finish.** Your build may not print ninja
  status lines, or output is buffered. Make sure you launch with `rb` (which uses
  `stdbuf`). Check `build.log` — you should see `[ X% Y/Z]` lines.
- **Bot doesn't respond.** Token/chat id wrong, or the process isn't running
  (`systemctl status rombot`). Try `/id`.
- **"state save failed" on startup.** The folder in `state_file` / `log_file`
  doesn't exist. Create it (`mkdir -p ~/rombot`).
- **Wrong zip name on success.** Fix `output_glob` to match your tree.
