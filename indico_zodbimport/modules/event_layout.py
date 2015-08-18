# This file is part of Indico.
# Copyright (C) 2002 - 2015 European Organization for Nuclear Research (CERN).
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 3 of the
# License, or (at your option) any later version.
#
# Indico is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Indico; if not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

from operator import attrgetter

import binascii
import click
import mimetypes

from indico.modules.events.layout import layout_settings
from indico.modules.events.models.events import Event
from indico.util.console import cformat, verbose_iterator
from indico.util.fs import secure_filename
from indico.util.struct.iterables import committing_iterator
from indico_zodbimport import Importer, convert_to_unicode
from indico_zodbimport.util import get_archived_file

ALLOWED_THEMES = {'orange.css', 'brown.css', 'right_menu.css'}


class EventLayoutImporter(Importer):
    def __init__(self, **kwargs):
        self.archive_dirs = kwargs.pop('archive_dir')
        super(EventLayoutImporter, self).__init__(**kwargs)

    @staticmethod
    def decorate_command(command):
        command = click.option('--archive-dir', required=True, multiple=True,
                               help="The base path where resources are stored (ArchiveDir in indico.conf). "
                                    "When used multiple times, the dirs are checked in order until a file is "
                                    "found.")(command)
        return command

    def has_data(self):
        return bool(layout_settings.query.filter_by(name='is_searchable').count())

    def migrate(self):
        self.migrate_layout_settings()

    def migrate_layout_settings(self):
        print cformat('%{white!}migrating layout settings, event logos and custom CSS templates')

        default_styles = self.zodb_root['MaKaCInfo']['main']._styleMgr._defaultEventStylesheet
        for event, event_type, dmgr, logo, custom_css in committing_iterator(self._iter_event_layout_data()):
            if event_type != 'conference':
                theme = dmgr._defaultstyle
                if not theme or theme == default_styles[event_type]:
                    continue
                layout_settings.set(event, 'timetable_theme', theme)
                if not self.quiet:
                    self.print_success(cformat('- %{cyan}Default timetable theme: {}').format(theme), event_id=event.id)
                continue

            settings = self._get_event_settings(event, dmgr)
            layout_settings.set_multi(event, settings)
            if not self.quiet:
                self.print_success(cformat('- %{cyan}Layout settings'), event_id=event.id)
            if logo or custom_css:
                e = Event.get(event.id)
                if not e:
                    self.print_warning('Event does not exist (anymore)! Logo and/or CSS file not saved!',
                                       event_id=event.id)
                    continue
            if logo:
                path = get_archived_file(logo, self.archive_dirs)[1]
                if path is None:
                    self.print_error(cformat('%{red!}{} logo not found on disk; skipping it').format(event),
                                     event_id=event.id)
                    continue
                with open(path, 'rb') as f:
                    logo_content = f.read()
                logo_content_type = mimetypes.guess_type(logo.fileName)[0]
                logo_metadata = {
                    'size': len(logo_content),
                    'hash': binascii.crc32(logo_content) & 0xffffffff,
                    'filename': secure_filename(convert_to_unicode(logo.fileName), 'logo'),
                    'content_type': logo_content_type
                }
                e.logo = logo_content
                e.logo_metadata = logo_metadata
                if not self.quiet:
                    self.print_success(cformat('- %{cyan}[Logo] {}').format(logo.fileName), event_id=event.id)
            if custom_css:
                stylesheet = custom_css._localFile
                path = get_archived_file(stylesheet, self.archive_dirs)[1]
                if path is None:
                    self.print_error(cformat('%{red!}{} CSS file not found on disk; skipping it').format(event),
                                     event_id=event.id)
                    continue
                with open(path, 'rb') as f:
                    stylesheet_content = convert_to_unicode(f.read())
                e.stylesheet_metadata = {
                    'size': len(stylesheet_content),
                    'hash': binascii.crc32(stylesheet_content) & 0xffffffff,
                    'filename': secure_filename(convert_to_unicode(stylesheet.fileName), 'stylesheet.css'),
                    'content_type': 'text/css'
                }
                e.stylesheet = stylesheet_content
                if not self.quiet:
                    self.print_success(cformat('- %{cyan}[CSS] {}').format(stylesheet.fileName), event_id=event.id)

    def _iter_event_layout_data(self):
        it = self.zodb_root['conferences'].itervalues()
        if self.quiet:
            it = verbose_iterator(it, len(self.zodb_root['conferences']), attrgetter('id'), attrgetter('title'))
        wfr = self.zodb_root['webfactoryregistry']
        dmr = self.zodb_root['displayRegistery']
        for event in self.flushing_iterator(it):
            wf = wfr.get(event.id)
            event_type = 'conference' if wf is None else wf.id
            dmgr = dmr[event.id]
            style_mgr = getattr(dmgr, '_styleMngr', None) if event_type == 'conference' else None
            custom_css = getattr(style_mgr, '_css', None) if event_type == 'conference' else None
            yield event, event_type, dmgr, event._logo, custom_css

    def _get_event_settings(self, event, dmgr):
        format_opts = getattr(dmgr, '_format', None)
        tt = getattr(dmgr, '_tickerTape', None)
        style_mgr = getattr(dmgr, '_styleMngr', None)
        event_id = event.id
        menu = getattr(dmgr, '_menu', None)

        settings = {
            'is_searchable': getattr(dmgr, '_searchEnabled', None),
            'show_nav_bar': getattr(dmgr, '_displayNavigationBar', None),
            'show_social_badges': getattr(dmgr, '_showSocialApps', None),
        }
        if format_opts:
            settings['header_text_color'] = format_opts._data.get('titleTextColor')
            settings['header_background_color'] = format_opts._data.get('titleBgColor')
        else:
            self.print_error(cformat('%{red!} Skipping some settings, missing _format attribute'), event_id=event_id)
        if tt:
            settings['show_banner'] = getattr(tt, '_enabledNowPlaying', None)
            settings['announcement'] = getattr(tt, '_text', None)
            settings['show_announcement'] = getattr(tt, '_enabledSimpleText', None)
        else:
            self.print_error(cformat('%{red!} Skipping some settings, missing _tickerTape attribute'),
                             event_id=event_id)
        if style_mgr:
            template = getattr(style_mgr, '_usingTemplate', None)
            theme = getattr(template, 'templateId', None)
            settings['theme'] = theme if theme in ALLOWED_THEMES else None
            settings['use_custom_css'] = getattr(style_mgr, '_css', None) is not None
        else:
            self.print_error(cformat('%{red!} Skipping some settings, missing _styleMngr attribute'), event_id=event_id)
        if menu:
            settings['timetable_by_room'] = getattr(menu, '_timetable_layout', None) == 'room'
            settings['timetable_detailed'] = getattr(menu, '_timetable_detailed_view', False)
        else:
            self.print_error(cformat('%{red!} Skipping some settings, missing _menu attribute'), event_id=event_id)

        return {k: v for k, v in settings.iteritems() if v is not None}