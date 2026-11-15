"""The export's sound mix: the clips' own sound plus added tracks. Each source is a tone of its
own pitch, so its level in the result can be read on its own."""
import pytest

import app
from conftest import RATE, level, make_sound, make_video, onsets, samples


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("audio")
    return {"video": make_video(d / "v.mp4", seconds=9, tone=300),
            "tone": make_sound(d / "tone.wav", "sine=frequency=880:duration=9"),
            # half a second of tone, then silence: 2 s long
            "beep": make_sound(d / "beep.wav", "sine=frequency=880:duration=0.5,apad=whole_dur=2"),
            "late": make_sound(d / "late.wav", "sine=frequency=880:duration=0.5,adelay=1000:all=1,apad=whole_dur=3"),
            "mono": make_sound(d / "mono.wav", "sine=frequency=880:duration=9", mono=True),
            "loud": make_sound(d / "loud.wav", "aevalsrc=0.9*sin(2*PI*880*t):d=9")}


def mixed(media, tmp_path, video_volume, **track):
    out = tmp_path / "mix.mp4"
    app.mix(str(media["video"]), out, [{"path": str(media[track.pop("file", "tone")]), "at": 0, "volume": 1, **track}], video_volume)
    return samples(out)


@pytest.mark.parametrize("video, track", [(1, 1), (1, 0), (0, 1), (1, 0.5), (0.25, 1), (0, 0)])
def test_each_volume_sets_only_its_own_source(media, tmp_path, video, track):
    x = mixed(media, tmp_path, video, volume=track)[RATE:4 * RATE]
    assert level(x, 300) == pytest.approx(0.125 * video, abs=0.004)  # ffmpeg's sine is 1/8 of full scale
    assert level(x, 880) == pytest.approx(0.125 * track, abs=0.004)


def test_mono_track_is_as_loud_as_a_stereo_one(media, tmp_path):
    assert level(mixed(media, tmp_path, 0, file="mono")[RATE:4 * RATE], 880) == pytest.approx(0.125, abs=0.004)


def test_mix_is_as_long_as_the_video(media, tmp_path):
    assert len(mixed(media, tmp_path, 1, file="beep", loop=True)) / RATE == pytest.approx(9, abs=0.1)


def test_track_starts_where_it_is_placed(media, tmp_path):
    assert onsets(mixed(media, tmp_path, 0, file="beep", at=1.5)) == pytest.approx([1.5], abs=0.03)


def test_track_dragged_past_the_start_loses_its_beginning(media, tmp_path):
    assert onsets(mixed(media, tmp_path, 0, file="late", at=-0.7)) == pytest.approx([0.3], abs=0.03)


def test_track_plays_once_unless_looped(media, tmp_path):
    assert onsets(mixed(media, tmp_path, 0, file="beep", at=1)) == pytest.approx([1], abs=0.03)
    assert onsets(mixed(media, tmp_path, 0, file="beep", at=1, loop=True)) == pytest.approx([1, 3, 5, 7], abs=0.03)


def test_looped_track_dragged_past_the_start_keeps_its_rhythm(media, tmp_path):
    assert onsets(mixed(media, tmp_path, 0, file="beep", at=-0.7, loop=True)) == pytest.approx([1.3, 3.3, 5.3, 7.3], abs=0.03)


def test_loud_tracks_are_limited_not_clipped(media, tmp_path):
    out = tmp_path / "mix.mp4"
    app.mix(str(media["video"]), out, [{"path": str(media["loud"]), "at": 0, "volume": 1}] * 2, 1)
    # the three sources add up to 1.9 of full scale. The first second is left out: AAC overshoots
    # for a few hundredths of a second where a full-level tone starts from nothing.
    assert 0.9 < abs(samples(out)[RATE:]).max() <= 1.0
