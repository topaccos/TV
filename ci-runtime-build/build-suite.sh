#!/usr/bin/env bash
# Linux CI only. Run AFTER ci-player-build/build-media.sh has populated app/libs.
# Usage: bash ci-runtime-build/build-suite.sh [repository] [output-directory]
# Optional: SUITE_HOST_FAMILIES='leanback mobile', SUITE_MODULES='mpv python quickjs nodejs thunder [bdj]'
# All hosts require prepare-optional.py --bdj-classpath-only first (no JVM).
# BD-J additionally requires --bdj-only and TV_BDJ_RUNTIME_DIRECTORY (relative to app, or absolute).
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(realpath "${1:-$here/..}")
out=$(realpath -m "${2:-$repo/ci-suite}")
[[ $(uname -s) == Linux ]] || { printf 'Builds require a Linux runner.\n' >&2; exit 2; }
[[ ${GITHUB_RUN_NUMBER:-} =~ ^[0-9]+$ ]] || { printf 'Set GITHUB_RUN_NUMBER to a numeric CI run number.\n' >&2; exit 2; }
run=$((10#$GITHUB_RUN_NUMBER))
code=$((100000 + run))
name="0.1.${run}-test"
(( run > 0 && code <= 2100000000 )) || { printf 'Invalid Android versionCode.\n' >&2; exit 2; }
[[ -f "$repo/local.properties" && -f "$repo/gradlew" ]] || { printf 'Missing root local.properties or gradlew.\n' >&2; exit 2; }
for aar in lib-mpvplayer-release.aar lib-decoder-ffmpeg-release.aar; do
    [[ -s "$repo/app/libs/$aar" ]] || { printf 'Missing player AAR: %s; run ci-player-build first.\n' "$aar" >&2; exit 2; }
done
read -r -a families <<< "${SUITE_HOST_FAMILIES-leanback}"
(( ${#families[@]} > 0 )) || { printf 'At least one host family is required.\n' >&2; exit 2; }
seen=' '
for family in "${families[@]}"; do
    case "$family" in leanback|mobile) ;; *) printf 'Unknown host family: %s\n' "$family" >&2; exit 2 ;; esac
    [[ "$seen" != *" $family "* ]] || { printf 'Duplicate host family: %s\n' "$family" >&2; exit 2; }
    seen+="$family "
done
read -r -a modules <<< "${SUITE_MODULES:-mpv python quickjs nodejs thunder}"
# Core modules cannot be disabled by SUITE_MODULES.
selected=(mpv python)
for module in "${modules[@]}"; do
    case "$module" in
        mpv|python) ;;
        quickjs|nodejs|thunder|bdj)
            if [[ " ${selected[*]} " != *" $module "* ]]; then selected+=("$module"); fi ;;
        *) printf 'Unknown module: %s\n' "$module" >&2; exit 2 ;;
    esac
done
# Never mix previous run APKs or manifests with this run. Logs contain no key files.
if [[ -d "$out" && -n $(ls -A "$out") ]]; then
    printf 'Output directory must be empty: %s\n' "$out" >&2
    exit 2
fi
mkdir -p "$out/host" "$out/runtime" "$out/logs"
# Fail closed before Gradle if host classpath/source was not prepared. Keep the
# corresponding source with downloadable APKs rather than on a disposable runner.
source_options=()
if [[ " ${selected[*]} " == *' bdj '* ]]; then
    source_options+=(--include-jvm-source)
fi
python3 "$here/prepare-optional.py" --repo "$repo" --export-bdj-source "$out/sources/bdj" "${source_options[@]}" > "$out/logs/bdj-source.log" 2>&1
status="$out/logs/build-status.tsv"
printf 'component\tfamily\tabi\tstatus\texitCode\tlog\n' > "$status"
init=$(mktemp --suffix=.gradle)
trap 'rm -f "$init"' EXIT
# The host currently hard-codes its version. Override the DSL in memory only.
python3 - "$init" "$code" "$name" <<'PY'
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
''' % (sys.argv[2], sys.argv[3]), encoding='utf-8')
PY
record() { printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5" "$6" >> "$status"; }
run_logged() {
    local log=$1 rc
    shift
    printf 'Running %s\n' "$log"
    if "$@" > "$out/logs/$log" 2>&1; then return 0; else rc=$?; return "$rc"; fi
}
collect() {
    python3 - "$1" "$2" "$3" <<'PY'
from pathlib import Path
import shutil, sys
root, name, target = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
found = list(root.rglob(name))
if len(found) != 1 or not found[0].is_file() or found[0].stat().st_size == 0:
    raise SystemExit(f'Missing or ambiguous APK {name}: {found}')
shutil.copy2(found[0], target / name)
PY
}
# Runner-only exact/idempotent guard: test hosts must not install upstream updates.
patch_host() {
    python3 - "$repo" <<'PY'
from pathlib import Path
import sys
repo = Path(sys.argv[1])
def patched(path, old, new):
    text = path.read_text(encoding='utf-8')
    if new in text:
        assert text.count(new) == 1 and text.count(old) == 1, f'Ambiguous existing suite patch: {path}'
        return text
    assert text.count(old) == 1, f'Expected exactly one suite patch anchor: {path}'
    return text.replace(old, new, 1)
app = repo / 'app/build.gradle'
updater = repo / 'app/src/main/java/com/fongmi/android/tv/Updater.java'
anchor = '        versionName "5.8.2"\n'
field = "        buildConfigField 'boolean', 'RUNTIME_SUITE_TEST', providers.gradleProperty('tvRuntimeSuiteTest').getOrElse('false')\n"
start = '    public void start(FragmentActivity activity, boolean force) {\n'
guard = '        if (BuildConfig.RUNTIME_SUITE_TEST) return;\n'
app_text = patched(app, anchor, anchor + field)
updater_text = patched(updater, start, start + guard)
assert app_text.count('RUNTIME_SUITE_TEST') == 1, 'Conflicting BuildConfig suite fields'
assert updater_text.count('BuildConfig.RUNTIME_SUITE_TEST') == 1, 'Conflicting updater suite guards'
app.write_text(app_text, encoding='utf-8')
updater.write_text(updater_text, encoding='utf-8')
print('Runner suite update guard ready (default false; enabled only by test build property).')
PY
}
gradle=(bash "$repo/gradlew" --no-daemon --stacktrace --console=plain)
guard_rc=0
run_logged host-guard.log patch_host || guard_rc=$?
for family in "${families[@]}"; do
    log="host-$family.log"
    if (( guard_rc != 0 )); then
        for abi in arm64-v8a armeabi-v7a; do record host "$family" "$abi" failed "$guard_rc" host-guard.log; done
        continue
    fi
    if run_logged "$log" "${gradle[@]}" -p "$repo" -I "$init" -PtvRuntimeSuiteTest=true ":app:assemble${family^}Release"; then
        for abi in arm64-v8a armeabi-v7a; do
            if collect "$repo/app/build/outputs/apk/$family/release" "$family-${abi//-/_}.apk" "$out/host" >> "$out/logs/$log" 2>&1; then
                record host "$family" "$abi" built 0 "$log"
            else record host "$family" "$abi" failed 1 "$log"; fi
        done
    else
        rc=$?
        for abi in arm64-v8a armeabi-v7a; do record host "$family" "$abi" failed "$rc" "$log"; done
    fi
done
for module in "${selected[@]}"; do
    case "$module" in
        mpv) prefix=tvMpv ;; python) prefix=tvPython ;; quickjs) prefix=tvQuickJs ;;
        nodejs) prefix=tvNodeJs ;; thunder) prefix=tvThunder ;; bdj) prefix=tvBdj ;;
    esac
    extra=()
    if [[ "$module" == bdj ]]; then
        if [[ -z ${TV_BDJ_RUNTIME_DIRECTORY:-} ]]; then
            printf 'BD-J requires TV_BDJ_RUNTIME_DIRECTORY; no placeholder payload is generated.\n' > "$out/logs/bdj.log"
            for abi in arm64-v8a armeabi-v7a; do record bdj - "$abi" failed 2 bdj.log; done
            continue
        fi
        extra+=("-PtvBdjRuntimeDirectory=$TV_BDJ_RUNTIME_DIRECTORY")
    fi
    for flavor in arm64 arm32; do
        if [[ "$flavor" == arm64 ]]; then abi=arm64-v8a; else abi=armeabi-v7a; fi
        log="$module-$flavor.log"
        if [[ "$module" == python ]]; then
            # patchelf is installed by the workflow. Chaquopy selects Python 3.10;
            # let the real task report interpreter/pip failures, without guessing fixes.
            if run_logged "python-aar-$flavor.log" "${gradle[@]}" -p "$repo/chaquo/runtime" "assemble${flavor^}Release"; then :; else
                rc=$?; record "$module" - "$abi" failed "$rc" "python-aar-$flavor.log"; continue
            fi
        fi
        if run_logged "$log" "${gradle[@]}" -p "$repo/runtime" "-PtvRuntimeModule=$module" \
                "-P${prefix}VersionCode=$code" "-P${prefix}VersionName=$name" \
                "${extra[@]}" "assemble${flavor^}Release"; then
            if collect "$repo/runtime/build/$module/outputs/apk/$flavor/release" "runtime-$module-${abi//-/_}.apk" "$out/runtime" >> "$out/logs/$log" 2>&1; then
                record "$module" - "$abi" built 0 "$log"
            else record "$module" - "$abi" failed 1 "$log"; fi
        else rc=$?; record "$module" - "$abi" failed "$rc" "$log"; fi
    done
done
# Verification always runs after independent failures and writes an honest manifest.
# Exit zero requires all requested hosts + BOTH MPV/Python ABIs; optional failures yield partial.
python3 "$here/verify-suite.py" "$out" --repo "$repo" --version-code "$code" --version-name "$name" \
    --host-families "${families[@]}" --modules "${selected[@]}"
