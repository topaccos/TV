#!/usr/bin/env python3
"""Offline orchestration tests of the real build-suite.sh, not APK verification.

All repositories, tool shims, logs, and deliberately plain-text .apk fixtures live
in TemporaryDirectory and are deleted after each case. No Android SDK, Gradle,
network, device, or distribution artifact is used. The fake verifier only records
arguments/statuses and models the final exit status; static verification belongs
to the separate verifier tests. Run with `uv run --offline --no-project python
ci-runtime-build/test-build-orchestration.py` on Linux or Windows with Git Bash.
"""
import csv
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
ABIS = ("arm64-v8a", "armeabi-v7a")
MODULES = ("mpv", "python", "quickjs", "nodejs", "thunder", "bdj")
FIXTURE_APK = "ORCHESTRATION-ONLY PLAIN TEXT; NOT AN ANDROID APK\n"
APP_ANCHOR = '        versionName "5.8.2"\n'
UPDATER_ANCHOR = '    public void start(FragmentActivity activity, boolean force) {\n'
GUARD_MESSAGE = "Runner suite update guard ready"

# These programs are written only into the temporary fake repository. They never
# import the real preparation/verifier code or invoke an external build tool.
FAKE_PREPARE = r'''import json, os, sys
from pathlib import Path
with Path(os.environ['ORCHESTRATION_EVENTS']).open('a', encoding='utf-8') as stream:
    stream.write(json.dumps({'kind': 'prepare', 'argv': sys.argv[1:]}) + '\n')
print('ORCHESTRATION ONLY: offline preparation stub')
'''

FAKE_GRADLE = r'''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
project = Path(args[args.index('-p') + 1])
repo = Path(os.environ['ORCHESTRATION_REPO'])
assert '--no-daemon' in args and '--stacktrace' in args and '--console=plain' in args
module = next((a.split('=', 1)[1] for a in args if a.startswith('-PtvRuntimeModule=')), None)
task = args[-1]
event = {'kind': 'gradle', 'argv': args, 'project': project.as_posix(), 'module': module, 'task': task}
with Path(os.environ['ORCHESTRATION_EVENTS']).open('a', encoding='utf-8') as stream:
    stream.write(json.dumps(event) + '\n')
print('ORCHESTRATION ONLY: fake Gradle ' + task + ' module=' + str(module), flush=True)
if project == repo:
    family = {'leanback': ':app:assembleLeanbackRelease', 'mobile': ':app:assembleMobileRelease'}
    family = next(name for name, expected in family.items() if task == expected)
    assert '-PtvRuntimeSuiteTest=true' in args
    init = Path(args[args.index('-I') + 1]).read_text(encoding='utf-8')
    assert '100027' in init and '0.1.27-test' in init
    assert "buildConfigField 'boolean', 'RUNTIME_SUITE_TEST'" in (repo / 'app/build.gradle').read_text(encoding='utf-8')
    assert 'if (BuildConfig.RUNTIME_SUITE_TEST) return;' in (repo / 'app/src/main/java/com/fongmi/android/tv/Updater.java').read_text(encoding='utf-8')
    if os.environ.get('ORCHESTRATION_FAIL_HOST') == family:
        print('intentional host failure: ' + family)
        raise SystemExit(23)
    for abi in ('arm64-v8a', 'armeabi-v7a'):
        target = repo / 'app/build/outputs/apk' / family / 'release' / (family + '-' + abi.replace('-', '_') + '.apk')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(os.environ['ORCHESTRATION_APK_TEXT'], encoding='utf-8')
else:
    flavor = {'assembleArm64Release': 'arm64', 'assembleArm32Release': 'arm32'}[task]
    if project == repo / 'chaquo/runtime':
        if os.environ.get('ORCHESTRATION_FAIL_PYTHON_AAR') == flavor:
            print('intentional Python AAR prerequisite failure: ' + flavor)
            raise SystemExit(31)
        (repo / ('python-aar-' + flavor + '.ready')).write_text('orchestration only', encoding='utf-8')
    else:
        assert project == repo / 'runtime'
        prefix = {'mpv': 'tvMpv', 'python': 'tvPython', 'quickjs': 'tvQuickJs', 'nodejs': 'tvNodeJs', 'thunder': 'tvThunder', 'bdj': 'tvBdj'}[module]
        assert '-P' + prefix + 'VersionCode=100027' in args
        assert '-P' + prefix + 'VersionName=0.1.27-test' in args
        if module == 'python':
            assert (repo / ('python-aar-' + flavor + '.ready')).is_file(), 'Python AAR must be built first'
        if module == 'bdj':
            assert any(a.startswith('-PtvBdjRuntimeDirectory=') for a in args)
        abi = 'arm64_v8a' if flavor == 'arm64' else 'armeabi_v7a'
        target = repo / 'runtime/build' / module / 'outputs/apk' / flavor / 'release' / ('runtime-' + module + '-' + abi + '.apk')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(os.environ['ORCHESTRATION_APK_TEXT'], encoding='utf-8')
'''

FAKE_VERIFIER = r'''# Orchestration recorder only: does NOT inspect or verify any APK.
import argparse, csv, io, json, os, sys
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('out')
parser.add_argument('--repo', required=True)
parser.add_argument('--version-code', required=True)
parser.add_argument('--version-name', required=True)
parser.add_argument('--host-families', nargs='+', required=True)
parser.add_argument('--modules', nargs='+', required=True)
args = parser.parse_args()
status = (Path(args.out) / 'logs/build-status.tsv').read_text(encoding='utf-8')
rows = list(csv.DictReader(io.StringIO(status), delimiter='\t'))
with Path(os.environ['ORCHESTRATION_EVENTS']).open('a', encoding='utf-8') as stream:
    stream.write(json.dumps({'kind': 'verify', 'argv': sys.argv[1:]}) + '\n')
Path(os.environ['ORCHESTRATION_CAPTURE']).write_text(json.dumps({
    'orchestration_only_not_static_verification': True,
    'argv': sys.argv[1:], 'families': args.host_families, 'modules': args.modules,
    'version_code': args.version_code, 'version_name': args.version_name,
    'status': status, 'rows': rows,
}), encoding='utf-8')
print('ORCHESTRATION ONLY: final verifier recorder invoked')
raise SystemExit(17 if any(row['status'] != 'built' for row in rows) else 0)
'''


class BuildOrchestrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # On Windows this must resolve to Git Bash, not an unconfigured WSL stub.
        cls.bash = shutil.which('bash')
        if not cls.bash:
            raise unittest.SkipTest('Bash unavailable; install/use Git Bash on Windows or Bash on Linux')
        probe = subprocess.run(
            [cls.bash, '--noprofile', '--norc', '-c',
             'for tool in dirname realpath mktemp ls mkdir rm; do command -v "$tool" >/dev/null || exit 1; done; '
             'realpath -m . >/dev/null; p=$(mktemp --suffix=.gradle) && rm -f "$p"'],
            capture_output=True, text=True, timeout=20,
        )
        if probe.returncode:
            raise unittest.SkipTest('Bash/GNU utilities unavailable (Git Bash or Linux required): ' + probe.stderr)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='suite orchestration ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / 'fake repo'
        self.tools = self.root / 'fake toolchain'
        self.suite = self.root / 'suite scripts'
        self.out = self.root / 'suite output'
        self.events_path = self.root / 'events.jsonl'
        self.capture_path = self.root / 'verifier-capture.json'
        self.app = self.repo / 'app/build.gradle'
        self.updater = self.repo / 'app/src/main/java/com/fongmi/android/tv/Updater.java'
        self.write(self.repo / 'local.properties', '# offline fake SDK configuration only\n')
        for aar in ('lib-mpvplayer-release.aar', 'lib-decoder-ffmpeg-release.aar'):
            self.write(self.repo / 'app/libs' / aar, 'orchestration fixture, not an AAR\n')
        self.write(self.app, 'android {\n    defaultConfig {\n' + APP_ANCHOR + '    }\n}\n')
        self.write(self.updater, 'class Updater {\n' + UPDATER_ANCHOR + '    }\n}\n')
        self.write(self.repo / 'fake-gradle.py', FAKE_GRADLE)
        self.write(self.repo / 'gradlew', '#!/usr/bin/env bash\nexec python3 "$(dirname "$0")/fake-gradle.py" "$@"\n')
        self.write(self.suite / 'prepare-optional.py', FAKE_PREPARE)
        self.write(self.suite / 'verify-suite.py', FAKE_VERIFIER)
        # Copy the real script byte-for-byte: no shell implementation is mocked.
        self.suite.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(HERE / 'build-suite.sh', self.suite / 'build-suite.sh')
        self.write(self.tools / 'uname', '#!/usr/bin/env bash\nprintf "Linux\\n"\n', executable=True)
        # Git Bash may have no python3 (or a Windows Store alias). Explicitly use
        # this test's offline interpreter. Forward slashes also work on Windows.
        interpreter = shlex.quote(Path(sys.executable).as_posix())
        self.write(self.tools / 'python3', '#!/usr/bin/env bash\nexec ' + interpreter + ' "$@"\n', executable=True)
        self.env = os.environ.copy()
        for key in ('SUITE_HOST_FAMILIES', 'SUITE_MODULES', 'TV_BDJ_RUNTIME_DIRECTORY',
                    'BASH_ENV', 'ENV', 'CDPATH', 'ORCHESTRATION_FAIL_HOST', 'ORCHESTRATION_FAIL_PYTHON_AAR'):
            self.env.pop(key, None)
        self.env.update({
            'PATH': str(self.tools) + os.pathsep + self.env.get('PATH', ''),
            'GITHUB_RUN_NUMBER': '27',
            'ORCHESTRATION_REPO': str(self.repo),
            'ORCHESTRATION_EVENTS': str(self.events_path),
            'ORCHESTRATION_CAPTURE': str(self.capture_path),
            'ORCHESTRATION_APK_TEXT': FIXTURE_APK,
            'PYTHONDONTWRITEBYTECODE': '1',
        })

    @staticmethod
    def write(path, text, executable=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
        if executable:
            path.chmod(0o755)

    def run_suite(self, families=None, modules=None, **environment):
        env = self.env.copy()
        if families is not None:
            env['SUITE_HOST_FAMILIES'] = families
        if modules is not None:
            env['SUITE_MODULES'] = modules
        env.update(environment)
        result = subprocess.run(
            [self.bash, '--noprofile', '--norc', (self.suite / 'build-suite.sh').as_posix(),
             self.repo.as_posix(), self.out.as_posix()],
            env=env, cwd=self.root, capture_output=True, text=True, timeout=120,
        )
        self.diagnostics = result.stdout + '\n' + result.stderr
        return result

    def events(self):
        return [json.loads(line) for line in self.events_path.read_text(encoding='utf-8').splitlines()]

    def capture(self, families, modules):
        self.assertTrue(self.capture_path.is_file(), self.diagnostics)
        capture = json.loads(self.capture_path.read_text(encoding='utf-8'))
        self.assertTrue(capture['orchestration_only_not_static_verification'])
        self.assertEqual(capture['families'], list(families))
        self.assertEqual(capture['modules'], list(modules))
        self.assertEqual(capture['version_code'], '100027')
        self.assertEqual(capture['version_name'], '0.1.27-test')
        argv = capture['argv']
        start = argv.index('--host-families')
        stop = argv.index('--modules')
        self.assertEqual(argv[start + 1:stop], list(families), 'families must be passed explicitly to final verifier')
        self.assertEqual(argv[stop + 1:], list(modules))
        self.assertEqual(self.events()[-1]['kind'], 'verify', 'verification must run after all builds')
        self.assertEqual(sum(event['kind'] == 'verify' for event in self.events()), 1)
        status = (self.out / 'logs/build-status.tsv').read_text(encoding='utf-8')
        self.assertEqual(capture['status'], status)
        self.assertEqual(status.splitlines()[0], 'component\tfamily\tabi\tstatus\texitCode\tlog')
        rows = list(csv.DictReader(io.StringIO(status), delimiter='\t'))
        expected = {('host', family, abi) for family in families for abi in ABIS}
        expected.update((module, '-', abi) for module in modules for abi in ABIS)
        actual = [(row['component'], row['family'], row['abi']) for row in rows]
        self.assertEqual(len(actual), len(expected), self.diagnostics)
        self.assertEqual(len(actual), len(set(actual)), 'duplicate component/family/ABI status rows')
        self.assertEqual(set(actual), expected)
        for row in rows:
            self.assertTrue((self.out / 'logs' / row['log']).is_file(), row)
        return rows

    def assert_fixture_apks(self, count):
        apks = list(self.out.rglob('*.apk'))
        self.assertEqual(len(apks), count, self.diagnostics)
        for apk in apks:
            self.assertEqual(apk.read_text(encoding='utf-8'), FIXTURE_APK)

    def test_two_families_six_shared_modules_have_sixteen_unique_statuses(self):
        result = self.run_suite('leanback mobile', ' '.join(MODULES), TV_BDJ_RUNTIME_DIRECTORY='fake-bdj-payload')
        self.assertEqual(result.returncode, 0, self.diagnostics)
        rows = self.capture(('leanback', 'mobile'), MODULES)
        self.assertEqual(len(rows), 16)
        self.assertTrue(all(row['status'] == 'built' and row['exitCode'] == '0' for row in rows))
        self.assert_fixture_apks(16)
        events = self.events()
        self.assertEqual(events[0]['kind'], 'prepare')
        self.assertIn('--include-jvm-source', events[0]['argv'])
        gradle = [event for event in events if event['kind'] == 'gradle']
        self.assertEqual(len(gradle), 16)  # 2 hosts, 12 shared runtimes, 2 Python AAR prerequisites.
        for module in MODULES:
            self.assertEqual(sum(event['module'] == module for event in gradle), 2, 'runtimes must not rebuild per host')
        guard = (self.out / 'logs/host-guard.log').read_text(encoding='utf-8')
        self.assertIn(GUARD_MESSAGE, guard)
        for family in ('leanback', 'mobile'):
            log = (self.out / 'logs' / ('host-' + family + '.log')).read_text(encoding='utf-8')
            self.assertIn(':app:assemble' + family.title() + 'Release', log)
            self.assertNotIn(GUARD_MESSAGE, log)
        self.assertEqual(self.app.read_text(encoding='utf-8').count('RUNTIME_SUITE_TEST'), 1)
        self.assertEqual(self.updater.read_text(encoding='utf-8').count('BuildConfig.RUNTIME_SUITE_TEST'), 1)

    def test_mobile_failure_does_not_prevent_tv_or_shared_runtimes_or_verifier(self):
        # Fail the FIRST requested family: this also catches accidental early exit.
        result = self.run_suite('mobile leanback', ' '.join(MODULES),
                                ORCHESTRATION_FAIL_HOST='mobile', TV_BDJ_RUNTIME_DIRECTORY='fake-bdj-payload')
        self.assertEqual(result.returncode, 17, self.diagnostics)
        rows = self.capture(('mobile', 'leanback'), MODULES)
        for row in rows:
            failed = row['component'] == 'host' and row['family'] == 'mobile'
            self.assertEqual(row['status'], 'failed' if failed else 'built')
            self.assertEqual(row['exitCode'], '23' if failed else '0')
            if failed:
                self.assertEqual(row['log'], 'host-mobile.log')
        self.assert_fixture_apks(14)
        self.assertIn(GUARD_MESSAGE, (self.out / 'logs/host-guard.log').read_text(encoding='utf-8'))
        self.assertIn('intentional host failure: mobile', (self.out / 'logs/host-mobile.log').read_text(encoding='utf-8'))

    def test_host_guard_failure_records_all_four_hosts_but_builds_runtimes(self):
        self.write(self.app, 'android { /* deliberately missing exact version anchor */ }\n')
        result = self.run_suite('leanback mobile', ' '.join(MODULES), TV_BDJ_RUNTIME_DIRECTORY='fake-bdj-payload')
        self.assertEqual(result.returncode, 17, self.diagnostics)
        rows = self.capture(('leanback', 'mobile'), MODULES)
        hosts = [row for row in rows if row['component'] == 'host']
        self.assertEqual(len(hosts), 4)
        for row in hosts:
            self.assertEqual(row['status'], 'failed')
            self.assertNotEqual(row['exitCode'], '0')
            self.assertEqual(row['log'], 'host-guard.log')
        self.assertTrue(all(row['status'] == 'built' for row in rows if row['component'] != 'host'))
        self.assertFalse(any(event['task'].startswith(':app:') for event in self.events() if event['kind'] == 'gradle'))
        self.assertIn('Expected exactly one suite patch anchor', (self.out / 'logs/host-guard.log').read_text(encoding='utf-8'))
        self.assertFalse((self.out / 'logs/host-leanback.log').exists())
        self.assertFalse((self.out / 'logs/host-mobile.log').exists())
        self.assert_fixture_apks(12)

    def test_invalid_duplicate_and_empty_families_reject_before_any_work(self):
        for families, message in (
            ('desktop', 'Unknown host family'), ('leanback desktop', 'Unknown host family'),
            ('leanback leanback', 'Duplicate host family'), ('mobile leanback mobile', 'Duplicate host family'),
            ('', 'At least one host family'), ('   \t ', 'At least one host family'),
        ):
            with self.subTest(families=families):
                result = self.run_suite(families)
                self.assertEqual(result.returncode, 2, self.diagnostics)
                self.assertIn(message, result.stderr)
                self.assertFalse(self.out.exists(), 'invalid family selection must not create output')
                self.assertFalse(self.events_path.exists(), 'preparation/Gradle/verifier must not run')
                self.assertFalse(self.capture_path.exists())
                self.assertNotIn('RUNTIME_SUITE_TEST', self.app.read_text(encoding='utf-8'))

    def test_unset_family_defaults_to_leanback_and_passes_it_explicitly(self):
        result = self.run_suite()
        self.assertEqual(result.returncode, 0, self.diagnostics)
        rows = self.capture(('leanback',), MODULES[:-1])
        self.assertEqual(len(rows), 12)
        self.assert_fixture_apks(12)
        self.assertFalse((self.out / 'logs/host-mobile.log').exists())
        self.assertNotIn('--include-jvm-source', self.events()[0]['argv'])

    def test_mobile_only_still_builds_required_core_modules(self):
        result = self.run_suite('mobile', 'quickjs')
        self.assertEqual(result.returncode, 0, self.diagnostics)
        rows = self.capture(('mobile',), ('mpv', 'python', 'quickjs'))
        self.assertEqual(len(rows), 8)
        self.assert_fixture_apks(8)
        self.assertFalse((self.out / 'logs/host-leanback.log').exists())

    def test_python_aar_failure_is_recorded_without_stopping_other_abi_or_modules(self):
        result = self.run_suite('leanback mobile', 'quickjs', ORCHESTRATION_FAIL_PYTHON_AAR='arm64')
        self.assertEqual(result.returncode, 17, self.diagnostics)
        rows = self.capture(('leanback', 'mobile'), ('mpv', 'python', 'quickjs'))
        failed = [row for row in rows if row['status'] != 'built']
        self.assertEqual(len(failed), 1)
        self.assertEqual((failed[0]['component'], failed[0]['family'], failed[0]['abi'], failed[0]['exitCode'], failed[0]['log']),
                         ('python', '-', 'arm64-v8a', '31', 'python-aar-arm64.log'))
        python_tasks = [event['task'] for event in self.events() if event['kind'] == 'gradle' and event['module'] == 'python']
        self.assertEqual(python_tasks, ['assembleArm32Release'])
        self.assert_fixture_apks(9)


if __name__ == '__main__':
    unittest.main(verbosity=2)
