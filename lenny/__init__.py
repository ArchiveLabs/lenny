#!/usr/bin/env python

import os as _os

__title__ = 'lenny'
try:
    with open(_os.path.join(_os.path.dirname(__file__), '..', 'VERSION')) as _f:
        __version__ = _f.read().strip()
except FileNotFoundError:
    __version__ = '0.0.0'
__author__ = 'ArchiveLabs' # roni, mek, et al