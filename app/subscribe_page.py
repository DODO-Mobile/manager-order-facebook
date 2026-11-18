"""One-off: subscribe Page to this App for the `feed` field, then show current subscription.
Usage: python -m app.subscribe_page
"""
import json
from .config import Settings
from .messenger import Messenger

cfg = Settings.load()
m = Messenger(cfg)
print("subscribe:", json.dumps(m.subscribe_page(cfg.page_id), ensure_ascii=False))
print("current  :", json.dumps(m.list_subscriptions(cfg.page_id), ensure_ascii=False))
