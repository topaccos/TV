#!/usr/bin/env python3
"""Connect existing real MPV filter outcomes and verified runtime loading."""
import pathlib
import sys
root = pathlib.Path(sys.argv[1]) / 'libraries/mpvplayer/src/main/java/androidx/media3/mpvplayer'
def replace(path, old, new):
    source = path.read_text()
    if source.count(old) != 1:
        raise RuntimeError(f'Unexpected source layout in {path}: {old!r}')
    path.write_text(source.replace(old, new))
replace(root/'MpvLibrary.java', '    return LOADER.isAvailable();',
        '    java.util.function.BooleanSupplier loader = libraryLoader;\n'
        '    return loader != null ? loader.getAsBoolean() : LOADER.isAvailable();')
replace(root/'MpvPlayer.java', '  public boolean setAudioFilter(MpvAudioFilter audioFilter) {',
        '  public void setAudioFilterListener(@Nullable java.util.function.Consumer<Boolean> listener) {\n'
        '    verifyApplicationThread();\n'
        '    effectController.setAudioFilterListener(listener);\n'
        '  }\n\n'
        '  public boolean setAudioFilter(MpvAudioFilter audioFilter) {')
controller = root/'core/MpvEffectController.java'
replace(controller, '  @Nullable private MpvAudioFilter appliedAudioFilter;',
        '  @Nullable private MpvAudioFilter appliedAudioFilter;\n'
        '  @Nullable private java.util.function.Consumer<Boolean> audioFilterListener;\n\n'
        '  public void setAudioFilterListener(@Nullable java.util.function.Consumer<Boolean> listener) {\n'
        '    audioFilterListener = listener;\n'
        '  }\n\n'
        '  private boolean applyAudioFilterAndNotify(MpvAudioFilter filter) {\n'
        '    boolean success = applyAudioFilter(filter);\n'
        '    if (audioFilterListener != null) audioFilterListener.accept(success);\n'
        '    return success;\n'
        '  }')
source = controller.read_text()
if source.count('applyAudioFilter(targetAudioFilter)') != 2:
    raise RuntimeError('Unexpected audio filter apply call sites')
controller.write_text(source.replace('applyAudioFilter(targetAudioFilter)', 'applyAudioFilterAndNotify(targetAudioFilter)'))
print('Connected native runtime loader and real audio filter result notifications')
