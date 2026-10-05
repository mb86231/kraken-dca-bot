"""Optional Telegram notifier for order status."""

from __future__ import annotations

import urllib.parse
import urllib.request

from bot.secrets_store import SecretsStore
from bot.utils import redact_sensitive


class Notifier:
    """Optional Telegram notifier for order status. Failures are logged but never crash the bot."""

    def __init__(self):
        values = SecretsStore().resolve("telegram")
        self.bot_token = (values.get("bot_token") or "").strip()
        self.chat_id = (values.get("chat_id") or "").strip()
        self.enabled = bool(self.bot_token and self.chat_id)

    def send(self, message: str) -> bool:
        """Send a plain-text Telegram message."""
        if not self.enabled:
            return False
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            data = urllib.parse.urlencode(
                {
                    "chat_id": self.chat_id,
                    "text": message,
                    "disable_web_page_preview": "true",
                }
            ).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            return True
        except Exception as e:
            # Redact token from any error text before printing/logging
            safe_error = redact_sensitive(str(e))
            print(f"Warning: Telegram notification failed: {safe_error}")
            return False

    def test(self) -> tuple[bool, str]:
        """Send a test message and return (success, message)."""
        if not self.enabled:
            return False, "Telegram is not configured (missing token or chat ID)"
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            data = urllib.parse.urlencode(
                {
                    "chat_id": self.chat_id,
                    "text": "🧪 DCA-Bot test notification: your Telegram alerts are working.",
                    "disable_web_page_preview": "true",
                }
            ).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            return True, "Test message sent successfully"
        except Exception as e:
            safe_error = redact_sensitive(str(e))
            return False, safe_error

    def to_dict(self, mask_secrets: bool = True) -> dict:
        """Return Telegram configuration for the dashboard."""
        return {
            "enabled": self.enabled,
            "bot_token_set": bool(self.bot_token),
            "chat_id_set": bool(self.chat_id),
            "bot_token": "••••••••••" if (self.bot_token and mask_secrets) else self.bot_token,
            "chat_id": "••••••••" if (self.chat_id and mask_secrets) else self.chat_id,
        }
