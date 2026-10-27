#!/usr/bin/env python3
"""Offline regression tests; synthetic fixtures are never runtime deliverables."""
import importlib.util
import io
import os
import csv
import hashlib
import json
import subprocess
from zipfile import ZipFile, BadZipFile
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bdj_assets


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


suite = load('suite_verifier_tests', 'verify-suite.py')
optional = load('suite_optional_tests', 'prepare-optional.py')


class SignatureTests(unittest.TestCase):
    def check_signature(self, output):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(suite, 'command', return_value=output):
                return suite.signature(Path('fixture.apk'), 'apksigner', Path(directory) / 'signature.log')

    def test_old_and_new_signer_labels(self):
        digest = 'ab' * 32
        for label in ('Signer #1', 'V2 Signer', 'V2 Signer:', 'V3.1 Signer'):
            with self.subTest(label=label):
                self.assertEqual(self.check_signature(f'{label} certificate SHA-256 digest: {digest}\n'), digest)

    def test_repeated_same_certificate_is_one_identity(self):
        digest = '12' * 32
        text = ''.join(f'V{version} Signer certificate SHA-256 digest: {digest}\n' for version in (2, 3))
        self.assertEqual(self.check_signature(text), digest)

    def test_distinct_signers_rejected(self):
        with self.assertRaisesRegex(ValueError, 'exactly one signing certificate'):
            self.check_signature('Signer #1 certificate SHA-256 digest: ' + 'ab' * 32 + '\n'
                                 'V2 Signer certificate SHA-256 digest: ' + 'cd' * 32 + '\n')

    def test_missing_or_malformed_certificate_rejected(self):
        for text in ('Verified', 'Signer #1 certificate SHA-256 digest: abcd\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.check_signature(text)


class OptionalInputTests(unittest.TestCase):
    def test_cache_hash_is_checked_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'cache.zip'
            target.write_bytes(b'fixture')
            digest = optional.sha256(target)
            with patch.object(optional.urllib.request, 'urlopen', side_effect=AssertionError('network forbidden')):
                optional.download('https://example.invalid/input', target, digest)
                with self.assertRaisesRegex(RuntimeError, 'refusing overwrite'):
                    optional.download('https://example.invalid/input', target, '0' * 64)
            self.assertEqual(target.read_bytes(), b'fixture')

    def test_bdj_only_does_not_audit_libass(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'app').mkdir()
            with patch('sys.argv', ['prepare-optional.py', '--bdj-only', '--repo', directory]), \
                    patch.object(optional, 'prepare_bdj') as prepare, \
                    patch.object(optional, 'audit_libass', side_effect=AssertionError('libass not selected')):
                self.assertEqual(optional.main(), 0)
                prepare.assert_called_once_with(Path(directory).resolve())

    def test_bdj_only_rejects_libass_approval_request(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'app').mkdir()
            with patch('sys.argv', ['prepare-optional.py', '--bdj-only', '--require-libass', '--repo', directory]), \
                    patch.object(optional, 'prepare_bdj') as prepare:
                with self.assertRaises(SystemExit) as error:
                    optional.main()
                self.assertEqual(error.exception.code, 2)
                prepare.assert_not_called()

    def test_unparsed_native_methods_fail_closed(self):
        with self.assertRaises(RuntimeError):
            optional.native_methods('public static native Unknown nativeCreate();')


class BdjClasspathTests(unittest.TestCase):
    def test_real_properties_contract(self):
        self.assertEqual(len(bdj_assets.parse_properties(bdj_assets.properties_bytes())), 4)

    def test_missing_properties_and_jar(self):
        with self.assertRaises(KeyError):
            bdj_assets.verify_assets({}.__getitem__)
        with self.assertRaises(KeyError):
            bdj_assets.verify_assets({bdj_assets.PREFIX + 'jars.properties': bdj_assets.properties_bytes()}.__getitem__)

    def test_invalid_properties(self):
        valid = bdj_assets.properties_bytes()
        for data in (b'', valid + valid.splitlines(keepends=True)[0],
                     valid.replace(b'.size=777599', b'.size=0'),
                     valid.replace(b'.size=777599', b'.size=777598'),
                     valid.replace(b'ac14a66b', b'AC14A66B'),
                     valid.replace(b'ac14a66b', b'00000000'),
                     valid.replace(b'1.4.1', b'1.4.0')):
            with self.subTest(data=data[:80]), self.assertRaises(ValueError):
                bdj_assets.parse_properties(data)

    def test_unsafe_and_duplicate_zip_paths(self):
        for names in (('../escape',), ('/absolute',), ('C:/drive',), ('bad\\path',), ('same', 'same')):
            with self.subTest(names=names):
                data = io.BytesIO()
                import warnings
                with warnings.catch_warnings(), ZipFile(data, 'w') as archive:
                    warnings.simplefilter('ignore', UserWarning)
                    for name in names:
                        archive.writestr(name, b'fixture')
                # Windows ZipInfo normalizes backslashes while writing; restore
                # the raw unsafe entry in both ZIP headers to test the reader.
                raw = data.getvalue().replace(b'bad/path', b'bad\\path')
                with ZipFile(io.BytesIO(raw)) as archive, self.assertRaises(ValueError):
                    bdj_assets.verify_paths(archive, 'fixture')

    def test_wrong_size_or_digest(self):
        name = next(iter(bdj_assets.JARS))
        with self.assertRaisesRegex(ValueError, 'size mismatch'):
            bdj_assets.verify_jar(b'fixture', name)
        with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
            bdj_assets.verify_jar(b'x' * bdj_assets.JARS[name]['size'], name)

    def test_bad_jar_structure_is_rejected(self):
        # Structural unit fixtures temporarily replace the pin only inside this
        # test; production preparation always uses the real immutable constants.
        import hashlib
        name = next(iter(bdj_assets.JARS))
        def check(data, message):
            fixture = {name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                              'classes': ('required.class',)}}
            with patch.object(bdj_assets, 'JARS', fixture), self.assertRaisesRegex(ValueError, message):
                bdj_assets.verify_jar(data, name)
        data = io.BytesIO()
        with ZipFile(data, 'w') as archive:
            archive.writestr('META-INF/MANIFEST.MF', b'Manifest-Version: 1.0\n')
        check(data.getvalue(), 'missing required entry')
        data = io.BytesIO()
        with ZipFile(data, 'w') as archive:
            archive.writestr('META-INF/MANIFEST.MF', b'Manifest-Version: 1.0\n')
            archive.writestr('required.class', b'not a Java class')
        check(data.getvalue(), 'invalid class header')
        data = io.BytesIO()
        with ZipFile(data, 'w') as archive:
            archive.writestr('META-INF/MANIFEST.MF', b'Manifest-Version: 1.0\n')
            archive.writestr('required.class', b'\xca\xfe\xba\xbe\x00\x00\x00\x3d')
        check(data.getvalue(), 'unsupported class version')
        with patch.object(bdj_assets, 'JARS', {name: {'size': 3, 'sha256': hashlib.sha256(b'bad').hexdigest()}}):
            with self.assertRaises(BadZipFile):
                bdj_assets.verify_jar(b'bad', name)

    def test_jvm_source_export_requires_export_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'app').mkdir()
            with patch('sys.argv', ['prepare-optional.py', '--include-jvm-source', '--repo', directory]):
                with self.assertRaises(SystemExit) as error:
                    optional.main()
                self.assertEqual(error.exception.code, 2)

    def test_publish_never_overwrites_local_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'asset'
            optional.publish_bytes(path, b'original')
            optional.publish_bytes(path, b'original')
            with self.assertRaisesRegex(RuntimeError, 'refusing overwrite'):
                optional.publish_bytes(path, b'changed')
            self.assertEqual(path.read_bytes(), b'original')

    @unittest.skipUnless(os.environ.get('TV_BDJ_TEST_ARTIFACT'), 'set TV_BDJ_TEST_ARTIFACT to verified real producer ZIP')
    def test_real_pinned_jars_and_modified_bytes(self):
        artifact = Path(os.environ['TV_BDJ_TEST_ARTIFACT'])
        self.assertEqual(optional.sha256(artifact), bdj_assets.ARTIFACT_SHA256)
        with ZipFile(artifact) as archive:
            assets = {bdj_assets.PREFIX + 'jars.properties': bdj_assets.properties_bytes()}
            for name in bdj_assets.JARS:
                data = archive.read('build/bdj/' + name)
                assets[bdj_assets.PREFIX + name] = data
                result = bdj_assets.verify_jar(data, name)
                self.assertEqual(result['sha256'], bdj_assets.JARS[name]['sha256'])
                with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
                    bdj_assets.verify_jar(data[:-1] + bytes([data[-1] ^ 1]), name)
            # License fixtures come from the pinned real source tar, not invented
            # text. This integration test still performs no network access.
            import tarfile
            source = artifact.parent / 'libbluray-1.4.1.tar.xz'
            self.assertEqual(optional.sha256(source), optional.BDJ_SOURCE_SHA256)
            with tarfile.open(source, 'r:xz') as tar:
                for path, name in (('COPYING', 'COPYING.libbluray'), ('contrib/asm/LICENSE.txt', 'LICENSE.ASM')):
                    assets[bdj_assets.PREFIX + name] = tar.extractfile('libbluray-1.4.1/' + path).read()
            self.assertEqual(len(bdj_assets.verify_assets(assets.__getitem__)), 2)
            assets[bdj_assets.PREFIX + 'LICENSE.ASM'] = b'modified'
            with self.assertRaisesRegex(ValueError, 'license'):
                bdj_assets.verify_assets(assets.__getitem__)


class SuiteMatrixTests(unittest.TestCase):
    """Exercise real ZIP paths/catalog checks; only external Android tools are simulated.

    Matrix fixtures omit the separately pinned BD-J host classpath assets, whose
    verification is isolated here and exercised against real pins above/below.
    """
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.output = self.root / 'suite'
        self.repo = HERE.parent
        self.catalog = suite.load_catalog(self.repo)
        self.families = ['leanback', 'mobile']
        self.modules = list(suite.MODULES)
        self.signers, self.xml, self.files = {}, {}, {}
        self.rows = []
        for component, family, abi in suite.requested_matrix(self.families, self.modules):
            name = f'{family}-{abi.replace("-", "_")}.apk' if component == 'host' else f'runtime-{component}-{abi.replace("-", "_")}.apk'
            file = self.output / ('host' if component == 'host' else 'runtime') / name
            file.parent.mkdir(parents=True, exist_ok=True)
            data = self.payload(component, abi)
            with ZipFile(file, 'w') as archive:
                for path, value in data.items():
                    archive.writestr(path, value)
            self.files[(component, family, abi)] = file
            self.signers[file.name] = 'ab' * 32
            self.xml[file.name] = self.manifest_xml(family)
            self.rows.append([component, family, abi, 'built', '0', name + '.log'])
        (self.output / 'logs').mkdir()

    @staticmethod
    def elf(abi):
        header = bytearray(20)
        header[:6] = b'\x7fELF' + bytes((2 if abi == 'arm64-v8a' else 1, 1))
        header[18:20] = (183 if abi == 'arm64-v8a' else 40).to_bytes(2, 'little')
        return bytes(header)

    @staticmethod
    def manifest_xml(family, action='android.intent.action.MAIN'):
        categories = ['android.intent.category.LAUNCHER']
        if family == 'leanback':
            categories.append('android.intent.category.LEANBACK_LAUNCHER')
        text = ('N: android=http://schemas.android.com/apk/res/android\n'
                '  E: manifest (line=1)\n    E: application (line=2)\n'
                '      E: activity (line=3)\n        E: intent-filter (line=4)\n'
                f'          E: action (line=5)\n            A: android:name(0x01010003)="{action}" (Raw: "{action}")\n')
        for category in categories:
            text += f'          E: category (line=6)\n            A: android:name(0x01010003)="{category}" (Raw: "{category}")\n'
        return text

    def payload(self, component, abi):
        elf = self.elf(abi)
        if component == 'host':
            return {'AndroidManifest.xml': b'compiled XML fixture', 'classes.dex': b'dex\n035\0',
                    f'lib/{abi}/libc++_shared.so': elf}
        files = {f'lib/{abi}/{name}': elf for name in self.catalog.LIBRARIES.get(component, ())}
        if component == 'python':
            files.update({'assets/chaquopy/' + name.format(abi=abi): b'fixture' for name in self.catalog.PYTHON_ASSETS})
        elif component == 'quickjs':
            files.update({'assets/js/lib/' + name: b'fixture' for name in ('net.js', 'spider.js')})
        elif component == 'nodejs':
            files['assets/nodejs/bootstrap.js'] = b'fixture'
        elif component == 'bdj':
            classes, runtime = io.BytesIO(), io.BytesIO()
            with ZipFile(classes, 'w') as jar:
                for name in ('java/lang/Object.class', 'java/net/URL.class', 'sun/misc/Unsafe.class'):
                    jar.writestr(name, b'fixture')
            architecture = 'aarch64' if abi == 'arm64-v8a' else 'arm'
            with ZipFile(runtime, 'w') as archive:
                archive.writestr('j2re-image/lib/rt.jar', classes.getvalue())
                for name in self.catalog.BDJ_LIBRARIES:
                    archive.writestr(f'j2re-image/lib/{architecture}/{name}', elf)
            files[self.catalog.BDJ_ARCHIVE] = runtime.getvalue()
        primary = self.catalog.BDJ_ARCHIVE if component == 'bdj' else f'lib/{abi}/{self.catalog.MODULES[component][1]}'
        metadata = {'module': component, 'abi': abi, 'packageName': 'com.fongmi.runtime.' + component,
                    'compatibilityId': self.catalog.MODULES[component][0], 'versionCode': 100123,
                    'versionName': '0.1.123-test', 'archive' if component == 'bdj' else 'library': primary,
                    'size': len(files[primary]), 'sha256': hashlib.sha256(files[primary]).hexdigest(),
                    'files': [{'path': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()} for name, data in files.items()]}
        if component in self.catalog.JAVA_ENTRIES:
            metadata.update(entryClass=self.catalog.JAVA_ENTRIES[component], javaApi=1)
        if component == 'mpv':
            metadata['hostLibraries'] = {'libc++_shared.so': hashlib.sha256(elf).hexdigest()}
        data = dict(files)
        if component in self.catalog.JAVA_ENTRIES:
            data['classes.dex'] = b'dex\n035\0' + bytes(104)
        data['assets/runtime.json'] = json.dumps(metadata).encode()
        return data

    def tools(self, argv, **kwargs):
        if argv[0] == 'fixture-apksigner':
            file = Path(argv[-1])
            text = 'Signer #1 certificate SHA-256 digest: ' + self.signers[file.name] + '\n'
        elif argv[0] == 'fixture-aapt2' and argv[2] == 'xmltree':
            text = self.xml[Path(argv[3]).name]
        elif argv[0] == 'fixture-aapt2' and argv[2] == 'badging':
            file = Path(argv[-1])
            component, family, abi = next(key for key, value in self.files.items() if value == file)
            package = 'com.fongmi.android.tv' if component == 'host' else 'com.fongmi.runtime.' + component
            text = f"package: name='{package}' versionCode='100123' versionName='0.1.123-test'\nsdkVersion:'21'\n"
            if component != 'bdj':
                text += f"native-code: '{abi}'\n"
        else:
            raise AssertionError(f'Unexpected external command: {argv}')
        return subprocess.CompletedProcess(argv, 0, text, '')

    def run_suite(self, families=None, modules=None, defaults=False, real_assets=False, rows=None, header=None):
        families = self.families if families is None else families
        modules = self.modules if modules is None else modules
        selected = set(suite.requested_matrix(families, modules))
        with (self.output / 'logs/build-status.tsv').open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
            writer.writerow(suite.STATUS_FIELDS if header is None else header)
            writer.writerows([row for row in self.rows if tuple(row[:3]) in selected] if rows is None else rows)
        argv = ['verify-suite.py', str(self.output), '--repo', str(self.repo), '--version-code', '100123',
                '--version-name', '0.1.123-test', '--apksigner', 'fixture-apksigner', '--aapt2', 'fixture-aapt2']
        if not defaults:
            argv += ['--host-families', *families, '--modules', *modules]
        from contextlib import ExitStack, redirect_stdout, redirect_stderr
        with ExitStack() as stack:
            stack.enter_context(patch('sys.argv', argv))
            stack.enter_context(patch.object(subprocess, 'run', side_effect=self.tools))
            if not real_assets:
                stack.enter_context(patch.object(bdj_assets, 'verify_assets', return_value=[]))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            result = suite.main()
        return result, json.loads((self.output / 'manifest.json').read_text(encoding='utf-8'))

    def replace_payload(self, key, path, data):
        file = self.files[key]
        with ZipFile(file) as archive:
            contents = {name: archive.read(name) for name in archive.namelist()}
        contents[path] = data
        with ZipFile(file, 'w') as archive:
            for name, value in contents.items():
                archive.writestr(name, value)

    def test_both_families_all_six_verify_sixteen_real_paths(self):
        code, manifest = self.run_suite()
        self.assertEqual(code, 0)
        self.assertTrue(manifest['requestedSuitePassed'])
        self.assertEqual(manifest['coreGate'], {'required': 8, 'verified': 8, 'passed': True})
        self.assertEqual(len(manifest['components']), 16)
        self.assertTrue(all(entry['status'] == 'verified' for entry in manifest['components']))
        self.assertEqual(len({entry['artifact']['path'] for entry in manifest['components']}), 16)

    def test_backward_default_and_explicit_mobile_core_selection(self):
        for families, modules, defaults in ((['leanback'], self.modules, True), (['mobile'], ['mpv', 'python'], False)):
            with self.subTest(families=families):
                code, manifest = self.run_suite(families, modules, defaults=defaults)
                self.assertEqual(code, 0)
                self.assertTrue(manifest['requestedSuitePassed'])
                self.assertEqual(manifest['requestedHostFamilies'], families)
                self.assertEqual(manifest['requestedModules'], modules)
                self.assertEqual(manifest['coreGate']['required'], 6)
                self.assertEqual(len(manifest['components']), 2 + 2 * len(modules))

    def test_missing_requested_host_record_fails_every_runtime_for_abi(self):
        rows = [row for row in self.rows if row[:3] != ['host', 'mobile', 'arm64-v8a']]
        code, manifest = self.run_suite(rows=rows)
        self.assertEqual(code, 1)
        self.assertFalse(manifest['requestedSuitePassed'])
        entries = [entry for entry in manifest['components'] if entry['abi'] == 'arm64-v8a' and entry['component'] != 'host']
        self.assertEqual(len(entries), 6)
        self.assertTrue(all(entry['status'] == 'failed' for entry in entries))

    def test_missing_mobile_apk_does_not_fall_back_to_leanback(self):
        self.files[('host', 'mobile', 'armeabi-v7a')].unlink()
        code, manifest = self.run_suite()
        self.assertEqual(code, 1)
        entry = next(entry for entry in manifest['components'] if entry['component'] == 'host' and entry['family'] == 'mobile' and entry['abi'] == 'armeabi-v7a')
        self.assertIn('Missing/empty/non-regular APK', entry['reason'])

    def test_missing_optional_record_is_partial_not_unrequested_skip(self):
        rows = [row for row in self.rows if row[:3] != ['bdj', '-', 'arm64-v8a']]
        code, manifest = self.run_suite(rows=rows)
        self.assertEqual(code, 0)
        self.assertEqual(manifest['status'], 'partial')
        self.assertFalse(manifest['requestedSuitePassed'])
        self.assertTrue(manifest['gatePassed'])

    def test_reported_optional_build_failure_is_partial(self):
        next(row for row in self.rows if row[0] == 'quickjs')[3:5] = ['failed', '1']
        code, manifest = self.run_suite()
        self.assertEqual(code, 0)
        self.assertFalse(manifest['requestedSuitePassed'])

    def test_strict_status_schema_and_rows(self):
        variants = [('duplicate', self.rows + [self.rows[0]], None),
                    ('unknown component', self.rows + [['unknown', '-', 'arm64-v8a', 'built', '0', 'x']], None),
                    ('unknown family', [['host', 'tv', 'arm64-v8a', 'built', '0', 'x']], None),
                    ('runtime family', [['mpv', 'mobile', 'arm64-v8a', 'built', '0', 'x']], None),
                    ('unknown abi', [['host', 'mobile', 'x86', 'built', '0', 'x']], None),
                    ('status', [['host', 'mobile', 'arm64-v8a', 'skipped', '0', 'x']], None),
                    ('built nonzero', [['host', 'mobile', 'arm64-v8a', 'built', '1', 'x']], None),
                    ('failed zero', [['host', 'mobile', 'arm64-v8a', 'failed', '0', 'x']], None),
                    ('negative exit', [['host', 'mobile', 'arm64-v8a', 'failed', '-1', 'x']], None),
                    ('missing column', [['host', 'mobile', 'arm64-v8a', 'built', '0']], None),
                    ('extra column', [self.rows[0] + ['extra']], None),
                    ('empty log', [['host', 'mobile', 'arm64-v8a', 'built', '0', '']], None),
                    ('legacy header', self.rows, ['component', 'abi', 'status', 'exitCode', 'log']),
                    ('reordered header', self.rows, list(reversed(suite.STATUS_FIELDS)))]
        for label, rows, header in variants:
            with self.subTest(label=label):
                code, manifest = self.run_suite(rows=rows, header=header)
                self.assertEqual(code, 1)
                self.assertTrue(manifest['errors'])
                self.assertFalse(manifest['gatePassed'])

    def test_unrequested_family_and_module_rows_rejected(self):
        for families, modules in ((['leanback'], self.modules), (self.families, ['mpv', 'python'])):
            with self.subTest(families=families, modules=modules):
                code, manifest = self.run_suite(families, modules, rows=self.rows)
                self.assertEqual(code, 1)
                self.assertIn('Unrequested', manifest['errors'][0])

    def test_argument_choices_duplicates_and_mandatory_core(self):
        for extra in (['--host-families', 'tv'], ['--modules', 'mpv'], ['--modules', 'python'],
                      ['--modules', 'mpv', 'python', 'unknown'], ['--host-families', 'mobile', 'mobile'],
                      ['--modules', 'mpv', 'python', 'mpv'], ['--host-families'], ['--modules']):
            from contextlib import redirect_stderr
            with self.subTest(extra=extra), patch('sys.argv', ['verify-suite.py', str(self.output), '--version-code', '100123', '--version-name', '0.1.123-test', *extra]), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                suite.main()
            self.assertEqual(error.exception.code, 2)

    def test_mobile_signer_differs_from_other_hosts(self):
        self.signers['mobile-arm64_v8a.apk'] = 'cd' * 32
        code, manifest = self.run_suite()
        self.assertEqual(code, 1)
        self.assertTrue(any('inconsistent signing' in error for error in manifest['errors']))
        self.assertTrue(all(entry['status'] == 'failed' for entry in manifest['components'] if entry['component'] == 'host'))

    def test_runtime_signer_compared_to_mobile_only_selection(self):
        self.signers['runtime-python-arm64_v8a.apk'] = 'cd' * 32
        code, manifest = self.run_suite(['mobile'], ['mpv', 'python'])
        self.assertEqual(code, 1)
        self.assertTrue(any('Runtime signer differs' in entry.get('reason', '') for entry in manifest['components']))

    def test_mpv_cxx_must_match_mobile_even_when_leanback_matches(self):
        self.replace_payload(('host', 'mobile', 'arm64-v8a'), 'lib/arm64-v8a/libc++_shared.so', self.elf('arm64-v8a') + b'mobile differs')
        code, manifest = self.run_suite()
        self.assertEqual(code, 1)
        entry = next(entry for entry in manifest['components'] if entry['component'] == 'mpv' and entry['abi'] == 'arm64-v8a')
        self.assertEqual(entry['status'], 'failed')
        self.assertIn('mobile host', entry['reason'])

    def test_sdk35_expanded_android_namespace_and_activity_alias(self):
        for family in self.families:
            for abi in suite.ABIS:
                name = self.files[('host', family, abi)].name
                self.xml[name] = self.xml[name].replace('android:name(', 'http://schemas.android.com/apk/res/android:name(').replace('E: activity ', 'E: activity-alias ')
        code, manifest = self.run_suite()
        self.assertEqual(code, 0)
        self.assertTrue(manifest['requestedSuitePassed'])

    def test_missing_host_bdj_assets_are_not_optional(self):
        code, manifest = self.run_suite(real_assets=True)
        self.assertEqual(code, 1)
        self.assertFalse(manifest['gatePassed'])
        self.assertTrue(all(entry['status'] == 'failed' for entry in manifest['components'] if entry['component'] == 'host'))

    def test_runtime_digest_validation_is_retained(self):
        value = bytearray(self.elf('arm64-v8a'))
        value[10] = 1  # Valid same-ABI ELF, same size, wrong metadata digest.
        self.replace_payload(('mpv', '-', 'arm64-v8a'), 'lib/arm64-v8a/libmpv.so', bytes(value))
        code, manifest = self.run_suite()
        self.assertEqual(code, 1)
        self.assertTrue(any('payload digest mismatch' in entry.get('reason', '') for entry in manifest['components']))

    def test_mobile_apk_with_leanback_manifest_fails(self):
        self.xml['mobile-arm64_v8a.apk'] = self.manifest_xml('leanback')
        code, manifest = self.run_suite()
        self.assertEqual(code, 1)
        self.assertTrue(any('do not prove mobile' in entry.get('reason', '') for entry in manifest['components']))

    def test_leanback_apk_with_mobile_manifest_fails(self):
        self.xml['leanback-arm64_v8a.apk'] = self.manifest_xml('mobile')
        code, manifest = self.run_suite()
        self.assertEqual(code, 1)
        self.assertTrue(any('do not prove leanback' in entry.get('reason', '') for entry in manifest['components']))

    def test_category_must_be_in_same_main_filter_not_elsewhere(self):
        self.xml['leanback-arm64_v8a.apk'] = (self.manifest_xml('mobile') +
            '        E: intent-filter (line=8)\n          E: category (line=9)\n'
            '            A: android:name(0x01010003)="android.intent.category.LEANBACK_LAUNCHER"\n')
        code, manifest = self.run_suite()
        self.assertEqual(code, 1)
        self.assertTrue(any('do not prove leanback' in entry.get('reason', '') for entry in manifest['components']))
        self.xml['leanback-arm64_v8a.apk'] = self.manifest_xml('leanback')
        self.xml['mobile-arm64_v8a.apk'] = self.manifest_xml('mobile', action='android.intent.action.VIEW')
        code, manifest = self.run_suite()
        self.assertEqual(code, 1)
        self.assertTrue(any('do not prove mobile' in entry.get('reason', '') for entry in manifest['components']))

    def test_corrupt_payload_and_foreign_abi_still_rejected(self):
        self.replace_payload(('mpv', '-', 'arm64-v8a'), 'lib/arm64-v8a/libmpv.so', self.elf('arm64-v8a') + b'tampered')
        code, manifest = self.run_suite()
        self.assertEqual(code, 1)
        self.assertTrue(any('payload size mismatch' in entry.get('reason', '') for entry in manifest['components']))
        self.replace_payload(('host', 'mobile', 'armeabi-v7a'), 'lib/x86/leaked.so', b'foreign')
        code, manifest = self.run_suite()
        self.assertEqual(code, 1)
        self.assertTrue(any('foreign ABI' in entry.get('reason', '') for entry in manifest['components']))

    @unittest.skipUnless(os.environ.get('TV_BDJ_TEST_ARTIFACT'), 'set TV_BDJ_TEST_ARTIFACT to verified real producer ZIP')
    def test_real_pinned_host_assets_in_actual_both_family_apks(self):
        import tarfile
        artifact = Path(os.environ['TV_BDJ_TEST_ARTIFACT'])
        self.assertEqual(optional.sha256(artifact), bdj_assets.ARTIFACT_SHA256)
        assets = {bdj_assets.PREFIX + 'jars.properties': bdj_assets.properties_bytes()}
        with ZipFile(artifact) as archive:
            for name in bdj_assets.JARS:
                assets[bdj_assets.PREFIX + name] = archive.read('build/bdj/' + name)
        source = artifact.parent / 'libbluray-1.4.1.tar.xz'
        self.assertEqual(optional.sha256(source), optional.BDJ_SOURCE_SHA256)
        with tarfile.open(source, 'r:xz') as tar:
            for path, name in (('COPYING', 'COPYING.libbluray'), ('contrib/asm/LICENSE.txt', 'LICENSE.ASM')):
                assets[bdj_assets.PREFIX + name] = tar.extractfile('libbluray-1.4.1/' + path).read()
        for key in self.files:
            if key[0] == 'host':
                with ZipFile(self.files[key], 'a') as archive:
                    for name, value in assets.items():
                        archive.writestr(name, value)
        code, manifest = self.run_suite(real_assets=True)
        self.assertEqual(code, 0)
        self.assertTrue(manifest['requestedSuitePassed'])
        self.assertTrue(all(len(entry['artifact']['bdjClasspath']) == 2 for entry in manifest['components'] if entry['component'] == 'host'))


if __name__ == '__main__':
    unittest.main(verbosity=2)

