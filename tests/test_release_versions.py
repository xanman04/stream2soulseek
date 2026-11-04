import pytest

from s2s.release_versions import Version, is_newer, parse_version


def test_parses_optional_v_prefix_and_build_metadata():
    assert parse_version("v1.2.3") == Version(1, 2, 3)
    assert parse_version("1.2.3+build.7") == parse_version("1.2.3+other")


@pytest.mark.parametrize("value", [
    "", "1", "1.2", "1.2.3.4", "01.2.3", "1.02.3", "1.2.03",
    "1.2.3-01", "1.2.3-alpha.01", "1.2.3-", "1.2.3+", "1.2.3+build..x",
    " 1.2.3", "1.2.3 ", "V1.2.3", "1.2.3-α",
])
def test_rejects_malformed_or_leading_zero_versions(value):
    with pytest.raises(ValueError):
        parse_version(value)


def test_prerelease_precedence_uses_numeric_identifiers():
    versions = [
        parse_version("1.7.0-rc.1"),
        parse_version("1.7.0-rc.2"),
        parse_version("1.7.0-rc.10"),
        parse_version("1.7.0"),
    ]
    assert versions == sorted(versions)


def test_prerelease_identifier_semver_ordering():
    assert parse_version("1.0.0-alpha") < parse_version("1.0.0-alpha.1")
    assert parse_version("1.0.0-1") < parse_version("1.0.0-alpha")


def test_build_metadata_does_not_change_precedence():
    assert parse_version("2.0.0+one") == parse_version("2.0.0+two")
    assert hash(parse_version("2.0.0+one")) == hash(parse_version("2.0.0+two"))
    assert not is_newer("2.0.0+two", "2.0.0+one")


@pytest.mark.parametrize("candidate,current,expected", [
    ("v1.7.0", "1.6.9", True),
    ("1.7.0-rc.2", "1.7.0-rc.1", True),
    ("1.7.0-rc.10", "1.7.0-rc.2", True),
    ("1.7.0", "1.7.0-rc.10", True),
    ("1.7.0-rc.1", "1.7.0", False),
    ("bad", "1.0.0", False),
    ("1.0.0", "bad", False),
])
def test_is_newer(candidate, current, expected):
    assert is_newer(candidate, current) is expected
