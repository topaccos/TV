#!/usr/bin/env python3
"""Build real pinned Media sources against verified unified FFmpeg inputs."""
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

MEDIA_COMMIT = '3c2cbe8ac742c2fe15eff52f03eeb3b1b648848d'
MPV_COMMIT = '1a135480fe5944cf6c7a6ae43056ab5862af7fc1'
ARTIFACTS = {
    'native': (12606075956, 'e95480b3709881bcefe5dc5417b32bfa0873866da6c2aba9b1cbf15fdf05ca9a'),
    'renderer': (12605342246, '0d6f5a865bf094409ece9ef3ca3149285a388373f64f45fb3331936547448b1f'),
}
ABIS = ('armeabi-v7a', 'arm64-v8a')
SPLIT = {'libavcodec.so', 'libavdevice.so', 'libavfilter.so', 'libavformat.so',
         'libavutil.so', 'libswresample.so', 'libswscale.so'}

def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def replace_once(p, old, new):
    text = p.read_text(encoding='utf-8')
    if text.count(old) != 1:
        raise RuntimeError(f'Unexpected source: {p}: wanted one occurrence of {old!r}')
    p.write_text(text.replace(old, new), encoding='utf-8')

def fetch_artifact(kind, work):
    artifact_id, expected = ARTIFACTS[kind]
    archive = work / f'{kind}-{artifact_id}.zip'
    if not archive.exists():
        url = f'https://nightly.link/FongMi/mpv-android/actions/artifacts/{artifact_id}.zip'
        request = urllib.request.Request(url, headers={'User-Agent': 'tv-native-build'})
        with urllib.request.urlopen(request, timeout=180) as source, archive.open('wb') as out:
            shutil.copyfileobj(source, out)
    if digest(archive) != expected:
        raise RuntimeError(f'Artifact SHA-256 mismatch: {archive}')
    destination = work / kind
    destination.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        for entry in z.infolist():
            if not (destination / entry.filename).resolve().is_relative_to(destination.resolve()):
                raise RuntimeError('Unsafe artifact archive path')
        z.extractall(destination)
    return destination

def single(paths, description):
    paths = list(paths)
    if len(paths) != 1:
        raise RuntimeError(f'{description}: expected exactly one file, got {paths}')
    return paths[0]

def verify_manifest(base, name):
    for line in (base / name).read_text().splitlines():
        expected, path = line.split(maxsplit=1)
        p = base / path.lstrip('*')
        if not p.resolve().is_relative_to(base.resolve()) or digest(p) != expected:
            raise RuntimeError(f'Manifest mismatch: {p}')

def main():
    media = pathlib.Path(sys.argv[1]).resolve()
    work = pathlib.Path(sys.argv[2]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    revision = subprocess.check_output(['git', '-C', str(media), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != MEDIA_COMMIT:
        raise RuntimeError(f'Wrong Media source revision {revision}')
    native = fetch_artifact('native', work)
    renderer_files = fetch_artifact('renderer', work)
    sdk = single(renderer_files.rglob('files.sha256'), 'SDK manifest').parent
    verify_manifest(sdk, 'files.sha256')
    for abi in ABIS:
        verify_manifest(sdk / abi / 'include', '../headers.sha256')
    decoder = media / 'libraries/decoder_ffmpeg/src/main'
    mpv = media / 'libraries/mpvplayer/src/main'
    for target in (decoder / 'jniLibs', mpv / 'jniLibs', decoder / 'renderer-sdk'):
        if target.exists():
            shutil.rmtree(target)
    shutil.copytree(sdk, decoder / 'renderer-sdk')
    provenance = []
    for abi in ABIS:
        for target in (decoder / 'jniLibs' / abi, mpv / 'jniLibs' / abi):
            target.mkdir(parents=True)
        producer = single(native.rglob(f'{abi}/libffmpeg.so'), f'{abi} unified FFmpeg').parent
        if any((producer / p).exists() for p in SPLIT):
            raise RuntimeError(f'Split producer FFmpeg libraries remain: {producer}')
        for name in ('libffmpeg.so', 'libc++_shared.so', 'libmpv.so', 'libplayer.so'):
            source = producer / name
            if not source.is_file():
                raise RuntimeError(f'Missing producer file: {source}')
            target = decoder if name == 'libffmpeg.so' else mpv
            shutil.copy2(source, target / 'jniLibs' / abi / name)
            provenance.append(f'sha256.{abi}.{name}={digest(source)}')
    cmake = decoder / 'jni/CMakeLists.txt'
    text = cmake.read_text()
    start = text.index('foreach(ffmpeg_lib avutil swresample avcodec avformat)')
    end = text.index('endforeach()', start) + len('endforeach()')
    text = text[:start] + '''add_library(ffmpeg SHARED IMPORTED)
set_target_properties(ffmpeg PROPERTIES IMPORTED_LOCATION "${ffmpeg_binaries}/libffmpeg.so")
add_library(shaderc STATIC IMPORTED)
set_target_properties(shaderc PROPERTIES IMPORTED_LOCATION "${renderer_sdk}/lib/libshaderc.a")
''' + text[end:]
    old = 'PRIVATE swresample\n                      PRIVATE avcodec\n                      PRIVATE avformat\n                      PRIVATE avutil'
    if text.count(old) != 1:
        raise RuntimeError('Unexpected FFmpeg native source layout')
    text = text.replace(old, 'PRIVATE ffmpeg')
    if text.count('PRIVATE libplacebo') != 1:
        raise RuntimeError('Unexpected libplacebo link source layout')
    text = text.replace('PRIVATE libplacebo', 'PRIVATE libplacebo\n                      PRIVATE shaderc\n                      PRIVATE vulkan\n                      PRIVATE mediandk')
    cmake.write_text(text)
    props = decoder / 'resources/META-INF/androidx.media3.decoder.ffmpeg/native-dependencies.properties'
    old_props = props.read_text()
    props.with_suffix('.properties.upstream').write_text(old_props)
    ndk = re.search(r'^android.ndk.version=(.+)$', (sdk / 'provenance.properties').read_text(), re.M)
    if not ndk:
        raise RuntimeError('SDK has no NDK provenance')
    ndk_version = ndk[1]
    if ndk_version.startswith('r'):
        sdk_root = pathlib.Path(os.environ.get('ANDROID_SDK_ROOT', os.environ.get('ANDROID_HOME', '')))
        revisions = []
        for properties in (sdk_root / 'ndk').glob('*/source.properties'):
            match = re.search(r'^Pkg.Revision\s*=\s*(.+)$', properties.read_text(), re.M)
            if match and match[1].split('.')[0] == ndk_version[1:]:
                revisions.append(match[1].strip())
        if len(revisions) != 1:
            raise RuntimeError(f'Install exactly one numeric NDK matching producer {ndk_version}: got {revisions}')
        ndk_version = revisions[0]
    props.write_text('schema.version=3\nmanifest.kind=decoder-ffmpeg-consumer\n'
                     f'media.commit={MEDIA_COMMIT}\nmpv-android.commit={MPV_COMMIT}\n'
                     f'artifact.github.run.id=40209432515\nandroid.ndk.version={ndk_version}\n'
                     'abi.list=armeabi-v7a,arm64-v8a\nlibrary.list=libffmpeg.so\n'
                     + '\n'.join(provenance) + '\n')
    bridge = mpv / 'java/is/xyz/mpv/MPVLib.java'
    replace_once(bridge, '  public static native void create(Context appctx);', '''  public interface Stream {
    int read(java.nio.ByteBuffer buffer);
    long seek(long position);
    long size();
    void cancel();
    void close();
  }

  private static final java.util.concurrent.ConcurrentHashMap<String,
      java.util.concurrent.Callable<Stream>> STREAMS = new java.util.concurrent.ConcurrentHashMap<>();

  public static void registerStream(String uri, java.util.concurrent.Callable<Stream> factory) {
    STREAMS.put(uri, factory);
  }

  public static void unregisterStream(String uri) {
    STREAMS.remove(uri);
  }

  public static @Nullable Stream openStream(String uri) throws Exception {
    java.util.concurrent.Callable<Stream> factory = STREAMS.get(uri);
    return factory == null ? null : factory.call();
  }

  public static native void create(Context appctx);''')
    replace_once(bridge, '''  public static void eventCommandReply(long requestId, int error) {
    for (EventObserver observer : OBSERVERS) {
      observer.eventCommandReply(requestId, error);
    }
  }''', '''  public static void eventCommandReply(long requestId, int error) {
    eventCommandReply(requestId, error, 0L);
  }

  public static void eventCommandReply(long requestId, int error, long result) {
    for (EventObserver observer : OBSERVERS) {
      observer.eventCommandReply(requestId, error, result);
    }
  }''')
    replace_once(bridge, '    default void eventCommandReply(long requestId, int error) {}', '''    default void eventCommandReply(long requestId, int error) {}

    default void eventCommandReply(long requestId, int error, long result) {
      eventCommandReply(requestId, error);
    }''')
    print(f'Prepared pinned real Media sources at {media}; SDK NDK {ndk_version}')

if __name__ == '__main__':
    main()
