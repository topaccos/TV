#!/usr/bin/env bash
# Linux only; invoke after ci-player-build. No release publication or key upload.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(realpath "${1:-$here/..}")
out=$(realpath -m "${2:-$repo/ci-suite}")
[[ $(uname -s) == Linux && ${GITHUB_RUN_NUMBER:-} =~ ^[0-9]+$ ]] || exit 2
run=$((10#$GITHUB_RUN_NUMBER))
code=$((100000 + run))
name="0.1.${run}-test"
(( run > 0 && code <= 2100000000 )) || exit 2
[[ -f "$repo/local.properties" && -f "$repo/gradlew" ]] || exit 2
for aar in lib-mpvplayer-release.aar lib-decoder-ffmpeg-release.aar; do
    [[ -s "$repo/app/libs/$aar" ]] || { printf 'Missing AAR: %s\n' "$aar" >&2; exit 2; }
done
if [[ -d "$out" && -n $(ls -A "$out") ]]; then printf 'Output must be empty\n' >&2; exit 2; fi
mkdir -p "$out/host" "$out/runtime" "$out/logs"
status="$out/logs/build-status.tsv"
printf 'component\tabi\tstatus\texitCode\tlog\n' > "$status"
init=$(mktemp --suffix=.gradle)
trap 'rm -f "$init"' EXIT
python3 - "$init" "$code" "$name" "$repo" <<'PY'
from pathlib import Path
import sys
Path(sys.argv[1]).write_text('''gradle.beforeProject { p ->
    p.pluginManager.withPlugin('com.android.application') {
        if (p.path == ':app') {
            p.extensions.getByName('androidComponents').finalizeDsl { dsl ->
                dsl.defaultConfig.versionCode = %s
                dsl.defaultConfig.versionName = '%s'
            }
        }
    }
}
''' % (sys.argv[2], sys.argv[3]))
repo=Path(sys.argv[4])
patches=[(repo/'app/build.gradle','        versionName "5.8.2"\n',"        buildConfigField 'boolean', 'RUNTIME_SUITE_TEST', providers.gradleProperty('tvRuntimeSuiteTest').getOrElse('false')\n"),(repo/'app/src/main/java/com/fongmi/android/tv/Updater.java','    public void start(FragmentActivity activity, boolean force) {\n','        if (BuildConfig.RUNTIME_SUITE_TEST) return;\n')]
for path,anchor,extra in patches:
    text=path.read_text()
    assert text.count(anchor)==1
    if extra not in text: path.write_text(text.replace(anchor,anchor+extra))
    else: assert text.count(extra)==1
PY
record() { printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5" >> "$status"; }
run_logged() { local log=$1 rc; shift; printf 'Running %s\n' "$log"; if "$@" > "$out/logs/$log" 2>&1; then return 0; else rc=$?; return "$rc"; fi; }
collect() {
    python3 - "$1" "$2" "$3" <<'PY'
from pathlib import Path
import shutil,sys
root,name,target=Path(sys.argv[1]),sys.argv[2],Path(sys.argv[3])
found=list(root.rglob(name))
if len(found)!=1 or found[0].stat().st_size==0: raise SystemExit(f'Missing/ambiguous {name}: {found}')
shutil.copy2(found[0],target/name)
PY
}
gradle=(bash "$repo/gradlew" --no-daemon --stacktrace --console=plain)
if run_logged host.log "${gradle[@]}" -p "$repo" -I "$init" -PtvRuntimeSuiteTest=true :app:assembleLeanbackRelease; then
    for abi in arm64-v8a armeabi-v7a; do
        if collect "$repo/app/build/outputs/apk/leanback/release" "leanback-${abi//-/_}.apk" "$out/host" >> "$out/logs/host.log" 2>&1; then record host "$abi" built 0 host.log; else record host "$abi" failed 1 host.log; fi
    done
else rc=$?; for abi in arm64-v8a armeabi-v7a; do record host "$abi" failed "$rc" host.log; done; fi
read -r -a modules <<< "${SUITE_MODULES:-mpv python quickjs nodejs thunder}"
selected=(mpv python)
for module in "${modules[@]}"; do
    case "$module" in mpv|python) ;; quickjs|nodejs|thunder|bdj) [[ " ${selected[*]} " == *" $module "* ]] || selected+=("$module");; *) exit 2;; esac
done
for module in "${selected[@]}"; do
    case "$module" in mpv) prefix=tvMpv;; python) prefix=tvPython;; quickjs) prefix=tvQuickJs;; nodejs) prefix=tvNodeJs;; thunder) prefix=tvThunder;; bdj) prefix=tvBdj;; esac
    extra=()
    if [[ "$module" == bdj ]]; then
        if [[ -z ${TV_BDJ_RUNTIME_DIRECTORY:-} ]]; then
            printf 'BD-J input missing; no placeholder produced\n' > "$out/logs/bdj.log"
            for abi in arm64-v8a armeabi-v7a; do record bdj "$abi" failed 2 bdj.log; done
            continue
        fi
        extra+=("-PtvBdjRuntimeDirectory=$TV_BDJ_RUNTIME_DIRECTORY")
    fi
    for flavor in arm64 arm32; do
        if [[ "$flavor" == arm64 ]]; then abi=arm64-v8a; else abi=armeabi-v7a; fi
        log="$module-$flavor.log"
        if [[ "$module" == python ]]; then
            if run_logged "python-aar-$flavor.log" "${gradle[@]}" -p "$repo/chaquo/runtime" "assemble${flavor^}Release"; then :; else
                rc=$?; record "$module" "$abi" failed "$rc" "python-aar-$flavor.log"; continue
            fi
        fi
        if run_logged "$log" "${gradle[@]}" -p "$repo/runtime" "-PtvRuntimeModule=$module" "-P${prefix}VersionCode=$code" "-P${prefix}VersionName=$name" "${extra[@]}" "assemble${flavor^}Release"; then
            if collect "$repo/runtime/build/$module/outputs/apk/$flavor/release" "runtime-$module-${abi//-/_}.apk" "$out/runtime" >> "$out/logs/$log" 2>&1; then record "$module" "$abi" built 0 "$log"; else record "$module" "$abi" failed 1 "$log"; fi
        else rc=$?; record "$module" "$abi" failed "$rc" "$log"; fi
    done
done
python3 "$here/verify-suite.py" "$out" --repo "$repo" --version-code "$code" --version-name "$name"
