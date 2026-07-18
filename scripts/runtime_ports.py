"""Port selection for isolated personal and distributable Shiguang editions."""
import json
import socket
import urllib.request
from http.server import ThreadingHTTPServer


DEFAULT_PORT = 8756


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """Prevent two local app backends from sharing one Windows port."""
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
            )
        super().server_bind()


def candidate_ports(requested_port=DEFAULT_PORT, *, consumer=False, edition="personal"):
    requested_port = int(requested_port)
    if requested_port != DEFAULT_PORT:
        return [requested_port]
    if not consumer:
        return [8756, 12756]
    suffix = 7 if str(edition).strip().lower() == "couple" else 6
    return [18000 + 750 + suffix, 28000 + 750 + suffix, 38000 + 750 + suffix]


def same_product_running(port, expected_id, urlopen=urllib.request.urlopen,
                         expected_build_id=""):
    try:
        with urlopen(f"http://127.0.0.1:{int(port)}/api/product", timeout=1.5) as response:
            product = json.loads(response.read().decode("utf-8"))
        same_product = str(product.get("id") or "") == str(expected_id or "")
        if expected_build_id:
            return same_product and str(product.get("build_id") or "") == str(expected_build_id)
        return same_product
    except Exception:
        return False
