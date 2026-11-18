import json
from sqlalchemy import select
from .db import CatalogItem, FacebookPost


def load_seed(Session, path: str):
    if not path:
        return
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    with Session() as s, s.begin():
        for p in data.get("posts", []):
            row = s.execute(select(FacebookPost).where(FacebookPost.page_id == p["page_id"],
                            FacebookPost.post_id == p["post_id"])).scalar_one_or_none()
            if row:
                for k, v in p.items():
                    setattr(row, k, v)
            else:
                s.add(FacebookPost(**p))
        for c in data.get("catalog", []):
            row = s.execute(select(CatalogItem).where(CatalogItem.catalog_id == c["catalog_id"],
                            CatalogItem.sku == c["sku"])).scalar_one_or_none()
            if row:
                for k, v in c.items():
                    setattr(row, k, v)
            else:
                s.add(CatalogItem(**c))
