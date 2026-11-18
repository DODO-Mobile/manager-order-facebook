"""Deterministic order parser. Prices come from catalog rows only."""
import re
import unicodedata
from dataclasses import dataclass, field

PARSER_VERSION = "rules-1.0"

NUM_WORDS = {"mot": 1, "hai": 2, "ba": 3, "bon": 4, "tu": 4, "nam": 5, "sau": 6,
             "bay": 7, "tam": 8, "chin": 9, "muoi": 10}
AMBIGUOUS_WORDS = {"cham", "ib", "inbox", "check", "ck", "gia", "bao nhieu", "con khong", "con ko"}
SPLIT_RE = re.compile(r"\s*(?:,|;|\+|&|\n|\bva\b|\bvoi\b|\bcung\b)\s*")


def norm(text: str) -> str:
    text = text.replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s?]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _has(word: str, text: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(norm(word))}(?!\w)", text) is not None


@dataclass
class Line:
    sku: str
    name: str
    color: str | None
    size: str | None
    qty: int
    unit_price: int
    subtotal: int


@dataclass
class ParseResult:
    items: list[Line] = field(default_factory=list)
    total: int = 0
    currency: str = "VND"
    confidence: str = "low"          # high | low
    status: str = "NEEDS_REVIEW"     # READY | NEEDS_REVIEW
    reason: str | None = None


def _review(reason: str, currency: str) -> ParseResult:
    return ParseResult(currency=currency, reason=reason)


def _qty(seg: str) -> int | None:
    m = re.search(r"(?<!\w)x\s*(\d{1,3})(?!\w)|(?<!\w)(\d{1,3})\s*x?(?!\w)", seg)
    if m:
        return int(m.group(1) or m.group(2))
    for w, n in NUM_WORDS.items():
        if re.search(rf"(?<!\w){w}(?!\w)", seg):
            return n
    return None


def parse_comment(text: str | None, catalog: list, currency: str = "VND") -> ParseResult:
    """catalog: iterable of CatalogItem-like (sku,name,aliases,unit_price,colors,sizes,in_stock)."""
    if not text or not text.strip():
        return _review("empty_comment", currency)
    if not catalog:
        return _review("no_catalog", currency)
    t = norm(text)
    if any(_has(w, t) for w in AMBIGUOUS_WORDS):
        return _review("ambiguous_keyword", currency)

    merged: dict[tuple, Line] = {}
    for seg in (s for s in SPLIT_RE.split(t) if s):
        matched = [p for p in catalog
                   if any(_has(a, seg) for a in ([p.name] + list(p.aliases or [])))]
        if not matched:
            if re.search(r"\d", seg):
                return _review("unknown_product", currency)
            continue  # filler like "nhe shop"
        if len(matched) > 1:
            return _review("multiple_products_in_segment", currency)
        p = matched[0]
        if not p.in_stock:
            return _review("out_of_stock", currency)
        qty = _qty(seg)
        if qty is None or qty < 1:
            return _review("missing_quantity", currency)

        color = None
        colors = p.colors or {}
        if colors:
            hits = [k for k, al in colors.items() if any(_has(a, seg) for a in [k] + list(al))]
            if len(hits) > 1:
                return _review("ambiguous_color", currency)
            if hits:
                color = hits[0]
            elif len(colors) == 1:
                color = next(iter(colors))
            else:
                return _review("missing_color", currency)

        size = None
        sizes = p.sizes or []
        if sizes:
            hits = [s for s in sizes if _has(s, seg)]
            if len(hits) > 1:
                return _review("ambiguous_size", currency)
            if hits:
                size = hits[0]
            elif len(sizes) == 1:
                size = sizes[0]
            else:
                return _review("missing_size", currency)

        key = (p.sku, color, size)
        if key in merged:
            ln = merged[key]
            ln.qty += qty
            ln.subtotal = ln.qty * ln.unit_price
        else:
            merged[key] = Line(p.sku, p.name, color, size, qty, p.unit_price, qty * p.unit_price)

    if not merged:
        return _review("no_items", currency)
    items = list(merged.values())
    return ParseResult(items=items, total=sum(i.subtotal for i in items), currency=currency,
                       confidence="high", status="READY")
