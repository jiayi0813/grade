"""The grade maths and the .cube files it is saved as."""
import numpy as np

import grade

GREYS = np.linspace(0, 1, 11)[:, None].repeat(3, 1)


def test_untouched_look_changes_nothing():
    rng = np.random.default_rng(1)
    rgb = rng.random((500, 3))
    assert np.abs(grade.look_fn()(rgb) - rgb).max() < 1e-3


def test_cube_file_reads_back_the_same(tmp_path):
    fn = grade.look_fn(exposure=0.5, contrast=1.2, saturation=0.8, temp=0.02)
    grade.write_cube(tmp_path / "look.cube", fn)
    assert np.abs(grade.read_cube(tmp_path / "look.cube") - grade.as_grid(grade.build_lut(fn))).max() < 1e-6


def test_lut_applies_the_look_it_was_built_from(tmp_path):
    fn = grade.look_fn(exposure=0.5, contrast=1.1)
    rng = np.random.default_rng(2)
    rgb = rng.random((500, 3)) * 0.6 + 0.2  # away from the clipped ends, where a LUT cannot follow a kink
    lut = grade.as_grid(grade.build_lut(fn))
    assert np.abs(grade.apply_lut(rgb, lut) - np.clip(fn(rgb), 0, 1)).max() < 0.01


def test_exposure_and_saturation_move_the_right_way():
    assert (grade.look_fn(exposure=1)(GREYS[1:-1]) > GREYS[1:-1]).all()
    red = np.array([[0.7, 0.3, 0.3]])
    spread = lambda rgb: float(rgb.max() - rgb.min())
    assert spread(grade.look_fn(saturation=0)(red)) < 0.01 < spread(red) < spread(grade.look_fn(saturation=1.5)(red))
