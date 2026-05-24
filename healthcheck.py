#!/usr/bin/env python3
"""
Health check probe — imports BOT_TOKEN directly from bot.py
Exits 0 (healthy) or 1 (unhealthy).
"""
import sys
import requests

# Import token directly from bot config
try:
    from bot import BOT_TOKEN
except ImportError:
    print("UNHEALTHY: Could not import bot.py")
    sys.exit(1)

def check():
    if not BOT_TOKEN or "YOUR_TELEGRAM_BOT_TOKEN_HERE" in BOT_TOKEN:
        print("UNHEALTHY: BOT_TOKEN not configured in bot.py")
        sys.exit(1)

    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getMe",
            timeout=8,
        )
        data = resp.json()

        if resp.status_code == 200 and data.get("ok"):
            name = data["result"].get("username", "unknown")
            print(f"HEALTHY: Bot @{name} is reachable")
            sys.exit(0)
        else:
            print(f"UNHEALTHY: API returned {resp.status_code} — {data}")
            sys.exit(1)

    except requests.exceptions.ConnectionError:
        print("UNHEALTHY: Cannot reach Telegram API")
        sys.exit(1)
    except requests.exceptions.Timeout:
        print("UNHEALTHY: Telegram API timed out")
        sys.exit(1)
    except Exception as e:
        print(f"UNHEALTHY: {e}")
        sys.exit(1)


if __name__ == "__main__":
    check()
