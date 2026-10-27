#!/usr/bin/env python3
"""Static Linux-runner checks, NOT device/runtime acceptance or a release catalog.

Usage: python3 ci-runtime-build/verify-suite.py ci-suite --repo . \
    --version-code 100123 --version-name 0.1.123-test
Uses the existing runtime_catalog readers without publishing or relaxing checks.
"""
import argparse
import csv
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
from zipfile import ZipFile

import bdj_assets

ABIS = ("arm64-v8a", "armeabi-v7a")
CORE = ("host", "mpv", "python")
MODULES = ("mpv", "python", "quickjs", "nodejs", "thunder", "bdj")
HOST_FAMILIES = ("leanback", "mobile")
STATUS_FIELDS = ("component", "family", "abi", "status", "exitCode", "log")


def requested_matrix(families, modules):
    return [("host", family, abi) for family in families for abi in ABIS] + [
        (module, "-", abi) for module in modules for abi in ABIS]


def read_statuses(file, requested):
    """Reject ambiguous/stale records, while retaining missing requested cells as failures."""
    with file.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if reader.fieldnames != list(STATUS_FIELDS):
            raise ValueError(f"Invalid build status header; expected {list(STATUS_FIELDS)}")
        statuses = {}
        for row in reader:
            if set(row) != set(STATUS_FIELDS) or any(value is None or not value.strip() for value in row.values()):
                raise ValueError("Malformed build status row")
            key = (row["component"], row["family"], row["abi"])
            if (key[0] not in ("host", *MODULES) or key[2] not in ABIS
                    or key[1] not in (HOST_FAMILIES if key[0] == "host" else ("-",))):
                raise ValueError(f"Unknown build status: {key}")
            if key in statuses:
                raise ValueError(f"Duplicate build status: {key}")
            if key not in requested:
                raise ValueError(f"Unrequested build status: {key}")
            if (row["status"] not in ("built", "failed")
                    or not re.fullmatch(r"0|[1-9][0-9]*", row["exitCode"])
                    or (row["status"] == "built") != (row["exitCode"] == "0")):
                raise ValueError(f"Invalid build status/exitCode: {key}")
            statuses[key] = row
    return statuses


def verify_host_family(file, family, aapt2, log):
    """Read compiled XML, not filenames, source manifests or badging heuristics."""
    if family not in HOST_FAMILIES:
        raise ValueError(f"{file.name}: unknown host family {family}")
    text = command([aapt2, "dump", "xmltree", str(file), "--file", "AndroidManifest.xml"])
    log.write_text(text, encoding="utf-8")
    stack, filters = [], []
    for line in text.splitlines():
        match = re.match(r"^(\s*)E: ([\w.-]+)(?:\s|$)", line)
        if match:
            indent, name = len(match[1]), match[2]
            while stack and stack[-1][0] >= indent:
                stack.pop()
            node = {"name": name, "attributes": {}, "children": []}
            if stack:
                stack[-1][1]["children"].append(node)
            if name == "intent-filter" and [item[1]["name"] for item in stack] in (
                    ["manifest", "application", "activity"],
                    ["manifest", "application", "activity-alias"]):
                filters.append(node)
            stack.append((indent, node))
        else:
            attribute = re.match(r'^\s*A: (?:android|http://schemas\.android\.com/apk/res/android):name(?:\(0x[0-9a-fA-F]+\))?="([^"]*)"(?:\s|$)', line)
            if attribute and stack:
                if "android:name" in stack[-1][1]["attributes"]:
                    raise ValueError(f"{file.name}: duplicate manifest android:name")
                stack[-1][1]["attributes"]["android:name"] = attribute[1]
    categories = set()
    for intent in filters:
        actions = {child["attributes"].get("android:name") for child in intent["children"] if child["name"] == "action"}
        if "android.intent.action.MAIN" in actions:
            categories.update(child["attributes"].get("android:name") for child in intent["children"] if child["name"] == "category")
    leanback = "android.intent.category.LEANBACK_LAUNCHER" in categories
    launcher = "android.intent.category.LAUNCHER" in categories
    if (family == "leanback" and not leanback) or (family == "mobile" and (not launcher or leanback)):
        raise ValueError(f"{file.name}: compiled MAIN launcher categories do not prove {family} family")
    return {"source": "AndroidManifest.xml", "mainCategories": sorted(value for value in categories if value)}


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tool(name, repo, explicit):
    if explicit:
        return str(Path(explicit).resolve()) if Path(explicit).is_file() else explicit
    versions = tomllib.loads((repo / "gradle/libs.versions.toml").read_text(encoding="utf-8"))["versions"]
    for sdk in (os.environ.get("ANDROID_SDK_ROOT"), os.environ.get("ANDROID_HOME")):
        if sdk:
            candidate = Path(sdk) / "build-tools" / versions["buildTools"] / name
            if candidate.is_file():
                return str(candidate)
    found = shutil.which(name)
    if not found:
        raise ValueError(f"Cannot find {name}; set ANDROID_HOME/ANDROID_SDK_ROOT or --{name}")
    return found


def command(argv):
    result = subprocess.run(argv, text=True, encoding="utf-8", errors="replace", capture_output=True)
    if result.returncode:
        raise ValueError(f"{Path(argv[0]).name} failed ({result.returncode}): {result.stdout}\n{result.stderr}")
    return result.stdout + result.stderr


def signature(file, apksigner, log):
    text = command([apksigner, "verify", "--verbose", "--print-certs", str(file)])
    log.write_text(text, encoding="utf-8")
    certificates = re.findall(r"^(?:Signer #[0-9]+|V[0-9.]+ Signer):? certificate SHA-256 digest: ([0-9a-fA-F:]+)\s*$", text, re.M)
    values = sorted({value.lower().replace(":", "") for value in certificates})
    if len(values) != 1 or not re.fullmatch("[0-9a-f]{64}", values[0]):
        raise ValueError(f"{file.name}: expected exactly one signing certificate")
    return values[0]


def badging(file, aapt2, log):
    text = command([aapt2, "dump", "badging", str(file)])
    log.write_text(text, encoding="utf-8")
    lines = text.splitlines()
    fields = dict(re.findall(r"(\w+)='([^']*)'", next((s for s in lines if s.startswith("package:")), "")))
    native = re.findall(r"'([^']*)'", next((s for s in lines if s.startswith("native-code:")), ""))
    return fields, native


def verify_zip(file, abi):
    with ZipFile(file) as apk:
        names = apk.namelist()
        if len(names) != len(set(names)) or apk.testzip() is not None:
            raise ValueError(f"{file.name}: duplicate ZIP entries or CRC failure")
        for name in names:
            if "\\" in name or "\x00" in name or name.startswith("/") or any(part in (".", "..") for part in name.split("/")):
                raise ValueError(f"{file.name}: unsafe ZIP path {name}")
            if name.lower().endswith((".jks", ".keystore", ".p12", ".pfx", ".pem", ".key")) and not name.startswith("assets/chaquopy/"):
                raise ValueError(f"{file.name}: unexpected key/certificate file {name}")
            if name.rsplit("/", 1)[-1] == "local.properties":
                raise ValueError(f"{file.name}: local.properties must not be packaged")
            if name.startswith("lib/") and not name.endswith("/") and name.split("/")[1] != abi:
                raise ValueError(f"{file.name}: foreign ABI payload {name}")


def load_catalog(repo):
    spec = importlib.util.spec_from_file_location("suite_runtime_catalog", repo / "scripts/runtime_catalog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_artifact(file, component, abi, args, catalog, tools, logs, family="-"):
    if not file.is_file() or file.is_symlink() or file.stat().st_size == 0:
        raise ValueError(f"Missing/empty/non-regular APK: {file}")
    verify_zip(file, abi)
    certificate = signature(file, tools["apksigner"], logs / f"{file.stem}-signature.log")
    fields, native = badging(file, tools["aapt2"], logs / f"{file.stem}-badging.log")
    package = "com.fongmi.android.tv" if component == "host" else "com.fongmi.runtime." + component
    if fields.get("name") != package or fields.get("versionCode") != str(args.version_code) or fields.get("versionName") != args.version_name:
        raise ValueError(f"{file.name}: unexpected package/version: {fields}")
    if native != ([] if component == "bdj" else [abi]):
        raise ValueError(f"{file.name}: unexpected manifest ABI: {native}")
    artifact = {"path": file.relative_to(args.directory).as_posix(), "packageName": package,
                "abi": abi, "versionCode": args.version_code, "versionName": args.version_name,
                "size": file.stat().st_size, "sha256": sha256(file), "signerSha256": certificate}
    with ZipFile(file) as apk:
        if component == "host":
            artifact["family"] = family
            artifact["familyProof"] = verify_host_family(file, family, tools["aapt2"], logs / f"{file.stem}-manifest.log")
            required = f"lib/{abi}/libc++_shared.so"
            if required not in apk.namelist() or "classes.dex" not in apk.namelist():
                raise ValueError(f"{file.name}: missing host DEX/libc++ payload")
            forbidden = {"libmpv.so", "libplayer.so", "libnode.so", "libquickjs-wrapper.so", "libxl_stat.so", "libxl_thunder_sdk.so"}
            libraries = [p for p in apk.namelist() if p.startswith("lib/") and p.endswith(".so")]
            if any(p.rsplit("/", 1)[-1] in forbidden for p in libraries):
                raise ValueError(f"{file.name}: dynamic runtime leaked into host")
            payloads = []
            for path in libraries:
                with apk.open(path) as stream:
                    catalog.verify_elf(file, stream, abi)
                    stream.seek(0)
                    payloads.append({"path": path, "size": apk.getinfo(path).file_size, "sha256": catalog.digest(stream)})
            artifact["payloads"] = payloads
            artifact["bdjClasspath"] = bdj_assets.verify_assets(apk.read)
        else:
            # Retain all current upstream schema, required-file, ELF and digest checks.
            validated = catalog.read_apk(file, component, abi, tools["aapt2"])
            metadata = catalog.read_runtime_metadata(file, apk, component, abi)
            artifact["minSdk"] = validated["artifact"]["minSdk"]
            artifact["runtimeMetadata"] = metadata
            artifact["payloads"] = metadata["files"]
            expected = {f"lib/{abi}/{name}" for name in catalog.LIBRARIES.get(component, ())}
            actual = {p for p in apk.namelist() if p.startswith("lib/") and not p.endswith("/")}
            if actual != expected:
                raise ValueError(f"{file.name}: unexpected native payload set: {sorted(actual ^ expected)}")
    return artifact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--version-code", type=int, required=True)
    parser.add_argument("--version-name", required=True)
    parser.add_argument("--apksigner")
    parser.add_argument("--aapt2")
    parser.add_argument("--host-families", nargs="+", choices=HOST_FAMILIES, default=["leanback"])
    parser.add_argument("--modules", nargs="+", choices=MODULES, default=list(MODULES))
    args = parser.parse_args()
    if len(args.host_families) != len(set(args.host_families)) or len(args.modules) != len(set(args.modules)):
        parser.error("Duplicate host families or modules are not allowed")
    if not {"mpv", "python"}.issubset(args.modules):
        parser.error("--modules must include the core runtimes mpv and python")
    args.directory, args.repo = args.directory.resolve(), args.repo.resolve()
    logs = args.directory / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    matrix = requested_matrix(args.host_families, args.modules)
    required_core = 2 * len(args.host_families) + 4
    manifest = {"schema": 1, "kind": "ci-suite-static-verification", "status": "failed", "gatePassed": False,
                "requestedHostFamilies": args.host_families, "requestedModules": args.modules,
                "requestedSuitePassed": False,
                "coreGate": {"required": required_core, "verified": 0, "passed": False},
                "versionCode": args.version_code, "versionName": args.version_name,
                "githubRunNumber": os.environ.get("GITHUB_RUN_NUMBER"),
                "githubRunAttempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                "scope": "Static signature, manifest, ABI, payload and metadata checks only; no device tests; not a release catalog.",
                "components": [], "playerAars": [], "errors": []}
    try:
        if args.version_code <= 100000 or not re.fullmatch(r"0\.1\.[0-9]+-test", args.version_name):
            raise ValueError("Expected CI test version: code=100000+N, name=0.1.N-test")
        run = int(args.version_name.split(".")[2].removesuffix("-test"))
        if args.version_code != 100000 + run:
            raise ValueError("CI versionCode/versionName disagree")
        tools = {name: tool(name, args.repo, getattr(args, name)) for name in ("apksigner", "aapt2")}
        manifest["tools"] = tools
        catalog = load_catalog(args.repo)
        statuses = read_statuses(logs / "build-status.tsv", set(matrix))
        for aar in sorted((args.repo / "app/libs").glob("lib-*-release.aar")):
            manifest["playerAars"].append({"name": aar.name, "size": aar.stat().st_size, "sha256": sha256(aar)})
        for component, family, abi in matrix:
            row = statuses.get((component, family, abi))
            entry = {"component": component, "family": family, "abi": abi, "status": "failed"}
            manifest["components"].append(entry)
            if row is None:
                entry["reason"] = "Missing requested build record"
                continue
            entry["build"] = row
            if row["status"] != "built":
                entry["reason"] = "Build or APK collection failed; see build log"
                continue
            filename = f"{family}-{abi.replace('-', '_')}.apk" if component == "host" else f"runtime-{component}-{abi.replace('-', '_')}.apk"
            file = args.directory / ("host" if component == "host" else "runtime") / filename
            try:
                entry["artifact"] = verify_artifact(file, component, abi, args, catalog, tools, logs, family)
                entry["status"] = "verified"
            except Exception as error:
                entry["reason"] = str(error)
                (logs / f"{file.stem}-verify-error.log").write_text(str(error) + "\n", encoding="utf-8")
        hosts = {(e["family"], e["abi"]): e for e in manifest["components"] if e["component"] == "host" and e["status"] == "verified"}
        certificates = {e["artifact"]["signerSha256"] for e in hosts.values()}
        if len(hosts) != 2 * len(args.host_families) or len(certificates) != 1:
            manifest["errors"].append("Requested hosts are missing or have inconsistent signing certificates")
        if len(certificates) > 1:
            for host in hosts.values():
                host.update(status="failed", reason="Requested hosts have inconsistent signing certificates")
        for entry in manifest["components"]:
            if entry["status"] != "verified" or entry["component"] == "host":
                continue
            artifact = entry["artifact"]
            corresponding = [hosts.get((family, entry["abi"])) for family in args.host_families]
            if any(host is None or host["status"] != "verified" for host in corresponding):
                entry.update(status="failed", reason="Missing verified requested same-ABI host")
                continue
            if any(artifact["signerSha256"] != host["artifact"]["signerSha256"] for host in corresponding):
                entry.update(status="failed", reason="Runtime signer differs from requested same-ABI host")
            if entry["component"] == "mpv":
                for host in corresponding:
                    cxx = next(p["sha256"] for p in host["artifact"]["payloads"] if p["path"].endswith("/libc++_shared.so"))
                    if artifact["runtimeMetadata"]["hostLibraries"]["libc++_shared.so"] != cxx:
                        entry.update(status="failed", reason=f"MPV libc++ requirement differs from {host['family']} host")
        core = [e for e in manifest["components"] if e["component"] in CORE]
        verified_core = sum(e["status"] == "verified" for e in core)
        manifest["gatePassed"] = len(core) == required_core and verified_core == required_core and not manifest["errors"]
        manifest["coreGate"].update(verified=verified_core, passed=manifest["gatePassed"])
        manifest["requestedSuitePassed"] = manifest["gatePassed"] and len(manifest["components"]) == len(matrix) and all(e["status"] == "verified" for e in manifest["components"])
        if manifest["gatePassed"]:
            manifest["status"] = "pass" if manifest["requestedSuitePassed"] else "partial"
    except Exception as error:
        manifest["errors"].append(str(error))
    destination = args.directory / "manifest.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(destination)
    print(f"Suite {manifest['status']}; core gate={manifest['gatePassed']}; {destination}")
    for entry in manifest["components"]:
        print(f"  {entry['component']}/{entry['family']}/{entry['abi']}: {entry['status']}" + (f" — {entry['reason']}" if "reason" in entry else ""))
    for error in manifest["errors"]:
        print(f"  ERROR: {error}", file=sys.stderr)
    return 0 if manifest["gatePassed"] else 1


if __name__ == "__main__":
    sys.exit(main())
