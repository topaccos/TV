#!/usr/bin/env python3
"""Prepare pinned BD-J host assets/JVM; audit (never mix) optional libass JNI.

Before Media Gradle compilation:
  python3 ci-runtime-build/prepare-optional.py "$media"
Then build-suite.sh can use TV_BDJ_RUNTIME_DIRECTORY=bdj-runtime and include bdj
in SUITE_MODULES. --require-libass fails closed on the known incompatible JNI.
No AAR is added to the host, no native code is executed, and ZIPs stay intact.
Corresponding-source URLs/hashes, original notices and build recipes are retained;
large upstream source archives are referenced, not downloaded automatically.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import tarfile
import urllib.request
import zipfile

import bdj_assets

BDJ_BASE = 'https://github.com/FongMi/bdj-runtime/releases/download/zero-jdk8u482-r1/'
BDJ_FILES = {
    'j2re-zero-arm64-v8a.zip': 'f147cc55cba3d2062697638655fa241e113bc691607e83983b2d00a6dcea8826',
    'j2re-zero-armeabi-v7a.zip': '035995b29f654ab1e9d6ecc6554d20cd51f73ef4386f874bd291be6b5bf2c0ff',
    'sources.json': '27e0759e674a3f4b6bb222b44de97a2f2caeb37dd395f0b92e4af6a5e3208d44',
    'SOURCE-NOTICE.md': 'ea8105c26867601ac7d447728e8d3e59173955eb44ba8993e9b2ce9a44a4ac89',
    'bdj-runtime-build-sources-fb75c464.zip': '8fd3bac47ecfdc764c37def131a9c0ff8d1b9045f4bb57de9d96bc7ebf73f9b0',
    'SHA256SUMS': '686175f0a570d6db5b396e1963a5a9042826943cc0fde6adf0f8e3aabd9cd33f',
}
FORK_REVISION = '01116cae74e3b149b1c295f9a841311ac4aa6e8f'
OFFICIAL_REVISION = '9dae39e9689ee305c83f94bd11cf3bcecb9d6171'
JAVA_SUFFIX = 'src/main/java/androidx/media3/exoplayer/libass/LibassNative.java'
FORK_JAVA = 'libraries/exoplayer/' + JAVA_SUFFIX
OFFICIAL_JAVA = 'libraries/exoplayer_libass/' + JAVA_SUFFIX
OFFICIAL_C = 'libraries/exoplayer_libass/src/main/jni/media3ass.c'
FORK_JAVA_SHA256 = '21e8b5bbb95909aeb372defd9e87def2b1fa38498c4810c7353653d8697af9df'
OFFICIAL_JAVA_SHA256 = 'cfbb22f99534c6d27977dd643ecc5327324c258d68c0518182c4d057352211a9'
OFFICIAL_C_SHA256 = '0c8adf7d2430e99a2e6464107de5de0f43eb7eec5f2f929b6961e0834fde5112'
# Recorded only as rejected candidate provenance. Never download/copy its .so.
LIBASS_CANDIDATE = {
    'url': 'https://nightly.link/FongMi/media/actions/artifacts/9131565941.zip',
    'zip_sha256': '8cc91dae9c7d6770033bfe72e505b9455136c3f85e77d9ddec1f844335afa0d8',
    'aar': 'lib-exoplayer-libass-release.aar',
    'aar_sha256': '3b2326d98e1339f3ca4d1763e3a3028de20a5dd62cc738875af51b6329fe2571',
    'head': OFFICIAL_REVISION,
}


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def checked_bytes(data, expected, label):
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise RuntimeError(f'SHA-256 mismatch for {label}: expected {expected}, got {actual}')
    return data


def download(url, target, expected):
    """Verify cache hits too; publish only a complete verified download."""
    if target.exists():
        if sha256(target) != expected:
            raise RuntimeError(f'Existing file checksum mismatch; refusing overwrite: {target}')
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
            temporary = Path(output.name)
            request = urllib.request.Request(url, headers={'User-Agent': 'tv-runtime-suite-optional/1'})
            with urllib.request.urlopen(request, timeout=120) as response:
                if not response.url.startswith('https://'):
                    raise RuntimeError(f'Non-HTTPS redirect: {url}')
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
        if sha256(temporary) != expected:
            raise RuntimeError(f'Download checksum mismatch: {url}')
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def native_methods(java):
    # This intentionally handles only this pinned, simple LibassNative facade.
    java = re.sub(r'/\*.*?\*/|//[^\n]*', '', java, flags=re.S)
    types = {'void': 'V', 'long': 'J', 'int': 'I', 'float': 'F', 'double': 'D', 'boolean': 'Z',
             'String': 'Ljava/lang/String;', 'byte[]': '[B',
             'LibassFrame': 'Landroidx/media3/exoplayer/libass/LibassFrame;'}
    methods = {}
    for result, name, arguments in re.findall(
            r'public\s+static\s+native\s+(\w+)\s+(\w+)\s*\((.*?)\)\s*;', java, re.S):
        parameters = []
        for argument in arguments.split(','):
            if argument.strip():
                parameter = re.sub(r'@\w+(?:\([^)]*\))?\s*', '', argument).strip().split()
                if len(parameter) != 2:
                    raise RuntimeError(f'Unsupported JNI parameter: {argument}')
                if parameter[0] not in types:
                    raise RuntimeError(f'Unsupported JNI parameter type: {parameter[0]}')
                parameters.append(types[parameter[0]])
        if name in methods:
            raise RuntimeError(f'Unsupported overloaded JNI method: {name}')
        if result not in types:
            raise RuntimeError(f'Unsupported JNI return type: {result}')
        methods[name] = '(' + ''.join(parameters) + ')' + types[result]
    if not methods or len(methods) != len(re.findall(r'\bnative\b', java)):
        raise RuntimeError('Unparsed JNI declaration; refusing compatibility inference')
    return methods


def audit_libass(media, cache):
    local = media / FORK_JAVA
    checked_bytes(local.read_bytes(), FORK_JAVA_SHA256, str(local))
    for source, digest, name in [
        (OFFICIAL_JAVA, OFFICIAL_JAVA_SHA256, 'official-LibassNative.java'),
        (OFFICIAL_C, OFFICIAL_C_SHA256, 'official-media3ass.c'),
    ]:
        download(f'https://raw.githubusercontent.com/FongMi/media/{OFFICIAL_REVISION}/{source}',
                 cache / name, digest)
    fork = native_methods(local.read_text(encoding='utf-8'))
    official = native_methods((cache / 'official-LibassNative.java').read_text(encoding='utf-8'))
    c = (cache / 'official-media3ass.c').read_text(encoding='utf-8')
    table_match = re.search(r'static JNINativeMethod methods\[\]\s*=\s*\{(.*?)\n\};', c, re.S)
    if not table_match or 'RegisterNatives(env, native_class, methods,' not in c:
        raise RuntimeError('Cannot establish official JNI registration table')
    registered = dict(re.findall(r'\{\s*"(\w+)"\s*,\s*"([^"]+)"', table_match.group(1)))
    if registered != official:
        raise RuntimeError('Pinned official Java and native registration table disagree')
    differences = {
        name: {'fork': fork.get(name), 'official_registered': registered.get(name)}
        for name in sorted(fork.keys() | registered.keys()) if fork.get(name) != registered.get(name)
    }
    # Even equality would require auditing callback constructors, load names,
    # native dependencies and binary/source provenance. Equality alone is NOT approval.
    report = {
        'status': 'blocked-incompatible-jni' if differences else 'blocked-further-audit-required',
        'fork_revision': FORK_REVISION,
        'fork_java_sha256': FORK_JAVA_SHA256,
        'official_java_sha256': OFFICIAL_JAVA_SHA256,
        'official_c_sha256': OFFICIAL_C_SHA256,
        'candidate': LIBASS_CANDIDATE,
        'fork_methods': fork,
        'official_registration_table': registered,
        'differences': differences,
        'action': 'No libass AAR or native library copied. Do not rename libraries or add placeholders.',
    }
    return report


BDJ_SOURCE_URL = 'https://download.videolan.org/pub/videolan/libbluray/1.4.1/libbluray-1.4.1.tar.xz'
BDJ_SOURCE_SHA256 = '76b5dc40097f28dca4ebb009c98ed51321b2927453f75cc72cf74acd09b9f449'
BDJ_BUILD_FILES = {
    'buildscripts/build-bdj-jars.sh': 'b7ef25ead2688be5d45bed1492ebcf8ceba1a89b67051f2c5c56dfc4fbfb1c47',
    'buildscripts/include/depinfo.sh': '1bf5adb96f9e20b583c75448b6f604dced731ba21c8cb8a67c5e42e982261ed9',
    'buildscripts/include/patch-libbluray.sh': 'a5a907471dfb8fdf36fe110019a51ac4f9d2284338e543a3ec37f1fc83c05e2c',
    'buildscripts/patches/libbluray-1.4.1-still-eof.patch': 'cc0a15ee88b1ead665dadbc3ac363c2686e5184fc1e04a76713665cad57c0fc7',
    'buildscripts/patches/libbluray-1.4.1-checked-seek.patch': '1055d99f0940b3077e9f7171a220f242396e6bf5196415f3fc987b316d51447c',
    'buildscripts/patches/libbluray-1.4.1-menu-info.patch': '17a1b88c72c3b467ba6ac8d9419f003c93f694733b9214deee13bad5ebfedb5c',
    'buildscripts/patches/libbluray-1.4.1-bdj-pointer.patch': '232ce1e634335af5d69a705745a57ea5cd5da8d7160b9441b5ba154e7b3dec85',
    'LICENSE': 'a618084dcea6a9a555571ffc1c7dd828eb40c20c8287dc3c3cbf5831ce72c506',
}


def publish_bytes(target, data):
    """Do not replace local edits; an existing different file is an error."""
    if target.exists():
        if target.read_bytes() != data:
            raise RuntimeError(f'Existing file differs; refusing overwrite: {target}')
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('xb') as output:
        output.write(data)


def prepare_bdj_classpath(repo):
    cache = repo / 'app/bdj-runtime/classpath-source'
    artifact = cache / 'libbluray-bdj-12605342235.zip'
    source = cache / 'libbluray-1.4.1.tar.xz'
    download(bdj_assets.ARTIFACT_URL, artifact, bdj_assets.ARTIFACT_SHA256)
    download(BDJ_SOURCE_URL, source, BDJ_SOURCE_SHA256)
    raw = f'https://raw.githubusercontent.com/FongMi/mpv-android/{bdj_assets.PRODUCER_REVISION}/'
    for path, digest in BDJ_BUILD_FILES.items():
        download(raw + path, cache / 'producer' / path, digest)
    assets = repo / 'app/src/main/assets/bdj'
    with zipfile.ZipFile(artifact) as archive:
        bdj_assets.verify_paths(archive, artifact.name)
        jars = {name: archive.read('build/bdj/' + name) for name in bdj_assets.JARS}
    for name, data in jars.items():
        bdj_assets.verify_jar(data, name)
    # The upstream JARs contain no license files: retain the actual source and
    # patches alongside the inputs and package both original license texts.
    with tarfile.open(source, 'r:xz') as archive:
        licenses = {}
        for relative, target, digest in (
            ('COPYING', 'COPYING.libbluray', 'b3aa400aca6d2ba1f0bd03bd98d03d1fe7489a3bbb26969d72016360af8a5c9d'),
            ('contrib/asm/LICENSE.txt', 'LICENSE.ASM', '293b6af371eee28b0ff16f0334ea19e20a3d5522143faa4b95b346855507879a'),
        ):
            member = archive.getmember('libbluray-1.4.1/' + relative)
            if not member.isfile() or member.size > 128 * 1024:
                raise RuntimeError(f'Invalid source license: {relative}')
            licenses[target] = checked_bytes(archive.extractfile(member).read(), digest, relative)
    notice = {
        'component': 'libbluray BD-J classpath 1.4.1',
        'producerRevision': bdj_assets.PRODUCER_REVISION,
        'producerRun': 'https://github.com/FongMi/mpv-android/actions/runs/40209432515',
        'artifactUrl': bdj_assets.ARTIFACT_URL,
        'artifactSha256': bdj_assets.ARTIFACT_SHA256,
        'sourceUrl': BDJ_SOURCE_URL,
        'sourceSha256': BDJ_SOURCE_SHA256,
        'buildFiles': BDJ_BUILD_FILES,
        'jars': bdj_assets.JARS,
        'license': 'libbluray LGPL-2.1-or-later; bundled ASM BSD-3-Clause. See packaged license texts.',
        'correspondingSource': 'app/bdj-runtime/classpath-source includes the original source tar, producer recipe and ordered patches; retain when redistributing.',
        'acceptance': 'Pinned input/static verification only, not JVM or BD-J menu acceptance.',
    }
    for name, data in jars.items():
        publish_bytes(assets / name, data)
    for name, data in licenses.items():
        publish_bytes(assets / name, data)
    publish_bytes(assets / 'SOURCE-NOTICE.json', (json.dumps(notice, indent=2) + '\n').encode())
    # Publish the descriptor last; never fabricate it for absent/unverified JARs.
    publish_bytes(assets / 'jars.properties', bdj_assets.properties_bytes())
    bdj_assets.verify_assets(lambda name: (repo / 'app/src/main' / name).read_bytes())
    print(f'BD-J pinned host classpath and licenses prepared: {assets}')


def package_bdj_source(repo, destination, include_jvm=False):
    """Export verified corresponding source, not static libraries or secrets."""
    cache = repo / 'app/bdj-runtime/classpath-source'
    assets = repo / 'app/src/main/assets/bdj'
    bdj_assets.verify_assets(lambda name: (repo / 'app/src/main' / name).read_bytes())
    inputs = [('libbluray-1.4.1.tar.xz', BDJ_SOURCE_SHA256)]
    inputs += [('producer/' + path, digest) for path, digest in BDJ_BUILD_FILES.items()]
    for relative, digest in inputs:
        data = checked_bytes((cache / relative).read_bytes(), digest, relative)
        publish_bytes(destination / relative, data)
    for name in ('COPYING.libbluray', 'LICENSE.ASM', 'SOURCE-NOTICE.json'):
        publish_bytes(destination / name, (assets / name).read_bytes())
    if include_jvm:
        for name, digest in BDJ_FILES.items():
            if name.startswith('j2re-zero-'):
                continue
            data = checked_bytes((repo / 'app/bdj-runtime' / name).read_bytes(), digest, name)
            publish_bytes(destination / 'jvm' / name, data)


def prepare_bdj(repo):
    prepare_bdj_classpath(repo)
    destination = repo / 'app/bdj-runtime'
    for name, digest in BDJ_FILES.items():
        download(BDJ_BASE + name, destination / name, digest)
    for abi in ('arm64-v8a', 'armeabi-v7a'):
        archive = destination / f'j2re-zero-{abi}.zip'
        # Read-only integrity check. Never extract or execute payloads; retaining
        # original bytes also retains licenses/notices inside each upstream ZIP.
        with zipfile.ZipFile(archive) as source:
            names = source.namelist()
            if len(names) != len(set(names)) or source.testzip() is not None:
                raise RuntimeError(f'Invalid ZIP integrity: {archive}')
            if not any('license' in n.lower() or 'copying' in n.lower() for n in names):
                raise RuntimeError(f'Missing original license notices: {archive}')
    print(f'BD-J JVM inputs verified (not device acceptance), unchanged archives and source notices: {destination}')
    print('Runtime hook: TV_BDJ_RUNTIME_DIRECTORY=bdj-runtime SUITE_MODULES="mpv python quickjs nodejs thunder bdj" bash ci-runtime-build/build-suite.sh')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('media', type=Path, nargs='?', help='custom Media tree after the pinned dlgt6 compatibility patch')
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--bdj-only', action='store_true', help='prepare pinned host classpath and JVM inputs without approving libass')
    parser.add_argument('--bdj-classpath-only', action='store_true', help='prepare only host JARs, licenses and corresponding source; no JVM download')
    parser.add_argument('--export-bdj-source', type=Path, help='offline: verify and copy already prepared corresponding source into this directory')
    parser.add_argument('--include-jvm-source', action='store_true', help='with --export-bdj-source, require and preserve pinned JVM source materials too')
    parser.add_argument('--require-libass', action='store_true', help='exit nonzero on blocked libass; default still prepares independent BD-J')
    args = parser.parse_args()
    if not (args.repo / 'app').is_dir():
        parser.error('--repo must contain app/')
    if args.export_bdj_source:
        if args.media or args.require_libass or args.bdj_only or args.bdj_classpath_only:
            parser.error('--export-bdj-source cannot be combined with preparation/audit flags')
        package_bdj_source(args.repo.resolve(), args.export_bdj_source.resolve(), args.include_jvm_source)
        return 0
    if args.include_jvm_source:
        parser.error('--include-jvm-source requires --export-bdj-source')
    if args.bdj_only or args.bdj_classpath_only:
        if args.media or args.require_libass or (args.bdj_only and args.bdj_classpath_only):
            parser.error('--bdj-only/--bdj-classpath-only cannot be combined with each other, media or --require-libass')
        if args.bdj_classpath_only:
            prepare_bdj_classpath(args.repo.resolve())
        else:
            prepare_bdj(args.repo.resolve())
        return 0
    if args.media is None:
        parser.error('media is required unless --bdj-only is selected')
    # Fail before runtime downloads if the supplied Media bridge is not our pin.
    report = audit_libass(args.media.resolve(), args.repo / 'app/bdj-runtime/libass-audit')
    audit_path = args.repo / 'app/bdj-runtime/libass-audit/result.json'
    audit_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(f'LIBASS BLOCKED: {len(report["differences"])} JNI method differences; {audit_path}', file=sys.stderr)
    for name, difference in report['differences'].items():
        print(f'  {name}: fork={difference["fork"]}, official={difference["official_registered"]}', file=sys.stderr)
    prepare_bdj(args.repo.resolve())
    if args.require_libass:
        return 2
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(f'Optional runtime preparation failed: {error}', file=sys.stderr)
        sys.exit(1)
