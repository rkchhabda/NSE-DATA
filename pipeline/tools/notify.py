"""notify.py - Multi-channel alert dispatcher (Telegram Bot & Email).

Dispatches 09:30 AM Morning Swing Entry alerts and Evening Screening reports
directly to mobile via:
  1. Telegram Bot (Instant push alert to Telegram mobile app)
  2. Email (Rich HTML and plain-text email with formatted trade tables)

Configuration can be supplied via:
  - Environment variables:
      TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
      SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL_TO
  - Or JSON config file: data/config/alerts.json (or pipeline/config/alerts.json)

Usage:
    py pipeline\tools\notify.py --test
    py pipeline\tools\notify.py --subject "Morning Alert" --body "Test message"
"""

import argparse
import json
import os
import smtplib
import sys
import urllib.parse
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent


DUMMY_PATTERNS = ("your_bot_token", "your_chat_id", "your_telegram", "your_email", "your_gmail", "placeholder")


def is_placeholder(val: str) -> bool:
    if not val:
        return True
    s = str(val).strip().lower()
    return any(p in s for p in DUMMY_PATTERNS)


def clean_telegram_token(token: str) -> str:
    """Strips leading 'bot' prefix if user included it in token."""
    t = str(token).strip()
    if t.lower().startswith("bot") and len(t) > 3 and t[3].isdigit():
        return t[3:]
    return t


def get_alert_config() -> Dict:
    """Loads notification credentials from config file or environment."""
    cfg = {
        "telegram_token": "",
        "telegram_chat_id": "",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
        "smtp_user": "",
        "smtp_password": "",
        "alert_email_to": "",
    }

    # 1. Load local config file first
    config_paths = [
        WORKSPACE_ROOT / "pipeline" / "config" / "alerts.json",
        WORKSPACE_ROOT / "data" / "config" / "alerts.json",
    ]
    for cp in config_paths:
        if cp.exists():
            try:
                with open(cp, "r", encoding="utf-8") as f:
                    file_cfg = json.load(f)
                    for k, v in file_cfg.items():
                        if v and not is_placeholder(str(v)):
                            cfg[k] = v
            except Exception:
                pass

    # 2. Override with env vars only if env vars are NOT placeholders
    env_map = {
        "telegram_token": "TELEGRAM_BOT_TOKEN",
        "telegram_chat_id": "TELEGRAM_CHAT_ID",
        "smtp_host": "SMTP_HOST",
        "smtp_port": "SMTP_PORT",
        "smtp_user": "SMTP_USER",
        "smtp_password": "SMTP_PASSWORD",
        "alert_email_to": "ALERT_EMAIL_TO",
    }
    for cfg_key, env_var in env_map.items():
        val = os.environ.get(env_var)
        if val and not is_placeholder(val):
            cfg[cfg_key] = int(val) if cfg_key == "smtp_port" else val

    if cfg["telegram_token"]:
        cfg["telegram_token"] = clean_telegram_token(cfg["telegram_token"])

    return cfg


def send_telegram_alert(token: str, chat_id: str, message: str) -> bool:
    """Sends a Telegram message via Bot API."""
    token = clean_telegram_token(token)
    if not token or is_placeholder(token) or not chat_id or is_placeholder(chat_id):
        print("  [!] Telegram alert skipped: Valid TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID required.")
        return False

    if ":" not in token:
        print(f"  [!] Invalid Telegram bot token format '{token[:8]}...'. Tokens from BotFather look like '123456789:ABC_def...'.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            if res_data.get("ok"):
                print("  [OK] Telegram mobile alert sent successfully.")
                return True
            else:
                print(f"  [!] Telegram error response: {res_data}")
                return False
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        if "chat not found" in err_body.lower():
            print("  [!] Telegram Error: 'chat not found'.")
            print("      Action Required: Open your bot in Telegram (https://t.me/MyNseAlertbot) and tap 'START'.")
            print("      Telegram requires you to send /start once so the bot has permission to message you.")
        else:
            print(f"  [!] Telegram HTTP error ({e.code}): {err_body}")
        return False
    except Exception as e:
        print(f"  [!] Failed to send Telegram alert: {e}")
        return False


def send_email_alert(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    to_email: str,
    subject: str,
    html_content: str,
    plain_content: Optional[str] = None,
) -> bool:
    """Sends an email alert via SMTP (supporting SSL or STARTTLS)."""
    clean_pass = str(smtp_password).replace(" ", "").strip()
    if not smtp_user or not clean_pass or not to_email:
        print("  [!] Email alert skipped: SMTP credentials or ALERT_EMAIL_TO not configured.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"NSE Swing Alert <{smtp_user}>"
    msg["To"] = to_email

    if plain_content:
        msg.attach(MIMEText(plain_content, "plain", "utf-8"))
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    try:
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=12)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=12)
            server.starttls()

        server.login(smtp_user, clean_pass)
        server.sendmail(smtp_user, [to_email], msg.as_string())
        server.quit()
        print(f"  [OK] Email alert sent to {to_email}.")
        return True
    except Exception as e:
        print(f"  [!] Failed to send email alert: {e}")
        return False


def format_morning_telegram_message(evaluations: List[Dict], date_str: str) -> str:
    """Formats 09:30 morning evaluations into a concise Telegram HTML message."""
    lines = [
        f"🚨 <b>NSE 09:30 AM SWING ENTRY ALERTS</b> ({date_str})",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]

    for idx, e in enumerate(evaluations, 1):
        sym = e["symbol"]
        v = e["verdict"]
        p930 = e["p930"]
        chg = e["change_pct"]
        ord_info = e.get("order")
        fo_info = e.get("fo")

        fo_line = ""
        if fo_info:
            cw_str = f" | Wall: ₹{fo_info['call_wall']}" if fo_info.get("call_wall") else ""
            fo_line = f"\n• F&O: <b>{fo_info.get('buildup', 'N/A')}</b> (OI: {fo_info.get('fut_oi_change_pct', 0.0):+0.1f}%{cw_str})"

        if v == "GREEN_LIGHT":
            badge = "🟢 <b>BUY LIMIT</b>"
            details = (
                f"• Limit Entry: <b>₹{ord_info['limit_price']}</b>\n"
                f"• Stop Loss: ₹{ord_info['stop_loss']} | T1: ₹{ord_info['target_1']}\n"
                f"• Size: <b>{ord_info['shares']} shares</b> (~₹{ord_info['max_investment']:,.0f})"
                f"{fo_line}"
            )
        elif v == "BREAKOUT_TRIGGER":
            badge = "🟡 <b>STOP-LIMIT (WAIT)</b>"
            details = (
                f"• Trigger: <b>₹{ord_info['trigger_price']}</b> | Limit: ₹{ord_info['limit_price']}\n"
                f"• Stop Loss: ₹{ord_info['stop_loss']} | T1: ₹{ord_info['target_1']}\n"
                f"• Size: <b>{ord_info['shares']} shares</b> (~₹{ord_info['max_investment']:,.0f})"
                f"{fo_line}"
            )
        else:
            badge = "❌ <b>SKIP TODAY</b>"
            details = f"• {e['reason']}{fo_line}"

        lines.append(f"\n<b>#{idx} {sym}</b> | CMP: ₹{p930:.2f} ({chg:+.2f}%)")
        lines.append(f"Verdict: {badge}")
        lines.append(details)

    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("<i>Risk Rule: Max 1% portfolio risk per trade.</i>")
    return "\n".join(lines)


def format_morning_email_html(evaluations: List[Dict], date_str: str, capital: float, risk_pct: float) -> str:
    """Formats 09:30 morning evaluations into a responsive HTML email table."""
    rows = []
    for idx, e in enumerate(evaluations, 1):
        sym = e["symbol"]
        v = e["verdict"]
        ord_info = e.get("order")
        fo_info = e.get("fo")
        fo_badge = f'<br><span style="font-size:11px;color:#0066cc;font-weight:normal;">F&O: {fo_info.get("buildup", "")}</span>' if fo_info else ""

        if v == "GREEN_LIGHT":
            status_badge = '<span style="background-color:#d4edda;color:#155724;padding:4px 8px;border-radius:4px;font-weight:bold;">🟢 BUY LIMIT</span>'
            order_text = f"<b>Limit:</b> ₹{ord_info['limit_price']}<br><b>SL:</b> ₹{ord_info['stop_loss']}<br><b>Target:</b> ₹{ord_info['target_1']}"
            size_text = f"<b>{ord_info['shares']}</b> shares<br>(~₹{ord_info['max_investment']:,.0f})"
        elif v == "BREAKOUT_TRIGGER":
            status_badge = '<span style="background-color:#fff3cd;color:#856404;padding:4px 8px;border-radius:4px;font-weight:bold;">🟡 STOP-LIMIT</span>'
            order_text = f"<b>Trigger:</b> ₹{ord_info['trigger_price']}<br><b>Limit:</b> ₹{ord_info['limit_price']}<br><b>SL:</b> ₹{ord_info['stop_loss']}"
            size_text = f"<b>{ord_info['shares']}</b> shares<br>(~₹{ord_info['max_investment']:,.0f})"
        else:
            status_badge = '<span style="background-color:#f8d7da;color:#721c24;padding:4px 8px;border-radius:4px;font-weight:bold;">❌ SKIP TODAY</span>'
            order_text = f"<i>{e['reason']}</i>"
            size_text = "—"

        rows.append(f"""
        <tr style="border-bottom:1px solid #dee2e6;">
            <td style="padding:10px;"><b>#{idx} {sym}</b>{fo_badge}</td>
            <td style="padding:10px;">₹{e['p930']:.2f} ({e['change_pct']:+.2f}%)</td>
            <td style="padding:10px;">{status_badge}</td>
            <td style="padding:10px;">{order_text}</td>
            <td style="padding:10px;">{size_text}</td>
        </tr>
        """)

    html = f"""
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family:Arial,sans-serif;color:#333;margin:0;padding:20px;background-color:#f8f9fa;">
        <div style="max-width:700px;margin:auto;background:#fff;padding:25px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1);">
            <h2 style="color:#1a73e8;margin-top:0;">🚀 NSE 09:30 AM Morning Swing Entry Report</h2>
            <p style="color:#666;font-size:14px;">Date: <b>{date_str}</b> | Capital: <b>₹{capital:,.0f}</b> | Risk/Trade: <b>{risk_pct}%</b></p>
            <table style="width:100%;border-collapse:collapse;margin-top:15px;font-size:14px;">
                <thead>
                    <tr style="background:#f1f3f4;text-align:left;">
                        <th style="padding:10px;">Symbol</th>
                        <th style="padding:10px;">CMP (09:30)</th>
                        <th style="padding:10px;">Verdict</th>
                        <th style="padding:10px;">Order Details</th>
                        <th style="padding:10px;">Position Size</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(rows)}
                </tbody>
            </table>
            <div style="margin-top:20px;padding:12px;background:#e8f0fe;border-left:4px solid:#1a73e8;border-radius:4px;font-size:13px;">
                <b>Discipline Rule:</b> Never use market orders at open. Only enter GREEN LIGHT setups with limit orders, or set Buy Stop-Limits above the 15-min high for breakout candidates.
            </div>
        </div>
    </body>
    </html>
    """
    return html


def dispatch_morning_alerts(evaluations: List[Dict], date_str: str, capital: float, risk_pct: float) -> None:
    """Dispatches 09:30 morning alerts to both Telegram and Email."""
    cfg = get_alert_config()

    # 1. Telegram
    tg_text = format_morning_telegram_message(evaluations, date_str)
    send_telegram_alert(cfg["telegram_token"], cfg["telegram_chat_id"], tg_text)

    # 2. Email
    subject = f"🚨 NSE Swing Alerts (09:30 AM): {date_str}"
    email_html = format_morning_email_html(evaluations, date_str, capital, risk_pct)
    send_email_alert(
        smtp_host=cfg["smtp_host"],
        smtp_port=cfg["smtp_port"],
        smtp_user=cfg["smtp_user"],
        smtp_password=cfg["smtp_password"],
        to_email=cfg["alert_email_to"],
        subject=subject,
        html_content=email_html,
        plain_content=tg_text,
    )


def main():
    parser = argparse.ArgumentParser(description="Test or send notifications via Telegram and Email")
    parser.add_argument("--test", action="store_true", help="Send a test notification using configured channels")
    parser.add_argument("--subject", type=str, default="NSE Swing Test Alert", help="Alert subject")
    parser.add_argument("--body", type=str, default="This is a test notification from Antigravity NSE.", help="Alert body")
    args = parser.parse_args()

    cfg = get_alert_config()
    print("Notification Configuration Status:")
    print(f"  • Telegram Configured: {'YES' if (cfg['telegram_token'] and cfg['telegram_chat_id']) else 'NO'}")
    print(f"  • Email Configured:    {'YES' if (cfg['smtp_user'] and cfg['alert_email_to']) else 'NO'}")

    if args.test:
        print("\nSending test alerts...")
        if cfg["telegram_token"] and cfg["telegram_chat_id"]:
            send_telegram_alert(cfg["telegram_token"], cfg["telegram_chat_id"], f"<b>{args.subject}</b>\n\n{args.body}")
        if cfg["smtp_user"] and cfg["alert_email_to"]:
            send_email_alert(
                smtp_host=cfg["smtp_host"],
                smtp_port=cfg["smtp_port"],
                smtp_user=cfg["smtp_user"],
                smtp_password=cfg["smtp_password"],
                to_email=cfg["alert_email_to"],
                subject=args.subject,
                html_content=f"<p>{args.body}</p>",
                plain_content=args.body,
            )


if __name__ == "__main__":
    main()
