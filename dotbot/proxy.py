"""Explicit HTTP CONNECT proxy support; never persist or display proxy credentials."""
import os
from urllib.parse import unquote, urlsplit

from aiohttp import BasicAuth


def from_environment():
    value = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not value:
        raise ValueError("--proxy-from-env requires HTTPS_PROXY or https_proxy.")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "http" or not parsed.hostname or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or any(c.isspace() for c in value)):
            raise ValueError()
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        if port is not None:
            host += f":{port}"
        auth = None
        if parsed.username is not None:
            auth = BasicAuth(unquote(parsed.username), unquote(parsed.password or ""))
            auth.encode()  # Validate encoding without logging credentials.
        return {"proxy": f"http://{host}", "proxy_auth": auth}
    except (ValueError, UnicodeError):
        raise ValueError("Proxy must be a valid http:// host URL, optionally with Basic Auth; HTTPS and SOCKS proxies are unsupported.") from None
