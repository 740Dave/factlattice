"""Build credential-SHAPED test strings at runtime.

A literal such as an API-key prefix followed by a long token is exactly what a secret scanner (and a
public host's push protection) looks for, whether or not it is real. The reviewer tests need strings
of those shapes to prove the detector catches them, so they assemble them from short fragments here:
the shape exists in memory when the test runs and in no tracked file.
"""


def j(*parts):
    return "".join(parts)
