import base64
import hashlib
import hmac
import json
import time


def sign(data, key):
    body = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":"), sort_keys=True).encode()).decode().rstrip("=")
    signature = hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()
    return body + "." + signature


def unsign(token, key):
    if not isinstance(token, str) or len(token) > 4096:
        raise ValueError("Некорректный token")
    try:
        body, signature = token.rsplit(".", 1)
        if not hmac.compare_digest(hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest(), signature):
            raise ValueError
        result = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if not isinstance(result, dict) or result.get("expires", time.time() + 1) < time.time():
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("Некорректный или истёкший token") from None
