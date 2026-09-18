"""Narrow QtGui collection for DupScan's widgets-only desktop UI.

The upstream hook collects every installed image/input plugin. Two optional
plugins pull large native stacks that DupScan never invokes: Qt Virtual
Keyboard/QML and the Qt PDF image decoder. Keep all other QtGui plugins until
their product impact has been separately proven.
"""

import os

from PyInstaller.utils.hooks.qt import add_qt6_dependencies

hiddenimports, binaries, datas = add_qt6_dependencies(__file__)

_EXCLUDED_PLUGINS = {
    "libqpdf.dylib",
    # TUIO network-touch input is not a DupScan input surface and is the only
    # remaining native dependency on QtNetwork in the macOS bundle.
    "libqtuiotouchplugin.dylib",
    "libqtvirtualkeyboardplugin.dylib",
}
binaries = [
    entry for entry in binaries
    if os.path.basename(entry[0]) not in _EXCLUDED_PLUGINS
]
