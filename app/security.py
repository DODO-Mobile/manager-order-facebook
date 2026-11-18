import hashlib
import hmac


def verify_signature(raw_body: bytes, header: str | None, app_secret: str) -> bool:
    """X-Hub-Signature-256 = 'sha256=' + HMAC-SHA256(raw_body, app_secret). Timing-safe."""
    if not header or not app_secret or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


def mask(value: str | None, keep: int = 3) -> str:
    if not value:
        return ""
    return value[:keep] + "***" if len(value) > keep else "***"
