"""Whole exports of small generated clips, on both engines: size, frame rate, length, colour and overlays."""
import base64
import io

import pytest
from PIL import Image

from conftest import RATE, export, frame, info, level, make_video, samples

GREY, RED = (128, 128, 128), (255, 0, 0)


def picture(size, colour):
    """An overlay as the page sends it: a PNG data URL the size of the export."""
    data = io.BytesIO()
    Image.new("RGBA", size, colour).save(data, "PNG")
    return "data:image/png;base64," + base64.b64encode(data.getvalue()).decode()


def close(a, b, by=6):
    return all(abs(x - y) <= by for x, y in zip(a, b))


@pytest.fixture(scope="module")
def grey(tmp_path_factory):
    return make_video(tmp_path_factory.mktemp("export") / "grey.mp4", seconds=4)


def test_size_rate_and_length_are_kept(work, engine, grey):
    assert info(export(work, [(grey, 0.5, 2.5)])) == (320, 180, "30/1", 60, pytest.approx(2, abs=0.05))


def test_grade_that_changes_nothing_keeps_the_colour(work, engine, grey):
    assert close(frame(export(work, [(grey, 0, 1)]), 0.5), GREY, by=3)


def test_mono_clip_sound_keeps_its_level(work, engine):
    out = export(work, [(make_video(work / "v.mp4", tone=300, mono=True), 0, 3)])
    assert level(samples(out)[RATE:2 * RATE], 300) == pytest.approx(0.125, abs=0.004)


def test_other_frame_rates_are_kept(work, engine):
    assert info(export(work, [(make_video(work / "v.mp4", fps="60000/1001", seconds=2), 0, 1)]))[2] == "60000/1001"


@pytest.mark.parametrize("turn", [90, 180, 270])
def test_clip_stored_turned_comes_out_upright(work, engine, turn):
    out = export(work, [(make_video(work / "v.mp4", picture="testsrc2", seconds=2, turn=turn), 0, 1)])
    assert info(out)[:2] == ((180, 320) if turn % 180 else (320, 180))


def test_clips_are_joined_end_to_end(work, engine, grey):
    red = make_video(work / "red.mp4", picture="color=c=red", seconds=2)
    out = export(work, [(grey, 0, 1.5), (red, 0, 1)])
    assert info(out)[3:] == (75, pytest.approx(2.5, abs=0.05))
    assert close(frame(out, 1.2), GREY) and frame(out, 1.8)[0] > 200 > 60 > frame(out, 1.8)[1]


def test_overlay_shows_only_between_its_times(work, engine, grey):
    out = export(work, [(grey, 0, 3)], overlays=[{"png": picture((320, 180), RED + (255,)), "at": 1, "length": 1, "fade": 0}])
    assert close(frame(out, 0.5), GREY) and close(frame(out, 2.5), GREY)
    assert frame(out, 1.5)[0] > 200 > 60 > frame(out, 1.5)[1]


def test_overlay_fades_in_and_out(work, engine, grey):
    out = export(work, [(grey, 0, 3)], overlays=[{"png": picture((320, 180), (255, 255, 255, 255)), "at": 1, "length": 1.5, "fade": 0.5}])
    rising, full, falling = (frame(out, t)[1] for t in (1.25, 1.75, 2.25))
    assert full > 245 and 150 < rising < 230 and 150 < falling < 230


def test_half_transparent_overlay_mixes_with_the_picture(work, engine, grey):
    out = export(work, [(grey, 0, 2)], overlays=[{"png": picture((320, 180), (255, 255, 255, 128)), "at": 0, "length": 2, "fade": 0}])
    assert close(frame(out, 1), (192, 192, 192), by=8)


def test_overlay_runs_across_a_cut(work, engine, grey):
    out = export(work, [(grey, 0, 1), (grey, 2, 3)], overlays=[{"png": picture((320, 180), RED + (255,)), "at": 0.5, "length": 1, "fade": 0}])
    reds = [frame(out, t)[0] > 200 for t in (0.25, 0.75, 1.25, 1.75)]
    assert reds == [False, True, True, False]
