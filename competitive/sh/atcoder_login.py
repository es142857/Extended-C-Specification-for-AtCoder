#!/usr/bin/env python3
import http.cookiejar
import json
import subprocess
import time
from pathlib import Path

import requests
import websocket

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROFILE = r"C:\Users\soyuz\AppData\Local\EcppAtCoderLogin"
POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
COOKIE_JAR = Path.home() / ".local/share/online-judge-tools/cookie.jar"
PORT = 19227


def cdp(ws, method: str, params: dict | None = None, request_id: int = 1):
    ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
    while True:
        message = json.loads(ws.recv())
        if message.get("id") == request_id:
            if "error" in message:
                raise RuntimeError(message["error"])
            return message.get("result", {})


def open_chrome() -> None:
    command = (
        f"Start-Process -FilePath '{CHROME}' -ArgumentList "
        f"@('--remote-debugging-port={PORT}',"
        "'--remote-allow-origins=*',"
        f"'--user-data-dir={PROFILE}',"
        "'https://atcoder.jp/login')"
    )
    subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def debugger_url() -> str | None:
    hosts = ["127.0.0.1"]
    try:
        for line in Path("/etc/resolv.conf").read_text().splitlines():
            if line.startswith("nameserver "):
                hosts.append(line.split()[1])
                break
    except OSError:
        pass
    for host in hosts:
        try:
            pages = requests.get(f"http://{host}:{PORT}/json", timeout=1).json()
        except (requests.RequestException, ValueError):
            continue
        selected = next(
            (page for page in pages if page.get("type") == "page"
             and "atcoder.jp" in page.get("url", "")),
            pages[0] if pages else None,
        )
        if selected:
            return selected.get("webSocketDebuggerUrl", "").replace(
                "127.0.0.1", host
            ).replace("localhost", host)
    return None


def logged_in(cookies: list[dict]) -> bool:
    session = requests.Session()
    for cookie in cookies:
        if cookie.get("domain", "").lstrip(".").endswith("atcoder.jp"):
            session.cookies.set(
                cookie["name"], cookie["value"],
                domain=cookie.get("domain"), path=cookie.get("path", "/"),
            )
    try:
        response = session.get("https://atcoder.jp/settings", timeout=5)
    except requests.RequestException:
        return False
    return response.url.rstrip("/").endswith("/settings")


def save_cookies(cookies: list[dict]) -> None:
    COOKIE_JAR.parent.mkdir(parents=True, exist_ok=True)
    jar = http.cookiejar.MozillaCookieJar(COOKIE_JAR)
    if COOKIE_JAR.exists():
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except http.cookiejar.LoadError:
            jar.clear()
    for domain in ("atcoder.jp", ".atcoder.jp"):
        try:
            jar.clear(domain)
        except KeyError:
            pass
    for item in cookies:
        domain = item.get("domain", "")
        if not domain.lstrip(".").endswith("atcoder.jp"):
            continue
        expires = item.get("expires", -1)
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=item["name"], value=item["value"], port=None,
            port_specified=False, domain=domain,
            domain_specified=bool(domain), domain_initial_dot=domain.startswith("."),
            path=item.get("path", "/"), path_specified=True,
            secure=bool(item.get("secure")), expires=int(expires) if expires > 0 else None,
            discard=expires <= 0, comment=None, comment_url=None, rest={}, rfc2109=False,
        ))
    jar.save(ignore_discard=True, ignore_expires=True)


def main() -> int:
    open_chrome()
    print("ChromeでAtCoderにログインしてください。CAPTCHA完了後は自動で検出します。")
    url = None
    for _ in range(300):
        url = debugger_url()
        if url:
            try:
                ws = websocket.create_connection(url, timeout=3, origin="http://localhost")
                cookies = cdp(ws, "Storage.getCookies").get("cookies", [])
                if logged_in(cookies):
                    save_cookies(cookies)
                    cdp(ws, "Browser.close", request_id=2)
                    print("AtCoderのログイン情報をojへ保存しました。")
                    return 0
                ws.close()
            except (OSError, RuntimeError, websocket.WebSocketException):
                pass
        time.sleep(1)
    print("ログインを確認できませんでした。もう一度 atcoder-login を実行してください。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
