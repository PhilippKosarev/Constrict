#!/usr/bin/python3

# constrict_utils.py
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

import sys
import subprocess
import os
import argparse
import re
from pathlib import Path
from tempfile import TemporaryFile
from typing import List, Optional, Tuple, Callable
try:
    from constrict.enums import FpsMode, VideoCodec
except ModuleNotFoundError:
    from enums import FpsMode, VideoCodec


# Module responsible for compression logic. This script can be packaged
# on its own to provide a CLI compressor *only*. The GTK wrapper depends on
# this script for its 'business logic' too.


def get_duration(file_input: str) -> float:
    return float(
        subprocess.check_output([
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            file_input
        ])[:-1]
    )


def get_res_preset(
    bitrate: int,
    source_width: int,
    source_height: int,
    framerate: float
) -> int:
    """
    Returns a suitable resolution preset (i.e. 1080p, 720p, etc.) from a given
    bitrate and source resolution. Allows source videos to be shrunk according
    to a new, reduced bitrate for optimal perceived video quality. This
    function should not return a resolution preset larger than the source
    resolution (i.e. an upscaled or stretched resolution).

    -If -1 is returned, then the video's source resolution is recommended.
    """

    source_pixels = source_width * source_height  # Get pixel count
    bitrate_Kbps = bitrate / 1000  # Convert to kilobits
    """
    Bitrate-resolution recommendations are taken from:
    https://developers.google.com/media/vp9/settings/vod
    """
    bitrate_res_map_30 = {
        12000: (3840, 2160),  # 4K
        6000: (2560, 1440),  # 2K
        1800: (1920, 1080),  # 1080p
        1024: (1280, 720),  # 720p
        512: (640, 480),  # 480p
        276: (640, 360),  # 360p
        150: (320, 240),  # 240p
        0: (192, 144)  # 144p
    }
    bitrate_res_map_60 = {
        18000: (3840, 2160),  # 4K
        9000: (2560, 1440),  # 2K
        3000: (1920, 1080),  # 1080p
        1800: (1280, 720),  # 720p
        750: (640, 480),  # 480p
        276: (640, 360),  # 360p
        150: (320, 240),  # 240p
        0: (192, 144)  # 144p
    }

    bitrate_res_map = (
        bitrate_res_map_30 if framerate <= 30 else bitrate_res_map_60
    )

    for bitrate_lower_bound, res_preset in bitrate_res_map.items():
        preset_width, preset_height = res_preset[0], res_preset[1]
        preset_pixels = preset_width * preset_height
        if (
            bitrate_Kbps >= bitrate_lower_bound and
            source_pixels >= preset_pixels
        ):
            return preset_height

    portrait = source_height > source_width
    return source_width if portrait else source_height


def get_encoding_speed(
    frame_height: int,
    codec: int,
    extra_quality: bool
) -> str:
    hd = frame_height > 480

    match codec:
        case VideoCodec.H264:
            if extra_quality:
                return 'veryslow'
            else:
                return 'medium' if hd else 'slower'
        case VideoCodec.HEVC:
            if extra_quality:
                return 'veryslow'
            else:
                return 'medium' if hd else 'slow'
        case VideoCodec.AV1:
            if extra_quality:
                return '4'
            else:
                return '10' if hd else '8'
        case VideoCodec.VP9:
            if extra_quality:
                return '0'
            else:
                return '5' if hd else '4'

        case _:
            sys.exit('Error: unknown codec passed to get_encoding_speed')

# Returns null if there's no problem while getting progress of an ffmpeg
# operation. If there's an error, the error details will be returned.
def get_progress(
    file_input: str,
    ffmpeg_cmd: List[str],
    output_fn: Callable[[float, Optional[int]], None],
    frame_count: int,
    pass_num: Optional[int],
    last_pass_avg_fps: Optional[float],
    cancel_event: Callable
) -> Tuple[Optional[float], Optional[str]]:
    with TemporaryFile() as err_file:
        proc = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=err_file
        )

        frame = 0
        fps_sum = 0.0
        pulse_counter = 0
        avg_counter = 0

        if proc.stdout is not None:
            for line in proc.stdout:
                line_string = line.decode('utf-8')

                if re.search('^frame=.*$', line_string):
                    frame_match = re.search('[0-9]+', line_string)
                    if frame_match:
                        frame = int(frame_match.group())
                elif re.search('^fps=.*$', line_string):
                    total_frames = frame_count * (1 if pass_num is None else 2)
                    current_frame = frame_count * (pass_num or 0) + frame
                    progress_fraction = current_frame / total_frames
                    pulse_counter += 1

                    if pulse_counter < 10 or (pass_num == 1 and pulse_counter < 20):
                        # The first few frames of a pass are kind of
                        # unpredictable. The average FPS is anomalously low
                        # compared before it starts to 'warm up' to a
                        # relatively consistent value. Therefore, we don't
                        # display the time remaining on the first few frames of
                        # the first pass, and we just use the last pass'
                        # average FPS to calculate time remaining on the first
                        # few frames of the second pass.

                        # We are slightly more lenient on the first pass, since
                        # it's more important the user can see the estimated
                        # time earlier on. We are more careful with pass 2,
                        # because it suddenly makes the estimated time look
                        # jumpy and inconsistent once progress reaches 50%, if
                        # using anomalous FPS values.
                        fps = last_pass_avg_fps
                    else:
                        fps_match = re.search('[0-9]+[.]?[0-9]*', line_string)
                        if fps_match:
                            fps = float(fps_match.group())

                            avg_counter += 1
                            fps_sum += fps

                    frames_left = total_frames - current_frame

                    seconds_left = None

                    if fps:
                        seconds_left = int(frames_left // fps)
                        if seconds_left < 0:
                            seconds_left = 0

                    output_fn(progress_fraction, seconds_left)

                if cancel_event() == True:
                    proc.kill()
                    return (None, None)

        avg = fps_sum / avg_counter if avg_counter else None

        proc.wait()
        returncode = proc.poll()

        if returncode != 0:
            err_file.flush()
            err_file.seek(0, 0)

            errors = err_file.read()
            decoded = errors.decode('utf-8')

            return (avg, decoded)

        return (avg, None)


# Returns null if there's no problem with transcoding.
# If there's an error while transcoding, it'll return with the details of the
# error.
def transcode(
    file_input: str,
    file_output: str,
    video_bitrate: int,
    audio_bitrate: int,
    width: int,
    height: int,
    framerate: float,
    codec: int,
    extra_quality: bool,
    output_fn: Callable[[float, Optional[int]], None],
    frame_count: int,
    log_path: str,
    cancel_event: Callable[[], bool]
) -> Optional[str]:
    portrait = height > width
    frame_height = width if portrait else height

    preset_name = '-cpu-used' if codec == VideoCodec.VP9 else '-preset'
    preset = get_encoding_speed(frame_height, codec, extra_quality)

    cv_params = {
        VideoCodec.H264: 'libx264',
        VideoCodec.HEVC: 'libx265',
        VideoCodec.AV1: 'libsvtav1',
        VideoCodec.VP9: 'libvpx-vp9'
    }

    pass1_cmd = [
        'ffmpeg',
        '-y',
        '-progress', '-',
        '-i', f'{file_input}',
        f'{preset_name}', f'{"4" if codec == VideoCodec.VP9 else preset}',
        '-vf', f'scale={width}:{height}',
    ]

    if log_path is not None:
        pass1_cmd.extend(['-passlogfile', f'{log_path}'])

    if codec == VideoCodec.VP9:
        pass1_cmd.extend([
            '-deadline', 'good',
            '-row-mt', '1',
            '-frame-parallel', '1'
        ])

    if codec == VideoCodec.H264:
        pass1_cmd.extend(['-profile:v', 'main'])

    if framerate != -1:
        pass1_cmd.extend(['-r', f'{framerate}'])

    pass1_cmd.extend([
        '-c:v', f'{cv_params[codec]}',
        '-b:v', str(video_bitrate) + '',
        '-pix_fmt', 'yuv420p',
        '-pass', '1',
        '-an',
        '-f', 'null',
        '/dev/null'
    ])

    if cancel_event():
        return None

    avg_fps, progress_error = get_progress(
        file_input,
        pass1_cmd,
        output_fn,
        frame_count,
        None if codec == VideoCodec.VP9 else 0,
        None,
        cancel_event
    )

    if progress_error != None:
        return progress_error

    audio_channels = 1 if audio_bitrate < 12000 else 2

    pass2_cmd = [
        'ffmpeg',
        '-y',
        '-progress', '-',
        '-i', f'{file_input}',
        f'{preset_name}', f'{preset}',
        '-vf', f'scale={width}:{height}',
    ]

    if log_path is not None:
        pass2_cmd.extend(['-passlogfile', f'{log_path}'])

    if codec == VideoCodec.VP9:
        pass2_cmd.extend([
            '-deadline', 'good',
            '-row-mt', '1',
            '-frame-parallel', '1'
        ])

    if codec == VideoCodec.H264:
        pass2_cmd.extend(['-profile:v', 'main'])

    if framerate != -1:
        pass2_cmd.extend(['-r', f'{framerate}'])

    pass2_cmd.extend([
        '-c:v', f'{cv_params[codec]}',
        '-b:v', str(video_bitrate) + '',
        '-pix_fmt', 'yuv420p',
        '-pass', '2',
        '-c:a', 'libopus',
        '-b:a', f'{audio_bitrate}',
        '-ac', f'{audio_channels}',
        file_output
    ])

    if cancel_event():
        return None

    avg_fps, progress_error = get_progress(
        file_input,
        pass2_cmd,
        output_fn,
        frame_count,
        None if codec == VideoCodec.VP9 else 1,
        avg_fps,
        cancel_event
    )

    if progress_error != None:
        return progress_error

    return None


def get_framerate(file_input: str) -> float:
    cmd = [
        'ffprobe',
        '-v', '0',
        '-of',
        'default=noprint_wrappers=1:nokey=1',
        '-select_streams', 'v:0',
        '-show_entries',
        'stream=r_frame_rate',
        file_input
    ]
    fps_bytes = subprocess.check_output(cmd)
    fps_fraction = fps_bytes.decode('utf-8')
    fps_fraction_split = fps_fraction.split('/')
    fps_numerator = int(fps_fraction_split[0])
    fps_denominator = int(fps_fraction_split[1])
    fps_float = fps_numerator / fps_denominator
    return fps_float


def get_resolution(file_input: str) -> Tuple[int, int]:
    cmd = [
        'ffprobe',
        '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height',
        '-of', 'csv=s=x:p=0',
        file_input
    ]

    res_bytes = subprocess.check_output(cmd)
    res = res_bytes.decode('utf-8')
    res_array = res.split('x')
    width = int(res_array[0])
    height = int(res_array[1])

    return (width, height)


def get_rotation(file_input: str) -> int:
    cmd = [
        'ffprobe',
        '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream_side_data=rotation',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        file_input
    ]

    rotation_bytes = subprocess.check_output(cmd)

    try:
        rotation = int(rotation_bytes.decode('utf-8'))
    except ValueError:
        rotation = 0

    return rotation

def get_frame_count(file_input: str) -> int:
    cmd = [
        'ffprobe',
        '-v', 'error',
        '-select_streams', 'v:0',
        '-count_packets',
        '-show_entries', 'stream=nb_read_packets',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        file_input
    ]

    frame_count_bytes = subprocess.check_output(cmd)

    try:
        frame_count = int(frame_count_bytes.decode('utf-8'))
    except ValueError:
        frame_count = 1

    return frame_count

def get_encode_settings(
    target_size_MiB: int,
    fps_mode: int,
    width: int,
    height: int,
    fps: float,
    duration: float,
    factor: float = 1.0
) -> Tuple[int, int, int, float]:
    target_size_KiB = target_size_MiB * 1024
    target_size_bytes = target_size_KiB * 1024
    target_size_bits = target_size_bytes * 8

    target_bitrate = round(target_size_bits / duration) * factor

    # To account for metadata and such to prevent overshooting
    target_bitrate = round(target_bitrate * 0.99)

    '''
    crush mode tries to save some image clarity by significantly reducing audio
    quality and introducing a 24 FPS framerate cap. This makes the footage look
    slightly less blurry at 144p, and can sometimes save it from being
    downgraded to 144p as a preset resolution due to the boost in video
    bitrate.

    Why is the threshold 150 + 96 (= 246)? It's the sum of the lowest
    recommended bitrate for 240p (150Kbps), plus a 'good quality' bitrate for
    Opus audio (96Kbps). It means that it shouldn't be possible to 'downgrade'
    the video to 144p without applying crush mode. Additionally, footage can be
    'saved' from being downgraded to 144p where, for example:

    Total target bitrate = 200Kbps
    Bitrate less than threshold, therefore apply crush mode.
    Target audio bitrate set to 6Kbps (rather than 96Kbps) due to crush mode.
    Therefore, video bitrate is 194Kbps
    This is *above* 150Kbps, therefore preset resolution is 240p@24

    And if there was no crush mode:
    Total target bitrate = 200Kbps
    Target audio bitrate set to 96Kbps
    Therefore, video bitrate is 104Kbps
    This is *below* 150Kbps, therefore preset resolution is 144p@?
    '''
    crush_mode = (target_bitrate / 1000) < 150 + 96
    target_audio_bitrate = 6000 if crush_mode else 96000
    target_video_bitrate = target_bitrate - target_audio_bitrate

    preset_height = None
    max_fps = 60.0

    if crush_mode:
        max_fps = 24.0
    elif fps_mode == FpsMode.PREFER_CLEAR:
        max_fps = 30.0
    elif fps_mode == FpsMode.PREFER_SMOOTH:
        max_fps = 60.0
    elif fps_mode == FpsMode.AUTO:
        preset_height_30fps = get_res_preset(
            target_video_bitrate,
            width,
            height,
            30.0
        )
        preset_height_60fps = get_res_preset(
            target_video_bitrate,
            width,
            height,
            60.0
        )

        preset_height = preset_height_30fps
        heights_match = preset_height_30fps == preset_height_60fps
        max_fps = 60.0 if heights_match and preset_height >= 720 else 30.0

    target_fps = fps if fps <= max_fps else max_fps

    if preset_height is None:
        preset_height = get_res_preset(
            target_video_bitrate,
            width,
            height,
            target_fps
        )

    return (
        target_video_bitrate,
        target_audio_bitrate,
        preset_height,
        target_fps
    )


# Returns None if compression went smoothly.
# If there's an error while compressing, it'll return compression details.
# TODO: make error msgs translatable
def compress(
    file_input: str,
    file_output: str,
    target_size_MiB: int,
    framerate_option: int,
    extra_quality: bool,
    codec: int,
    tolerance: int,
    output_fn: Callable[[float, Optional[int]], None],
    log_path: str,
    cancel_event: Callable,
    on_new_attempt: Callable[[int, int, bool, int, float], None],
    on_attempt_fail: Callable[[int, int, bool, int, float, int, int], None]
) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    output_fn(0, None)

    target_size_bytes = target_size_MiB * 1024 * 1024
    before_size_bytes = os.stat(file_input).st_size
    after_size_bytes = 0

    if before_size_bytes <= target_size_bytes:
        return (None, None, "Constrict: File already meets the target size.")

    try:
        duration_seconds = get_duration(file_input)
        source_fps = get_framerate(file_input)
        width, height = get_resolution(file_input)
        source_frame_count = get_frame_count(file_input)
        portrait = (width < height) ^ (get_rotation(file_input) == -90)
    except subprocess.CalledProcessError:
        return (None, None, "Constrict: Could not retrieve video properties. Source video may be missing or corrupted.")

    try:
        Path(file_output).touch(exist_ok=False)
    except FileExistsError:
        # This should never reasonably happen if a unique file name has been
        # passed to this function as file_output.
        return (None, None, "Constrict: Could not create exported file. A file with the reserved name already exists.")
    except PermissionError:
        return (None, None, "Constrict: Could not create exported file. There are insufficient permissions to create a file at the requested export path.")

    # initialise values
    factor = 1.0
    attempt = 0
    percent_of_target = 200.0

    target_video_bitrate = 0
    target_audio_bitrate = 0
    target_height = 0
    target_fps = 0.0
    is_hq_audio = False

    while (percent_of_target < 100 - tolerance) or (percent_of_target > 100):
        if attempt > 0:
            on_attempt_fail(
                attempt,
                target_video_bitrate,
                is_hq_audio,
                target_height,
                target_fps,
                after_size_bytes,
                target_size_bytes
            )

        attempt += 1

        encode_settings = get_encode_settings(
            target_size_MiB,
            framerate_option,
            width,
            height,
            source_fps,
            duration_seconds,
            factor
        )

        target_video_bitrate, target_audio_bitrate, target_height, target_fps = encode_settings

        is_hq_audio = target_audio_bitrate > 48000

        on_new_attempt(
            attempt,
            target_video_bitrate,
            is_hq_audio,
            target_height,
            target_fps
        )
        output_fn(0, None)

        # Below 5 Kbps, barely anything is perceptible in the video anymore.
        if target_video_bitrate < 5000:
            return (None, None, "Constrict: Video bitrate got too low (<5 Kbps). The target size may be too low for this file.")

        scaling_factor = height / target_height
        target_width = int(((width / scaling_factor + 1) // 2) * 2)

        if portrait:
            # Swap height and width
            buffer = target_width
            target_width = target_height
            target_height = buffer

        displayed_res = target_width if portrait else target_height

        dest_frame_count = int(source_frame_count // (source_fps / target_fps))

        transcode_error = transcode(
            file_input,
            file_output,
            target_video_bitrate,
            target_audio_bitrate,
            target_width,
            target_height,
            target_fps,
            codec,
            extra_quality,
            output_fn,
            dest_frame_count,
            log_path,
            cancel_event
        )

        if transcode_error != None:
            return (None, None, transcode_error)

        if cancel_event():
            return (None, None, None)

        try:
            after_size_bytes = os.stat(file_output).st_size
        except FileNotFoundError:
            return (None, None, "Constrict: Cannot read output file. Was it moved or deleted mid-compression?")
        percent_of_target = (100 / target_size_bytes) * after_size_bytes

        factor *= 100 / percent_of_target

        if (percent_of_target > 100):
            # Prevent a lot of attempts resulting in above-target sizes
            factor *= 0.95

    return (file_output, after_size_bytes, None)

