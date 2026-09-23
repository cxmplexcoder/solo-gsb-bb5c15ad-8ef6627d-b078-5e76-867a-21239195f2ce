import pytest

from app.semverx import RangeSpec, SemverError, Version


def sat(version: str, spec: str) -> bool:
    return RangeSpec.parse(spec).satisfies(Version.parse(version))


class TestVersion:
    def test_basic_parse_and_compare(self):
        assert Version.parse("1.2.3").core == (1, 2, 3)
        assert Version.parse("2.0.0") > Version.parse("1.9.9")
        assert Version.parse("1.2.3") == Version.parse("v1.2.3")

    def test_prerelease_lower_than_release(self):
        assert Version.parse("1.0.0-alpha") < Version.parse("1.0.0")
        assert Version.parse("1.0.0-alpha.1") < Version.parse("1.0.0-alpha.2")
        assert Version.parse("1.0.0-alpha") < Version.parse("1.0.0-alpha.1")
        # 数字标识按数值比较
        assert Version.parse("1.0.0-alpha.2") < Version.parse("1.0.0-alpha.10")
        assert Version.parse("1.0.0-alpha") > Version.parse("1.0.0-aaa")

    def test_build_metadata_ignored(self):
        assert Version.parse("1.0.0+build.42") == Version.parse("1.0.0")

    def test_invalid(self):
        with pytest.raises(SemverError):
            Version.parse("1.2")
        with pytest.raises(SemverError):
            Version.parse("01.2.3")


class TestExactAndComparators:
    def test_exact(self):
        assert sat("1.2.3", "1.2.3")
        assert not sat("1.2.4", "1.2.3")

    def test_comparators(self):
        assert sat("2.0.0", ">=1.0.0")
        assert sat("1.0.0", ">=1.0.0")
        assert not sat("0.9.9", ">=1.0.0")
        assert sat("1.5.0", ">1.0.0 <2.0.0")
        assert sat("1.5.0", ">1.0.0, <2.0.0")
        assert not sat("2.0.0", ">1.0.0 <2.0.0")
        assert not sat("1.0.0", ">1.0.0 <2.0.0")
        assert sat("1.0.0", "<=1.0.0")


class TestCaretTilde:
    def test_caret(self):
        assert sat("1.2.9", "^1.2.3")
        assert not sat("2.0.0", "^1.2.3")
        assert not sat("1.1.9", "^1.2.3")
        assert sat("0.2.9", "^0.2.3")
        assert not sat("0.3.0", "^0.2.3")
        assert sat("0.0.3", "^0.0.3")
        assert not sat("0.0.4", "^0.0.3")

    def test_tilde(self):
        assert sat("1.2.9", "~1.2.3")
        assert not sat("1.3.0", "~1.2.3")
        assert sat("1.3.0", "~1")
        assert not sat("2.0.0", "~1.2")


class TestXRanges:
    def test_star(self):
        assert sat("99.99.99", "*")
        assert sat("0.0.1", "")

    def test_partial(self):
        assert sat("1.9.0", "1")
        assert not sat("2.0.0", "1")
        assert sat("1.2.9", "1.2")
        assert not sat("1.3.0", "1.2")
        assert sat("1.5.0", "1.x")
        assert sat("1.2.9", "1.2.x")

    def test_caret_partial(self):
        assert sat("1.9.0", "^1")
        assert not sat("2.0.0", "^1")
        assert sat("0.2.9", "^0.2")
        assert not sat("0.3.0", "^0.2")


class TestHyphen:
    def test_hyphen_full(self):
        assert sat("1.2.3", "1.2.3 - 2.3.4")
        assert sat("2.3.4", "1.2.3 - 2.3.4")
        assert not sat("2.3.5", "1.2.3 - 2.3.4")
        assert not sat("1.2.2", "1.2.3 - 2.3.4")

    def test_hyphen_partial_right(self):
        assert sat("1.2.9", "1.2.0 - 1.2")
        assert not sat("1.3.0", "1.2.0 - 1.2")
        assert sat("1.9.9", "1.2.0 - 1")
        assert not sat("2.0.0", "1.2.0 - 1")


class TestOr:
    def test_or_groups(self):
        assert sat("1.0.0", "^1.0.0 || ^2.0.0")
        assert sat("2.5.0", "^1.0.0 || ^2.0.0")
        assert not sat("3.0.0", "^1.0.0 || ^2.0.0")
        assert sat("1.0.0", "1.0.0 || 2.0.0")


class TestPrereleaseGates:
    def test_prerelease_not_matched_by_plain_range(self):
        assert not sat("1.5.0-rc.1", ">=1.0.0 <2.0.0")
        assert not sat("1.5.0-rc.1", "^1.0.0")
        assert not sat("1.0.0-rc", "*")
        assert not sat("1.0.0-rc", "1.x")

    def test_prerelease_matched_by_same_core(self):
        assert sat("1.5.0-rc.1", ">=1.5.0-rc.0 <2.0.0")
        assert not sat("1.4.0-rc.1", ">=1.5.0-rc.0 <2.0.0")
        assert sat("1.5.0-rc.1", "1.5.0-rc.1")
        assert not sat("1.5.0-rc.2", "1.5.0-rc.1")

    def test_invalid_spec(self):
        with pytest.raises(SemverError):
            RangeSpec.parse(">=1.x.3")
        with pytest.raises(SemverError):
            RangeSpec.parse("1.2.3.4")
