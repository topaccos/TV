#!/usr/bin/env python3
"""Prepare a real Android NodeBundle or verify host HTTP receipts against Android PID.

No adb, remote access, external Node process, host edits, or new HTTP API.
1. prepare --base <Android-visible fixture base> --output <HTTP-root>/node-<unique>
2. Use the EXISTING configuration-load path with <base>/node-<unique>/index.js.md5.
   It must be loaded as a VOD config, NOT /spider-api load, a JS script, or a ZIP.
3. Select node_local in the app; exercise home/category/detail/search/play.
4. Save the fixture server's access log and get node PID/version from Android
   runtime /info using the main runner's existing endpoint contract.
5. verify --access-log <log> --nonce <prepare nonce> --android-pid <pid>
   --android-version <version> --android-process-confirmed

Verification requires config + init + each requested route receipt carrying the
same nonce and independently confirmed Android PID/version. It verifies the
NodeSpider chain, NOT successful media playback (MPV is a separate test).
"""
import argparse
import json
from pathlib import Path
import re
import runpy
import sys
from urllib.parse import parse_qs, urlsplit


def verify(args):
    if not args.android_process_confirmed:
        raise ValueError("Android PID/version must be independently confirmed; no standalone Node pass")
    if args.android_pid <= 0 or not re.fullmatch(r"v\d+\.\d+\.\d+(?:[-+].*)?", args.android_version):
        raise ValueError("invalid Android Node PID/version")
    required = {"config", "init"} | set(args.methods.split(","))
    allowed = {"config", "init", "home", "homeVod", "category", "detail", "search", "play"}
    if not required <= allowed:
        raise ValueError("unknown method(s): " + ",".join(sorted(required - allowed)))
    seen = set()
    # Supports ordinary BaseHTTPRequestHandler/Nginx HTTP request access logs.
    # Only successful catalog requests with exact nonce/PID/version count.
    pattern = re.compile(r'"GET\s+(\S+)\s+HTTP/[0-9.]+"\s+200(?:\s|$)')
    for line in args.access_log.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.search(line)
        if not match:
            continue
        parsed = urlsplit(match.group(1))
        if not parsed.path.endswith("/catalog.json"):
            continue
        query = parse_qs(parsed.query)
        if (query.get("node_probe") == [args.nonce]
                and query.get("pid") == [str(args.android_pid)]
                and query.get("version") == [args.android_version]
                and len(query.get("method", [])) == 1):
            seen.add(query["method"][0])
    missing = required - seen
    return {"status": "missing-android-receipts" if missing else "android-node-spider-chain-pass",
            "android_runtime_tested": not missing, "playback_tested": False,
            "nonce": args.nonce, "android_pid": args.android_pid,
            "android_version": args.android_version, "methods": sorted(seen),
            "missing": sorted(missing), "proof_source": "fixture HTTP access log + caller-confirmed Android runtime PID/version"}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="generate four-file bundle under existing HTTP root")
    prepare.add_argument("--base", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--nonce")
    check = sub.add_parser("verify", help="offline verification; does not contact device")
    check.add_argument("--access-log", type=Path, required=True)
    check.add_argument("--nonce", required=True)
    check.add_argument("--android-pid", type=int, required=True)
    check.add_argument("--android-version", required=True)
    check.add_argument("--android-process-confirmed", action="store_true")
    check.add_argument("--methods", default="home,category,detail,search,play")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            generator = runpy.run_path(str(Path(__file__).resolve().parent / "test-node-assets" / "make-bundle.py"))
            result = generator["generate"](args.base, args.output, args.nonce)
            result["next"] = "Load the served index.js.md5 via the existing VOD-config path; select node_local; this preparation is NOT an Android pass"
        else:
            result = verify(args)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get("missing") else 0
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "error", "error": str(error), "android_runtime_tested": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
