# window.py
#
# Copyright 2025 Wartybix
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

from gi.repository import Adw, Gtk, Gio, GLib
from constrict.constrict_utils import compress, get_encode_settings, get_resolution, get_framerate, get_duration
import threading

class StagedVideo:
    def __init__(self, filepath, row, width, height, fps, duration):
        self.filepath = filepath
        self.row = row
        self.suffix = None
        self.width, self.height = width, height
        self.fps = fps
        self.duration = duration

    def clear_suffix(self):
        if self.suffix:
            self.row.remove(self.suffix)

        self.suffix = None

    def set_suffix(self, suffix):
        self.clear_suffix()

        self.row.add_suffix(suffix)
        self.suffix = suffix

    def __string__(self):
        return f'{self.filepath} - {self.width}×{self.height}@{self.fps} ({self.duration}s)'


def preview(target_size_MiB, fps_mode, width, height, fps, duration):
    encode_settings = get_encode_settings(
        target_size_MiB,
        fps_mode,
        width,
        height,
        fps,
        duration
    )

    if not encode_settings:
        return ''

    _, _, preset_height, target_fps = encode_settings

    if height > width:
        height = width

    return f'{height}p@{fps} → {preset_height}p@{target_fps}'


@Gtk.Template(resource_path='/com/github/wartybix/Constrict/window.ui')
class ConstrictWindow(Adw.ApplicationWindow):
    __gtype_name__ = 'ConstrictWindow'

    split_view = Gtk.Template.Child()
    view_stack = Gtk.Template.Child()
    export_button = Gtk.Template.Child()
    video_queue = Gtk.Template.Child()
    add_videos_button = Gtk.Template.Child()
    target_size_row = Gtk.Template.Child()
    target_size_input = Gtk.Template.Child()
    auto_row = Gtk.Template.Child()
    auto_check_button = Gtk.Template.Child()
    clear_row = Gtk.Template.Child()
    clear_check_button = Gtk.Template.Child()
    smooth_row = Gtk.Template.Child()
    smooth_check_button = Gtk.Template.Child()
    codec_dropdown = Gtk.Template.Child()
    extra_quality_toggle = Gtk.Template.Child()
    tolerance_row = Gtk.Template.Child()
    tolerance_input = Gtk.Template.Child()

    open_action = Gio.SimpleAction(name="open")
    export_action = Gio.SimpleAction(name="export")
    clear_all_action = Gio.SimpleAction(name="clear_all")

    staged_videos = []

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        toggle_sidebar_action = Gio.SimpleAction(name="toggle-sidebar")
        toggle_sidebar_action.connect("activate", self.toggle_sidebar)
        self.add_action(toggle_sidebar_action)

        self.open_action.connect("activate", self.open_file_dialog)
        self.add_action(self.open_action)

        self.export_action.connect("activate", self.export_file_dialog)
        self.export_action.set_enabled(bool(self.staged_videos))
        self.add_action(self.export_action)

        self.clear_all_action.connect("activate", self.delist_all)
        self.add_action(self.clear_all_action)

        self.target_size_input.connect("value-changed", self.refresh_previews)
        self.auto_check_button.connect("activate", self.refresh_previews)
        self.clear_check_button.connect("activate", self.refresh_previews)
        self.smooth_check_button.connect("activate", self.refresh_previews)

    def set_controls_lock(self, is_locked):
        self.target_size_row.set_sensitive(not is_locked)
        self.auto_row.set_sensitive(not is_locked)
        self.clear_row.set_sensitive(not is_locked)
        self.smooth_row.set_sensitive(not is_locked)
        self.codec_dropdown.set_sensitive(not is_locked)
        self.extra_quality_toggle.set_sensitive(not is_locked)
        self.tolerance_row.set_sensitive(not is_locked)

        self.clear_all_action.set_enabled(not is_locked)
        self.export_action.set_enabled(not is_locked)

    def refresh_previews(self, _):
        for video in self.staged_videos:
            target_size = int(self.target_size_input.get_value())
            fps_mode = self.get_fps_mode()

            subtitle = preview(
                target_size,
                fps_mode,
                video.width,
                video.height,
                video.fps,
                video.duration
            )
            video.row.set_subtitle(subtitle)

    def get_fps_mode(self):
        if self.auto_check_button.get_active():
            return 'auto'
        if self.clear_check_button.get_active():
            return 'prefer-clear'
        if self.smooth_check_button.get_active():
            return 'prefer-smooth'

        raise Exception('Tried to get fps mode, but none was set.')

    def toggle_sidebar(self, action, _):
        sidebar_shown = self.split_view.get_show_sidebar()
        self.split_view.set_show_sidebar(not sidebar_shown)

    def delist_all(self, action, _):
        for video in self.staged_videos:
            self.video_queue.remove(video.row)

        self.staged_videos = []

        self.view_stack.set_visible_child_name('status_page')
        self.export_action.set_enabled(False)

    def export_file_dialog(self, action, parameter):
        native = Gtk.FileDialog()
        native.select_folder(self, None, self.on_export_response)

    def on_export_response(self, dialog, result):
        folder = dialog.select_folder_finish(result)

        if not folder:
            return

        folder_path = folder.get_path()
        print(folder_path)

        thread = threading.Thread(target=self.bulk_compress, args=[folder_path])
        thread.daemon = True
        thread.start()

    def bulk_compress(self, destination):
        self.set_controls_lock(True)

        codecs = ['h264', 'hevc', 'av1', 'vp9']

        target_size = int(self.target_size_input.get_value())
        fps_mode = self.get_fps_mode()
        codec = codecs[self.codec_dropdown.get_selected()]
        extra_quality = self.extra_quality_toggle.get_active()
        tolerance = int(self.tolerance_input.get_value())

        for video in self.staged_videos:
            # compressing_text = Gtk.Label.new('Compressing…')
            progress_bar = Gtk.ProgressBar()
            progress_bar.set_valign(Gtk.Align['CENTER'])
            progress_bar.set_show_text(True)
            if codec == 'vp9':
                progress_bar.set_text('Analyzing…')
            video.set_suffix(progress_bar)

            def update_progress(fraction):
                print(f'progress updated - {round(fraction * 100)}%')
                if fraction == 0.0 and codec == 'vp9':
                    GLib.idle_add(progress_bar.pulse)
                    print('pulsed')
                else:
                    if progress_bar.get_text():
                        GLib.idle_add(progress_bar.set_text, None)
                    GLib.idle_add(progress_bar.set_fraction, fraction)

            # def update_txt: compressing_text.set_label

            compress(
                video.filepath,
                target_size,
                fps_mode,
                extra_quality,
                codec,
                tolerance,
                destination,
                update_progress
            )

            complete_text = Gtk.Label.new('Complete')
            complete_text.add_css_class('success')

            video.set_suffix(complete_text)

        self.set_controls_lock(False)

    def open_file_dialog(self, action, parameter):
        # Create new file selection dialog, using "open" mode
        native = Gtk.FileDialog()
        video_filter = Gtk.FileFilter()

        video_filter.add_mime_type('video/*')
        video_filter.set_name('Videos')

        native.set_default_filter(video_filter)
        native.set_title('Pick Videos')

        native.open_multiple(self, None, self.on_open_response)

    def on_open_response(self, dialog, result):
        files = dialog.open_multiple_finish(result)

        if not files:
            return

        self.video_queue.remove(self.add_videos_button)

        existing_paths = list(map(lambda x: x.filepath, self.staged_videos))
        print(existing_paths)

        for video in files:
            # TODO: make async query?
            video_path = video.get_path()

            if video_path in existing_paths:
                continue

            info = video.query_info('standard::display-name', Gio.FileQueryInfoFlags.NONE)
            display_name = info.get_display_name() if info else video.get_basename()
            print(f'{video.get_basename()} - {video_path}')

            # TODO: Add thumbnail -- I think Nautilus generates one from a
            # frame 1/3 through the video

            target_size = int(self.target_size_input.get_value())
            fps_mode = self.get_fps_mode()

            action_row = Adw.ActionRow()
            action_row.set_title(display_name)

            # cache metadata
            width, height = get_resolution(video)
            fps = get_framerate(video)
            duration = get_duration(video)

            subtitle = preview(target_size, fps_mode, width, height, fps, duration)
            action_row.set_subtitle(subtitle)

            self.video_queue.add(action_row)

            staged_video = StagedVideo(video.get_path(), action_row, width, height, fps, duration)
            self.staged_videos.append(staged_video)

        self.video_queue.add(self.add_videos_button)

        if self.staged_videos:
            self.view_stack.set_visible_child_name('queue_page')

        self.export_action.set_enabled(True)
        self.export_button.grab_focus()

        print(self.staged_videos)

