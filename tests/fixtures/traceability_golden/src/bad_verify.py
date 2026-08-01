# A 'verifies' from a non-test file is an invalid link (verifies must come from
# test/probe/policy/telemetry, not plain code) — CTR-order-002 stays untested.
# @cw-trace verifies CTR-order-002
def not_a_test():
    ...
