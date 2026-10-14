#!/usr/bin/env bash
#
# setup_sudoers.sh - allow the build user to power off without a password.
#
# post_build.py calls `sudo -n systemctl poweroff`. Without a sudoers rule that
# would ask for a password, which nobody can type when the server is shutting
# down unattended. Run this once:
#
#     bash ~/rombot/setup_sudoers.sh
#
set -e

USER_NAME="${1:-$USER}"
RULE="$USER_NAME ALL=(ALL) NOPASSWD: /usr/bin/systemctl poweroff, /usr/sbin/poweroff"

echo "Granting passwordless poweroff to: $USER_NAME"
echo "$RULE" | sudo tee /etc/sudoers.d/rombot-poweroff >/dev/null
sudo chmod 440 /etc/sudoers.d/rombot-poweroff

echo "Done. Test it with:  sudo -n systemctl poweroff --help >/dev/null && echo OK"
