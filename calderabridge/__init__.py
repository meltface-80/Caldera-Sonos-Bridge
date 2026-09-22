# Caldera Sonos Bridge — play your Plex music library to Sonos speakers.
# Copyright (c) 2026 Lewis Menzies (Music Duck / MusicD)
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Caldera Sonos Bridge - play Plex music to Sonos speakers."""

from .config import BRIDGE_NAME, BRIDGE_VERSION

__all__ = ["BRIDGE_NAME", "BRIDGE_VERSION"]
__version__ = BRIDGE_VERSION
