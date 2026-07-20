# Trivial passing suite so `ratchet.py score` produces a real, stable pass_set
# for this fixture repo. The junit classname is the module name (path-independent),
# so the derived case ids are identical across tmp copies and platforms.
# Uniquely named to avoid a basename collision with the parent repo's own tests.


def test_widget_addition():
    assert 1 + 1 == 2


def test_widget_identity():
    assert "widget" == "widget"
