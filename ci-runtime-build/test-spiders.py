#!/usr/bin/env python3
"""Local-only SpiderTestSession smoke. Standard library only; no third-party sites.

Main-session examples (this script does not install/launch the Android app):
  python test-spiders.py --serial emulator-5554 --runtime python
  python test-spiders.py --serial emulator-5554 --runtime python --runtime quickjs
  python test-spiders.py --serial emulator-5554 --media /absolute/owned-video.mp4 --send-play --hold
  python test-spiders.py --runtime node  # BLOCKED: /spider-api has no Node loader
  python test-spiders.py --self-test     # host fixtures only; NEVER an Android pass

Normal runs set adb forward tcp:<auto> tcp:9978 and reverse tcp:18765 tcp:18765.
JSON output is stdout; diagnostics are stderr. Exit 0=all requested runtimes passed,
1=failure, 2=blocked/missing runtime. --hold keeps fixtures, sessions and mappings
alive for later local playback until Ctrl-C (Android sessions expire after 15 min).
No /spider-api/play call is made unless --send-play is explicitly given. Sending
play proves dispatch only, NOT MPV playback, rendering, subtitle loading, or audio.
"""
import argparse
import io
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FIXTURES = Path(__file__).resolve().parent / "smoke-fixtures"
NODE_BLOCK = "SpiderTestSession validates .py/.js/csp_ only; .js is always QuickJS. NodeSpider's HTTP route contract is not loadable through this session API."


class SmokeError(RuntimeError):
    pass


class Blocked(SmokeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SmokeError("Redirect refused: " + newurl)


HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def request_json(url, method="GET", body=None, headers=None, timeout=10):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with HTTP.open(request, timeout=timeout) as response:
            if response.headers.get_content_type() != "application/json":
                raise SmokeError("Expected application/json from " + url)
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise SmokeError("HTTP %s: %s" % (error.code, error.read(4096).decode("utf-8", "replace"))) from error


def pcm_wave():
    data = io.BytesIO()
    with wave.open(data, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"".join(struct.pack("<h", int(3500 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(16000)))
    return data.getvalue()


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, media=None):
        super().__init__(("127.0.0.1", port), FixtureHandler)
        self.hits = Counter()
        self.lock = threading.Lock()
        self.media = media
        self.catalog = json.loads((FIXTURES / "catalog.json").read_text(encoding="utf-8"))
        if media:
            self.catalog["media"] = "/media/custom" + media.suffix.lower()
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    def count(self, path):
        with self.lock:
            return self.hits[path]

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=3)


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, pattern, *args):
        pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        routes = {"/python_spider.py": ("text/x-python", FIXTURES / "python_spider.py"),
                  "/quickjs_spider.js": ("text/javascript", FIXTURES / "quickjs_spider.js"),
                  "/subtitles/smoke.srt": ("application/x-subrip", FIXTURES / "subtitles/smoke.srt"),
                  "/subtitles/smoke.ass": ("text/x-ssa", FIXTURES / "subtitles/smoke.ass")}
        if path == "/catalog.json":
            mime, payload = "application/json", json.dumps(self.server.catalog).encode()
        elif path == self.server.catalog["media"]:
            mime = "video/mp4" if path.endswith(".mp4") else "application/octet-stream"
            if self.server.media:
                payload = self.server.media.read_bytes()
            else:
                mime, payload = "audio/wav", pcm_wave()
        elif path in routes:
            mime, file = routes[path]
            payload = file.read_bytes()
        else:
            self.send_error(404)
            return
        with self.server.lock:
            self.server.hits[path] += 1
        start, end, status = 0, len(payload) - 1, 200
        # MPV may seek. Serve only valid single byte ranges; never fetch upstream.
        byte_range = self.headers.get("Range")
        if byte_range:
            try:
                if not byte_range.startswith("bytes=") or "," in byte_range:
                    raise ValueError()
                left, right = byte_range[6:].split("-", 1)
                if not left:
                    suffix = int(right)
                    if suffix <= 0:
                        raise ValueError()
                    start = max(0, len(payload) - suffix)
                else:
                    start = int(left)
                    end = min(end, int(right)) if right else end
                if not 0 <= start <= end < len(payload):
                    raise ValueError()
                status = 206
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % len(payload))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, len(payload)))
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload[start:end + 1])


class Session:
    def __init__(self, origin, timeout):
        self.origin = origin
        self.timeout = timeout
        self.key = None

    def request(self, route="", method="GET", body=None):
        headers = {"Origin": self.origin, "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"}
        if self.key:
            headers["x-spider-session"] = self.key
        return request_json(self.origin + "/spider-api" + route, method, body, headers, self.timeout + 5)

    def create(self):
        reply = self.request(method="POST", body={})
        self.key = reply.get("session")
        if not isinstance(self.key, str) or not self.key:
            raise SmokeError("POST /spider-api did not return session")

    def verify_security(self):
        # Test the real app's rejection paths; never omit Origin or relax checks.
        def rejected(headers, expected, label):
            request = urllib.request.Request(self.origin + "/spider-api", data=b"{}", method="POST", headers=headers)
            try:
                with HTTP.open(request, timeout=10):
                    raise SmokeError("Security regression: accepted " + label)
            except urllib.error.HTTPError as error:
                require(error.code == expected, "%s expected HTTP %d, got %d" % (label, expected, error.code))

        rejected({"Origin": "http://fixture.invalid", "Content-Type": "application/json"}, 403, "cross-origin creation")
        rejected({"Origin": self.origin, "Sec-Fetch-Site": "cross-site", "Content-Type": "application/json"}, 403, "cross-site creation")
        rejected({"Origin": self.origin, "Content-Type": "text/plain"}, 415, "non-JSON creation")
        request = urllib.request.Request(self.origin + "/spider-api", headers={"Origin": self.origin})
        try:
            with HTTP.open(request, timeout=10):
                raise SmokeError("Security regression: accepted missing x-spider-session")
        except urllib.error.HTTPError as error:
            require(error.code == 403, "Missing x-spider-session expected HTTP 403, got %d" % error.code)

    def wait(self, snapshot, expected):
        deadline = time.monotonic() + self.timeout + 5
        while snapshot.get("state") in ("loading", "running"):
            if time.monotonic() >= deadline:
                self.request("/cancel", "POST", {})
                raise SmokeError("Host polling deadline exceeded")
            time.sleep(0.1)
            snapshot = self.request()
        if snapshot.get("state") != expected:
            result = str(snapshot.get("result", ""))
            if "module is not installed" in result:
                raise Blocked(result)
            raise SmokeError("Expected %s, got %s: %s" % (expected, snapshot.get("state"), result))
        return snapshot

    def load(self, source, base):
        snapshot = self.wait(self.request("/load", "POST", {"api": source, "ext": json.dumps({"base": base}), "jar": "", "timeout": self.timeout}), "loaded")
        if snapshot.get("loaded") is not True:
            raise SmokeError("Load completed without loaded=true")
        return snapshot

    def run(self, method, args):
        snapshot = self.wait(self.request("/run", "POST", {"method": method, "args": args, "timeout": self.timeout}), "done")
        try:
            result = json.loads(snapshot["result"])
        except (KeyError, TypeError, ValueError) as error:
            raise SmokeError("Invalid Spider JSON result for " + method) from error
        if not isinstance(result, dict):
            raise SmokeError("Spider result is not an object for " + method)
        return result, snapshot

    def close(self):
        if self.key:
            self.request(method="DELETE")
            self.key = None


def require(condition, message):
    if not condition:
        raise SmokeError(message)


def exercise(session, runtime, base, server, send_play=False):
    source = base + ("/python_spider.py" if runtime == "python" else "/quickjs_spider.js")
    session.verify_security()
    session.create()
    session.load(source, base)
    require(server.count(urllib.parse.urlsplit(source).path) >= 1, "App did not fetch the owned Spider source")
    checks = []

    def call(method, args):
        before = server.count("/catalog.json")
        result, snapshot = session.run(method, args)
        require(server.count("/catalog.json") > before, method + " did not fetch local JSON")
        checks.append({"method": method, "state": snapshot["state"], "elapsed_ms": snapshot.get("elapsed"), "requests": snapshot.get("requests", {})})
        return result, snapshot

    home, _ = call("home", [True])
    require(home.get("class") == server.catalog["class"], "home class mismatch")
    category, _ = call("category", ["local", "1", True, {}])
    require(category.get("page") == 1 and category.get("total") == 1, "category pagination mismatch")
    require(category.get("list") == server.catalog["list"], "category item mismatch")
    item = category["list"][0]
    detail, _ = call("detail", [[item["vod_id"]]])
    require(detail.get("list") == [item], "detail mismatch")
    search, _ = call("search", ["Local runtime", False, "1"])
    require(search.get("list") == [item], "search mismatch")
    # Separate empty search and second-page calls detect hardcoded all-success fixtures.
    empty, _ = call("search", ["does-not-exist", False, "1"])
    require(empty.get("list") == [] and empty.get("total") == 0, "empty search mismatch")
    second, _ = call("search", ["Local runtime", False, "2"])
    require(second.get("list") == [] and second.get("page") == 2, "search page-2 mismatch")
    flag = detail["list"][0]["vod_play_from"]
    episode = detail["list"][0]["vod_play_url"].split("$", 1)[1]
    play, snapshot = call("play", [flag, episode, []])
    require(play.get("parse") == 0 and play.get("url") == base + server.catalog["media"], "play URL/parse mismatch")
    require(len(play.get("subtitles", [])) == 2, "Missing fixture subtitles")
    report = {"runtime": runtime, "status": "pass", "scope": "real app SpiderTestSession calls + observed local JSON requests; NOT playback", "checks": checks, "play": play}
    if send_play:
        session.request("/play", "POST", {"result": snapshot["result"]})
        report["play_dispatch"] = "sent; playback is unverified"
    return report


def adb_command(args, *parts):
    command = [args.adb]
    if args.serial:
        command += ["-s", args.serial]
    try:
        result = subprocess.run(command + list(parts), capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Blocked("adb unavailable: " + str(error)) from error
    if result.returncode:
        raise Blocked("adb %s: %s" % (" ".join(parts), result.stderr.strip()))
    return result.stdout.strip()


def self_test():
    """Host-only integration of fixtures plus API wire shape, not an app/runtime test."""
    import importlib.util
    import types
    import unittest.mock
    server = FixtureServer(0).start()
    try:
        base = "http://127.0.0.1:%d" % server.server_port
        catalog = request_json(base + "/catalog.json")
        require(catalog["fixture"] == "tv-runtime-local-v1", "catalog marker")
        for path, signature in [(catalog["media"], b"RIFF"), ("/subtitles/smoke.srt", b"1\n"), ("/subtitles/smoke.ass", b"[Script Info]")]:
            with HTTP.open(base + path) as response:
                require(response.read().startswith(signature), "fixture payload " + path)
        with HTTP.open(urllib.request.Request(base + catalog["media"], headers={"Range": "bytes=0-3"})) as response:
            require(response.status == 206 and response.read() == b"RIFF", "media byte range")
        try:
            HTTP.open(base + "/../../LICENSE.md")
            raise SmokeError("Fixture server exposed nonfixture file")
        except urllib.error.HTTPError as error:
            require(error.code == 404, "Nonfixture route should 404")
        fake_base = types.ModuleType("base.spider")
        fake_base.Spider = object
        with unittest.mock.patch.dict(sys.modules, {"base": types.ModuleType("base"), "base.spider": fake_base}):
            spec = importlib.util.spec_from_file_location("owned_smoke_spider", FIXTURES / "python_spider.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        spider = module.Spider()

        class HostNet:
            def req(self, url, options):
                require(url == base + "/catalog.json", "Python attempted a nonfixture URL")
                return {"code": 200, "content": json.dumps(request_json(url))}

        spider.net = HostNet()
        spider.init(json.dumps({"base": base}))
        require(spider.homeContent(True)["class"] == catalog["class"], "Python home")
        require(spider.categoryContent("local", "1", True, {})["total"] == 1, "Python category")
        require(spider.detailContent(["local-1"])["list"] == catalog["list"], "Python detail")
        require(len(spider.searchContent("Local", False)["list"]) == 1, "Python search")
        require(spider.searchContent("missing", False)["list"] == [], "Python negative search")
        require(spider.searchContent("Local", False, "2")["list"] == [], "Python paged search")
        require(spider.playerContent("local", "local-1", [])["url"] == base + catalog["media"], "Python play")
        try:
            spider.init('{"base":"http://example.org"}')
            raise SmokeError("Python accepted a third-party base")
        except ValueError:
            pass
        calls = []

        def wire(url, method="GET", body=None, headers=None, timeout=10):
            calls.append((url, method, body, headers))
            return {"session": "local-self-test"} if method == "POST" else {}

        with unittest.mock.patch.dict(globals(), {"request_json": wire}):
            session = Session(base, 5)
            session.create()
            session.request("/run", "POST", {"method": "home", "args": [True], "timeout": 5})
            session.close()
        require(calls[0][0] == base + "/spider-api" and calls[0][2] == {}, "Session creation contract")
        require(calls[1][3]["x-spider-session"] == "local-self-test", "Session header")
        require(calls[1][3]["Origin"] == base and calls[1][3]["Content-Type"] == "application/json", "Origin/content-type contract")
        require(calls[-1][1] == "DELETE", "Session cleanup")
        waiting = Session(base, 5)
        try:
            waiting.wait({"state": "error", "result": "Python module is not installed"}, "loaded")
            raise SmokeError("Missing runtime was not blocked")
        except Blocked:
            pass
        try:
            waiting.wait({"state": "timedout", "result": "Crawler request timed out"}, "done")
            raise AssertionError("Timed out runtime was accepted")
        except SmokeError:
            pass
        print(json.dumps({"status": "host-self-test-ok", "android_runtime_tested": False, "note": "Python uses a stub base class and host net adapter here; no Android, QuickJS or Node runtime pass claimed"}))
    finally:
        server.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--serial", default=os.environ.get("ANDROID_SERIAL"))
    parser.add_argument("--app-port", type=int, default=9978)
    parser.add_argument("--fixture-port", type=int, default=18765)
    parser.add_argument("--runtime", action="append", choices=("python", "quickjs", "node"))
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--media", type=Path, help="Owned local media file, optionally MP4; served only over loopback")
    parser.add_argument("--send-play", action="store_true")
    parser.add_argument("--hold", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if not 1 <= args.timeout <= 300 or not 1 <= args.fixture_port <= 65535 or not 1 <= args.app_port <= 65535:
        parser.error("timeout must be 1..300 and ports 1..65535")
    if args.send_play and not args.hold:
        parser.error("--send-play requires --hold so playback fixtures/session remain alive")
    if args.media and (not args.media.is_file() or args.media.suffix.lower() not in (".mp4", ".mkv", ".wav", ".webm", ".ts", ".mp3")):
        parser.error("--media must be an existing owned media file (.mp4/.mkv/.wav/.webm/.ts/.mp3)")
    runtimes = list(dict.fromkeys(args.runtime or ["python", "quickjs"]))
    reports = []
    sessions, server, forward, reverse = [], None, None, False
    try:
        active = [runtime for runtime in runtimes if runtime != "node"]
        if active:
            server = FixtureServer(args.fixture_port, args.media).start()
            reverse_name = "tcp:%d" % args.fixture_port
            existing = adb_command(args, "reverse", "--list")
            if any(reverse_name == token for line in existing.splitlines() for token in line.split()[1:]):
                raise Blocked("Refusing to overwrite existing adb reverse " + reverse_name)
            adb_command(args, "reverse", "--no-rebind", reverse_name, reverse_name)
            reverse = True
            forward = adb_command(args, "forward", "tcp:0", "tcp:%d" % args.app_port)
            if not forward.isdigit():
                raise Blocked("adb forward tcp:0 did not return an allocated host port")
            origin = "http://127.0.0.1:" + forward
            base = "http://127.0.0.1:%d" % args.fixture_port
        for runtime in runtimes:
            try:
                if runtime == "node":
                    raise Blocked(NODE_BLOCK)
                session = Session(origin, args.timeout)
                sessions.append(session)
                reports.append(exercise(session, runtime, base, server, args.send_play))
            except Blocked as error:
                reports.append({"runtime": runtime, "status": "blocked", "error": str(error)})
            except Exception as error:
                reports.append({"runtime": runtime, "status": "fail", "error": str(error)})
        print(json.dumps({"results": reports, "playback_verified": False}, ensure_ascii=False, indent=2), flush=True)
        if args.hold and server:
            print("Holding local fixture server and sessions until Ctrl-C; /spider-api sessions expire after 15 min.", file=sys.stderr, flush=True)
            for session in sessions:
                if session.key:
                    print(json.dumps({"origin": session.origin, "x-spider-session": session.key}), file=sys.stderr, flush=True)
            while True:
                time.sleep(1)
    except Blocked as error:
        print(json.dumps({"status": "blocked", "error": str(error)}))
        return 2
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(json.dumps({"status": "fail", "error": str(error)}))
        return 1
    finally:
        for session in sessions:
            try:
                session.close()
            except Exception as error:
                print("Session cleanup failed: " + str(error), file=sys.stderr)
        if forward and forward.isdigit():
            try:
                adb_command(args, "forward", "--remove", "tcp:" + forward)
            except SmokeError as error:
                print(str(error), file=sys.stderr)
        if reverse:
            try:
                adb_command(args, "reverse", "--remove", "tcp:%d" % args.fixture_port)
            except SmokeError as error:
                print(str(error), file=sys.stderr)
        if server:
            server.stop()
    return 1 if any(report["status"] == "fail" for report in reports) else 2 if any(report["status"] == "blocked" for report in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
