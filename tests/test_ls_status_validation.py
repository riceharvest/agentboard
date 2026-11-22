import pytest


@pytest.fixture
def card(abx):
    rc, _, _ = abx("add", "a card")
    assert rc == 0


def test_unknown_single_value_rc2(abx, card):
    rc, out, err = abx("ls", "--status", "failed")
    assert rc == 2 and out.strip() == "" and "failed" in err and "cancelled" in err


def test_unknown_member_in_list_rc2(abx, card):
    rc, out, err = abx("ls", "--status", "open,bogus")
    assert rc == 2 and "bogus" in err


@pytest.mark.parametrize("v", ["open", "claimed", "done", "dead", "cancelled", "all", "open,claimed"])
def test_valid_values_rc0(abx, card, v):
    rc, _, _ = abx("ls", "--status", v)
    assert rc == 0


def test_empty_status_is_no_filter_like_all(abx, card):
    rc, out, _ = abx("ls", "--status", "")
    rc2, out2, _ = abx("ls", "--status", "all")
    assert rc == 0 and out == out2 and "a card" in out
