# Trivial passing suite so `ratchet.py score` produces a real, stable pass_set
# for this fixture repo. The junit classname is the module name (path-independent),
# so the derived case ids are identical across tmp copies and platforms.
# Uniquely named to avoid a basename collision with the parent repo's own tests.
#
# Both tests are ANNOTATED VERIFIER TESTS (#206), one per carrier (comment /
# docstring), so the fixture also exercises the verifier-hash dimension. The
# shared helper exists deliberately: weakening ITS body while the annotated
# test's own span is untouched is the dimension's documented no-fire boundary
# (evasion-config-indirection seed).


def _sum_holds(a, b, expected):
    return a + b == expected


# @cw-trace verifies CTR-rt-001
def test_widget_addition():
    assert _sum_holds(1, 1, 2)


def test_widget_identity():
    """@cw-trace verifies CTR-rt-002"""
    assert "widget" == "widget"
