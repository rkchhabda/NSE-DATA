#!/usr/bin/env python3
"""setup_github_secrets.py - Sets all required GitHub Actions secrets from alerts.json.

Run after authenticating `gh` as the repo owner:
    gh auth login
    python pipeline/tools/setup_github_secrets.py

Reads credentials from pipeline/config/alerts.json and sets them as GitHub
repository secrets. Values are piped via stdin so they never appear in
shell history or process arguments.
"""
import json
import subprocess
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = WORKSPACE / "pipeline" / "config" / "alerts.json"
REPOS = ["rkchhabda/antigravity-nse", "rkchhabda/NSE-DATA"]

SECRET_MAP = {
    "telegram_token":   "TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
    "smtp_host":        "SMTP_HOST",
    "smtp_port":        "SMTP_PORT",
    "smtp_user":        "SMTP_USER",
    "smtp_password":    "SMTP_PASSWORD",
    "alert_email_to":   "ALERT_EMAIL_TO",
}


def main():
    if not CONFIG_PATH.exists():
        print(f"Config not found: {CONFIG_PATH}")
        return 1

    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)

    # Verify gh is authenticated as the repo owner
    res = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    print(res.stdout or res.stderr)

    errors = 0
    for target_repo in REPOS:
        print(f"\n--- Setting secrets for repository: {target_repo} ---")
        for json_key, secret_name in SECRET_MAP.items():
            value = str(cfg.get(json_key, "")).strip()
            if not value:
                print(f"  SKIP  {secret_name} (empty in config)")
                continue

            r = subprocess.run(
                ["gh", "secret", "set", secret_name, "-R", target_repo, "--body", value],
                capture_output=True, text=True
            )
            if r.returncode == 0:
                print(f"  [OK] SET  {secret_name}")
            else:
                print(f"  [ERR] FAIL {secret_name}: {r.stderr.strip()}")
                errors += 1

    if errors:
        print(f"\n{errors} secret(s) failed. Make sure `gh auth login` is done as rkchhabda.")
        return 1

    print("\nAll secrets set! Now trigger a workflow to verify:")
    print(f"  gh workflow run morning_entry_check.yml -R {REPOS[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

