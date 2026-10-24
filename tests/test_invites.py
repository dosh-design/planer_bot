"""One-time invites: who can get into a couple and who can't."""
import datetime as dt
import threading

import pytest

import db
import planner


@pytest.fixture
def clock(monkeypatch):
    t = {"now": dt.datetime(2026, 10, 22, 9, 0)}
    monkeypatch.setattr(db, "local_now", lambda: t["now"])
    return t


@pytest.fixture
def masha():
    db.save_user(1, "Маша")
    planner.add_text(1, "Ты обещал вкрутить лампочку")  # something private in her couple
    return db.user(1)["couple_id"]


def test_valid_invite_joins_and_is_spent(masha):
    code = db.invite_code(1)
    db.save_user(2, "Дима")
    assert db.join(2, code) == "ok"
    assert db.partner(2)["name"] == "Маша" and db.user(2)["couple_id"] == masha
    assert db.join(2, code) == "self"  # pressing the link again is harmless


def test_used_invite_lets_nobody_else_in(masha):
    code = db.invite_code(1)
    db.save_user(2, "Дима")
    db.join(2, code)
    db.save_user(3, "Посторонний")
    assert db.join(3, code) == "used"
    assert db.user(3)["couple_id"] is None or db.user(3)["couple_id"] != masha
    assert db.partner(3) is None


def test_invite_survives_partner_leaving_only_as_used(masha):
    """After the partner leaves, the old link must not reopen the couple to whoever still has it."""
    code = db.invite_code(1)
    db.save_user(2, "Дима")
    db.join(2, code)
    db.delete_user(2)
    db.save_user(3, "Посторонний")
    assert db.join(3, code) == "used"
    fresh = db.invite_code(1)  # Маша can invite again with a new link
    assert fresh and fresh != code and db.join(3, fresh) == "ok"


def test_expired_invite(masha, clock):
    code = db.invite_code(1)
    clock["now"] += dt.timedelta(hours=db.INVITE_HOURS, minutes=1)
    db.save_user(2, "Дима")
    assert db.join(2, code) == "expired"
    assert db.invite_code(1) != code  # asking again gives a new link


def test_new_link_revokes_the_old_one(masha):
    old = db.invite_code(1)
    assert db.invite_code(1) == old  # the mini-app asks on every open: the same active link
    new = db.invite_code(1, fresh=True)
    db.save_user(2, "Дима")
    assert db.join(2, old) == "revoked"
    assert db.join(2, new) == "ok"


def test_unknown_and_old_style_codes(masha):
    db.save_user(2, "Дима")
    assert db.join(2, "nope") == "unknown"
    old_style = db.couple_code(1)  # the couple's internal code used to be the invite
    assert db.join(2, old_style) == "unknown"


def test_someone_in_a_couple_is_not_switched_silently(masha):
    db.save_user(2, "Дима")
    db.join(2, db.invite_code(1))
    db.save_user(3, "Аня")
    db.save_user(4, "Петя")
    db.join(4, db.invite_code(3))  # Аня and Петя are a couple
    db.save_user(5, "Оля")
    code = db.invite_code(5)
    assert db.join(3, code) == "taken"
    assert db.partner(3)["name"] == "Петя" and db.partner(5) is None


def test_full_couple_gives_no_invite(masha):
    db.save_user(2, "Дима")
    db.join(2, db.invite_code(1))
    assert db.invite_code(1) is None and db.invite_code(2) is None


def test_two_people_racing_for_one_link(masha):
    code = db.invite_code(1)
    for uid in (2, 3):
        db.save_user(uid, f"u{uid}")
    results, barrier = {}, threading.Barrier(2)

    def go(uid):
        barrier.wait()
        results[uid] = db.join(uid, code)

    threads = [threading.Thread(target=go, args=(uid,)) for uid in (2, 3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results.values()) == ["ok", "used"]
    assert len(db.members(masha)) == 2


def test_solo_items_follow_into_the_couple(masha):
    db.save_user(2, "Дима")
    planner.add_text(2, "купить пиво")
    db.join(2, db.invite_code(1))
    assert [r["text"] for r in db.items(masha, "buy")] == ["Пиво"]
