# WiZ bulb over local UDP. Firmware 1.38+ needs "allow all controls" in the WiZ app, or WIZ_SIGNING_KEY.
import base64, hashlib, hmac, json, os, re, socket, time

PORT = 38899
SIGNING_KEY = bytes.fromhex(os.environ["WIZ_SIGNING_KEY"]) if os.environ.get("WIZ_SIGNING_KEY") else None
_bulb = {"ip": None, "checked": 0.0}


def sign(payload: dict, key: bytes, timestamp: int | None = None) -> dict:
    params = dict(payload.get("params", {}))
    params["sigTs"] = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(key, json.dumps(params, separators=(",", ":")).encode(), hashlib.sha256).digest()
    return {"method": payload["method"], "env": "pro", "params": params,
            "hmac": base64.b64encode(digest).decode()}


def _send(ip: str, payload: dict, timeout: float = 0.6) -> dict | None:
    if SIGNING_KEY and payload["method"].startswith("set"):
        payload = sign(payload, SIGNING_KEY)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        try:
            s.sendto(json.dumps(payload, separators=(",", ":")).encode(), (ip, PORT))
            return json.loads(s.recvfrom(2048)[0])
        except (OSError, ValueError):
            return None


def find() -> str | None:
    if _bulb["ip"] and _send(_bulb["ip"], {"method": "getPilot", "params": {}}):
        return _bulb["ip"]
    if time.time() - _bulb["checked"] < 30:
        return None
    _bulb["ip"], _bulb["checked"] = None, time.time()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            base = probe.getsockname()[0].rsplit(".", 1)[0]
    except OSError:
        return None
    msg = json.dumps({"method": "getPilot", "params": {}}).encode()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(0.5)
        for _ in range(2):
            for i in range(1, 255):
                try:
                    s.sendto(msg, (f"{base}.{i}", PORT))
                except OSError:
                    pass
            try:
                data, addr = s.recvfrom(2048)
                if b"getPilot" in data:
                    _bulb["ip"] = addr[0]
                    print("Smart light found at", addr[0])
                    return addr[0]
            except OSError:
                pass
    return None


def state() -> str:
    ip = find()
    r = (_send(ip, {"method": "getPilot", "params": {}}) or {}).get("result") if ip else None
    if not r:
        return ""
    if not r.get("state"):
        return "The light is currently off."
    return f"The light is currently on at {r.get('dimming', '?')}% brightness."


def apply(light: dict) -> bool:
    ip = find()
    if not ip:
        return False
    params: dict = {"state": bool(light.get("on", True))}
    if params["state"]:
        if isinstance(light.get("brightness"), int):
            params["dimming"] = max(10, min(100, light["brightness"]))
        m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(light.get("color") or ""))
        if m:
            h = m.group(1)
            params.update(r=int(h[0:2], 16), g=int(h[2:4], 16), b=int(h[4:6], 16))
        elif isinstance(light.get("kelvin"), int) and light["kelvin"] > 0:
            params["temp"] = max(2200, min(6500, light["kelvin"]))
    r = _send(ip, {"method": "setPilot", "params": params})
    ok = bool(r and r.get("result", {}).get("success"))
    print("Light:", params, "->", "ok" if ok else (r or {}).get("error", "no answer"))
    return ok
