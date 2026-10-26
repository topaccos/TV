#!/usr/bin/env bash
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
tv=$(realpath "$1")
work=$(realpath -m "$2")
mkdir -p "$work"
media="$work/media"
if [[ ! -d "$media/.git" ]]; then
    git init "$media"
    git -C "$media" remote add origin https://github.com/FongMi/media.git
    git -C "$media" fetch --depth=1 origin 3c2cbe8ac742c2fe15eff52f03eeb3b1b648848d
    git -C "$media" checkout --detach FETCH_HEAD
fi
# Source-only third-party compatibility patch for API used by TV but absent
# from the published official Media checkout. Do not execute fork workflows.
git -C "$media" fetch --depth=1 https://github.com/dlgt6/media.git 01116cae74e3b149b1c295f9a841311ac4aa6e8f
git -C "$media" diff --binary HEAD FETCH_HEAD -- libraries > "$work/media-tv-compat.patch"
git -C "$media" apply "$work/media-tv-compat.patch"
printf '%s\n' 'Media compatibility source: dlgt6/media@01116cae74e3b149b1c295f9a841311ac4aa6e8f'
python3 "$here/prepare-media.py" "$media" "$work/artifacts"
# Vulkan is available from Android API 24, matching the TV host's minSdk.
python3 - "$media" <<'PY'
import pathlib,sys
p=pathlib.Path(sys.argv[1])/'libraries/decoder_ffmpeg/build.gradle.kts'
s=p.read_text()
old='defaultConfig { ndk { abiFilters += listOf("armeabi-v7a", "arm64-v8a") } }'
assert s.count(old)==1
p.write_text(s.replace(old,'defaultConfig { minSdk = 24; ndk { abiFilters += listOf("armeabi-v7a", "arm64-v8a") } }'))
PY
mapfile -t modules < <(python3 - "$media/move.txt" <<'PY'
import pathlib,sys
for name in pathlib.Path(sys.argv[1]).read_text().splitlines():
    if name.strip(): print(name.strip().removesuffix('-release.aar'))
PY
)
tasks=()
for module in "${modules[@]}"; do tasks+=(":$module:bundleReleaseAar"); done
(
    cd "$media"
    bash ./gradlew --no-daemon --stacktrace -I "$here/export-runtime.gradle" \
        "${tasks[@]}" exportTvRuntimeDependencies
)
python3 - "$media" "$tv" <<'PY'
import hashlib,pathlib,shutil,sys
media,tv=map(pathlib.Path,sys.argv[1:])
files=[]
for name in (media/'move.txt').read_text().splitlines():
    if not name.strip(): continue
    matches=list((media/'libraries').glob(f'*/build*/outputs/aar/{name}'))
    if len(matches)!=1 or matches[0].stat().st_size==0:
        raise SystemExit(f'Missing/ambiguous actual AAR: {name}: {matches}')
    files.append(matches[0])
for source in files:
    shutil.copy2(source,tv/'app/libs'/source.name)
    print(hashlib.sha256(source.read_bytes()).hexdigest(),source.name)
matches=list(media.glob('build*/tv-consumer-dependencies.gradle'))
if len(matches)!=1: raise SystemExit(f'Unexpected dependency export: {matches}')
shutil.copy2(matches[0],tv/'app/tv-media-dependencies.gradle')
app=tv/'app/build.gradle'
text=app.read_text()
statement="apply from: 'tv-media-dependencies.gradle'"
if statement not in text: app.write_text(text+'\n'+statement+'\n')
PY
