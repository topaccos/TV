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
python3 "$here/prepare-media.py" "$media" "$work/artifacts"
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
    matches=list((media/'libraries').glob(f'*/build/outputs/aar/{name}'))
    if len(matches)!=1 or matches[0].stat().st_size==0:
        raise SystemExit(f'Missing/ambiguous actual AAR: {name}: {matches}')
    files.append(matches[0])
for source in files:
    shutil.copy2(source,tv/'app/libs'/source.name)
    print(hashlib.sha256(source.read_bytes()).hexdigest(),source.name)
deps=media/'build/tv-consumer-dependencies.gradle'
shutil.copy2(deps,tv/'app/tv-media-dependencies.gradle')
app=tv/'app/build.gradle'
text=app.read_text()
statement="apply from: 'tv-media-dependencies.gradle'"
if statement not in text: app.write_text(text+'\n'+statement+'\n')
PY
