from agentboard import cli


def _kw(labels, cmd=None, manual=False):
    return cli._card_kwargs("t", "b", labels, 0, None, None, None, None, None, manual, 3, None, None, cmd, None)


def test_decision_label_makes_card_manual():
    assert _kw({"kind": "decision", "owner": "someone"})["runnable"] is False


def test_plain_card_stays_runnable():
    assert _kw({"owner": "someone"})["runnable"] is True
    assert _kw({})["runnable"] is True
