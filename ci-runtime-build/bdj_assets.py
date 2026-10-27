"""Pinned libbluray classpath contract for the compatibility Media host.

These are the real JARs from the same producer run as the native MPV inputs.
Checking properties alone is insufficient: also verify bytes against these pins.
"""
import hashlib
import io
import re
import struct
from zipfile import ZipFile

PRODUCER_REVISION = '1a135480fe5944cf6c7a6ae43056ab5862af7fc1'
ARTIFACT_URL = 'https://nightly.link/FongMi/mpv-android/actions/artifacts/12605342235.zip'
ARTIFACT_SHA256 = '10f535ec99c11fb5e87dfb8a0867aafe22452c01c47098dca154b1f1f32e353e'
JARS = {
    'libbluray-j2se-1.4.1.jar': {
        'size': 777599,
        'sha256': 'ac14a66b732c97f0e1eac2cd3d3a1e2308ead2171c2db8476b877cda73b6537b',
        'classes': ('org/videolan/Libbluray.class', 'org/videolan/BDJLoader.class'),
    },
    'libbluray-awt-j2se-1.4.1.jar': {
        'size': 70517,
        'sha256': 'cf4d504e8703a03e90dabb69791bfa8e610135ad09f10c773fd12596a0beb17a',
        'classes': ('java/awt/BDToolkit.class', 'java/awt/BDGraphicsEnvironment.class'),
    },
}
PREFIX = 'assets/bdj/'
LICENSES = {
    'COPYING.libbluray': 'b3aa400aca6d2ba1f0bd03bd98d03d1fe7489a3bbb26969d72016360af8a5c9d',
    'LICENSE.ASM': '293b6af371eee28b0ff16f0334ea19e20a3d5522143faa4b95b346855507879a',
}


def verify_paths(archive, label):
    # ZipInfo.filename normalizes backslashes on Windows and truncates NUL;
    # inspect the original entry spelling so checks behave identically on CI.
    names = [info.orig_filename for info in archive.infolist()]
    if len(names) != len(set(names)):
        raise ValueError(f'{label}: duplicate ZIP entries')
    for name in names:
        if ('\\' in name or '\x00' in name or name.startswith('/')
                or re.match(r'^[A-Za-z]:', name)
                or any(part in ('.', '..') for part in name.split('/'))):
            raise ValueError(f'{label}: unsafe ZIP path {name}')
    if len(names) > 4096 or sum(info.file_size for info in archive.infolist()) > 32 * 1024 * 1024:
        raise ValueError(f'{label}: excessive ZIP contents')
    if archive.testzip() is not None:
        raise ValueError(f'{label}: ZIP CRC failure')


def verify_jar(data, name):
    expected = JARS[name]
    if len(data) != expected['size']:
        raise ValueError(f'{name}: JAR size mismatch')
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected['sha256']:
        raise ValueError(f'{name}: pinned JAR SHA-256 mismatch (wrong version or modified bytes)')
    with ZipFile(io.BytesIO(data)) as jar:
        verify_paths(jar, name)
        for path in ('META-INF/MANIFEST.MF', *expected['classes']):
            if path not in jar.namelist():
                raise ValueError(f'{name}: missing required entry {path}')
        for path in jar.namelist():
            if not path.endswith('.class'):
                continue
            header = jar.read(path)[:8]
            if len(header) != 8 or header[:4] != b'\xca\xfe\xba\xbe':
                raise ValueError(f'{name}: invalid class header {path}')
            minor, major = struct.unpack('>HH', header[4:])
            if minor != 0 or not 45 <= major <= 49:
                raise ValueError(f'{name}: unsupported class version {major}.{minor}: {path}')
    return {'path': PREFIX + name, 'size': len(data), 'sha256': digest}


def properties_bytes():
    return ''.join(f'{name}.sha256={entry["sha256"]}\n{name}.size={entry["size"]}\n'
                   for name, entry in JARS.items()).encode('ascii')


def parse_properties(data):
    if len(data) > 4096:
        raise ValueError('bdj/jars.properties: excessive size')
    properties = {}
    for line in data.decode('ascii').splitlines():
        if not line.strip() or line.lstrip().startswith(('#', '!')):
            continue
        key, separator, value = line.partition('=')
        if not separator or key in properties:
            raise ValueError('bdj/jars.properties: malformed or duplicate property')
        properties[key] = value
    expected_keys = {name + suffix for name in JARS for suffix in ('.sha256', '.size')}
    if properties.keys() != expected_keys:
        raise ValueError('bdj/jars.properties: missing or unexpected keys/version')
    for name, expected in JARS.items():
        digest, size = properties[name + '.sha256'], properties[name + '.size']
        if not re.fullmatch('[0-9a-f]{64}', digest) or digest != expected['sha256']:
            raise ValueError(f'{name}: properties SHA-256 differs from pin')
        if not re.fullmatch('[1-9][0-9]*', size) or int(size) != expected['size']:
            raise ValueError(f'{name}: properties size differs from pin')
    return properties


def verify_assets(read):
    """read accepts an APK entry name; missing entries must raise an exception."""
    parse_properties(read(PREFIX + 'jars.properties'))
    jars = [verify_jar(read(PREFIX + name), name) for name in JARS]
    for name, expected in LICENSES.items():
        if hashlib.sha256(read(PREFIX + name)).hexdigest() != expected:
            raise ValueError(f'{name}: missing/modified BD-J license')
    return jars
