from tools.semver_guard import Pep440Error, SemVer, pep440_from_git_describe


def test_release_tags_remain_semver_and_do_not_accept_pep440_dev_suffix() -> None:
    assert SemVer.parse("0.18.0-alpha.1").prerelease == ("alpha", "1")
    try:
        SemVer.parse("0.18.0.dev1")
    except ValueError:
        pass
    else:
        raise AssertionError("PEP 440 development suffix was accepted as a SemVer tag")


def test_git_describe_converts_semver_prerelease_to_pep440() -> None:
    assert pep440_from_git_describe("v0.18.0-alpha.1-0-gabc1234") == "0.18.0a1"
    assert pep440_from_git_describe("v0.18.0-alpha-1-2-gabc1234") == "0.18.0a1.dev2+gabc1234"
    assert pep440_from_git_describe("v0.17.0-62-gabc1234") == "0.17.1.dev62+gabc1234"
    assert pep440_from_git_describe("v0.17.0-62-gabc1234-dirty") == "0.17.1.dev62+gabc1234.dirty"


def test_git_describe_rejects_unrepresentable_prerelease() -> None:
    try:
        pep440_from_git_describe("v0.18.0-preview.1-0-gabc1234")
    except Pep440Error:
        pass
    else:
        raise AssertionError("unsupported SemVer prerelease was converted to a misleading PEP 440 version")
