"""Find printers on the local network. Read-only: mDNS browse, SSDP listen, TCP connect, GET probes."""

from __future__ import annotations

import concurrent.futures
import ipaddress
import socket
import time

from .adapters import ADAPTERS

MDNS_TYPES = {"_moonraker._tcp.local.": "moonraker", "_octoprint._tcp.local.": "octoprint",
              "_prusa-link._tcp.local.": "prusalink"}
# (port, adapter types to try there)
KNOWN_PORTS = [(7125, ["moonraker"]), (4408, ["moonraker"]), (9999, ["creality"]), (8883, ["bambu"]),
               (80, ["moonraker", "prusalink", "octoprint"]), (5000, ["octoprint"])]


def local_subnet() -> ipaddress.IPv4Network | None:
    """The /24 of the address used for the default route."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET address; no packet is sent for UDP connect
            ip = s.getsockname()[0]
    except OSError:
        return None
    return ipaddress.ip_network(f"{ip}/24", strict=False)


def mdns(seconds: float = 3.0) -> list[dict]:
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        return []
    found: list[dict] = []
    zc = Zeroconf()

    class Listener:
        def add_service(self, zc_, type_, name):
            info = zc_.get_service_info(type_, name, timeout=1500)
            if info and info.parsed_addresses():
                found.append({"type": MDNS_TYPES[type_], "host": info.parsed_addresses()[0], "port": info.port,
                              "detail": f"mDNS {name}", "via": "mdns"})

        def update_service(self, *a):
            pass

        def remove_service(self, *a):
            pass

    try:
        ServiceBrowser(zc, list(MDNS_TYPES), Listener())
        time.sleep(seconds)
    finally:
        zc.close()
    return found


def bambu_ssdp(seconds: float = 3.0) -> list[dict]:
    """Bambu printers announce themselves by SSDP NOTIFY on UDP 2021. Listen only."""
    out: dict[str, dict] = {}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", 2021))
    except OSError:
        return []
    s.settimeout(0.5)
    end = time.monotonic() + seconds
    with s:
        while time.monotonic() < end:
            try:
                data, addr = s.recvfrom(4096)
            except (TimeoutError, OSError):
                continue
            text = data.decode("utf-8", "replace")
            if "bambu" not in text.lower():
                continue
            hdr = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in text.splitlines()) if v}
            out[addr[0]] = {"type": "bambu", "host": addr[0], "port": 8883, "via": "ssdp",
                            "serial": hdr.get("usn", ""), "model_code": hdr.get("devmodel.bambu.com", ""),
                            "detail": f"Bambu Lab {hdr.get('devname.bambu.com', '')}".strip()}
    return list(out.values())


def _open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_host(host: str, timeout: float = 1.0, ports=None) -> list[dict]:
    hits = []
    for port, types in ports or KNOWN_PORTS:
        if not _open(host, port, timeout):
            continue
        for t in types:
            info = ADAPTERS[t].probe(host, port, timeout=max(timeout, 2.0))
            if info:
                info["via"] = "scan"
                hits.append(info)
                break
    return hits


def scan(hosts: list[str], timeout: float = 0.5, workers: int = 64) -> list[dict]:
    out: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        for hits in pool.map(lambda h: probe_host(h, timeout), hosts):
            out.extend(hits)
    return out


def discover(subnet: str | None = None, hosts: list[str] | None = None, seconds: float = 3.0,
             use_mdns: bool = True) -> list[dict]:
    results: list[dict] = []
    if use_mdns and not hosts:
        results += mdns(seconds)
        results += bambu_ssdp(seconds)
    if hosts is None:
        net = ipaddress.ip_network(subnet, strict=False) if subnet else local_subnet()
        if net is None:
            return results
        if net.num_addresses > 1024:
            raise ValueError("scan at most a /22 at a time")
        hosts = [str(h) for h in net.hosts()]
    results += scan(hosts)
    seen, unique = set(), []
    for r in results:
        key = (r["host"], r["type"])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique
