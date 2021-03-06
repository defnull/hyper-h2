# -*- coding: utf-8 -*-
"""
h2/settings
~~~~~~~~~~~

This module contains a HTTP/2 settings object. This object provides a simple
API for manipulating HTTP/2 settings, keeping track of both the current active
state of the settings and the unacknowledged future values of the settings.
"""
import collections

from hyperframe.frame import SettingsFrame


#: A value structure for storing changed settings.
ChangedSetting = collections.namedtuple(
    'ChangedSetting', ['setting', 'original_value', 'new_value']
)


class Settings(collections.MutableMapping):
    """
    An object that encapsulates HTTP/2 settings state.

    HTTP/2 Settings are a complex beast. Each party, remote and local, has its
    own settings and a view of the other party's settings. When a settings
    frame is emitted by a peer it cannot assume that the new settings values
    are in place until the remote peer acknowledges the setting. In principle,
    multiple settings changes can be "in flight" at the same time, all with
    different values.

    This object encapsulates this mess. It provides a dict-like interface to
    settings, which return the *current* values pf the settings in question.
    Additionally, it keeps track of the stack of proposed values: each time an
    acknowledgement is sent/received, it updates the current values with the
    stack of proposed values.

    Finally, this object understands what the default values of the HTTP/2
    settings are, and sets those defaults appropriately.
    """
    def __init__(self, client=True):
        # Backing object for the settings. This is a dictionary of
        # (setting: [list of values]), where the first value in the list is the
        # current value of the setting. Strictly this doesn't use lists but
        # instead uses collections.deque to avoid repeated memory allocations.
        #
        # This contains the default values for HTTP/2.
        self._settings = {
            SettingsFrame.HEADER_TABLE_SIZE: collections.deque([4096]),
            SettingsFrame.ENABLE_PUSH: collections.deque([int(client)]),
            SettingsFrame.INITIAL_WINDOW_SIZE: collections.deque([65535]),
            SettingsFrame.SETTINGS_MAX_FRAME_SIZE: collections.deque([16384]),
        }

    def acknowledge(self):
        """
        The settings have been acknowledged, either by the user (remote
        settings) or by the remote peer (local settings).

        :returns: A dict of {setting: ChangedSetting} that were applied.
        """
        changed_settings = {}

        # If there is more than one setting in the list, we have a setting
        # value outstanding. Update them.
        for k, v in self._settings.items():
            if len(v) > 1:
                old_setting = v.popleft()
                new_setting = v[0]
                changed_settings[k] = ChangedSetting(
                    k, old_setting, new_setting
                )

        return changed_settings

    # Provide easy-access to well known settings.
    @property
    def header_table_size(self):
        """
        The current value of the SETTINGS_HEADER_TABLE_SIZE setting.
        """
        return self[SettingsFrame.HEADER_TABLE_SIZE]

    @header_table_size.setter
    def header_table_size(self, value):
        self[SettingsFrame.HEADER_TABLE_SIZE] = value

    @property
    def enable_push(self):
        """
        The current value of the SETTINGS_ENABLE_PUSH setting.
        """
        return self[SettingsFrame.ENABLE_PUSH]

    @enable_push.setter
    def enable_push(self, value):
        self[SettingsFrame.ENABLE_PUSH] = value

    @property
    def initial_window_size(self):
        """
        The current value of the SETTINGS_INITIAL_WINDOW_SIZE setting.
        """
        return self[SettingsFrame.INITIAL_WINDOW_SIZE]

    @initial_window_size.setter
    def initial_window_size(self, value):
        self[SettingsFrame.INITIAL_WINDOW_SIZE] = value

    @property
    def max_frame_size(self):
        """
        The current value of the SETTINGS_MAX_FRAME_SIZE setting.
        """
        return self[SettingsFrame.SETTINGS_MAX_FRAME_SIZE]

    @max_frame_size.setter
    def max_frame_size(self, value):
        self[SettingsFrame.SETTINGS_MAX_FRAME_SIZE] = value

    @property
    def max_concurrent_streams(self):
        """
        The current value of the SETTINGS_MAX_CONCURRENT_STREAMS setting.
        """
        return self.get(SettingsFrame.MAX_CONCURRENT_STREAMS, 2**32+1)

    @max_concurrent_streams.setter
    def max_concurrent_streams(self, value):
        self[SettingsFrame.MAX_CONCURRENT_STREAMS] = value

    # Implement the MutableMapping API.
    def __getitem__(self, key):
        val = self._settings[key][0]

        # Things that were created when a setting was received should stay
        # KeyError'd.
        if val is None:
            raise KeyError

        return val

    def __setitem__(self, key, value):
        try:
            items = self._settings[key]
        except KeyError:
            items = collections.deque([None])
            self._settings[key] = items

        items.append(value)

    def __delitem__(self, key):
        del self._settings[key]

    def __iter__(self):
        return self._settings.__iter__()

    def __len__(self):
        return len(self._settings)
