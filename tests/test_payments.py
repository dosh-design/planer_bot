"""Payments: a sum on a task, its monthly repeat, the edit, the bot's line."""
import datetime as dt

import pytest

import db
import parser
import planner
from test_web import client, init_data  # noqa: F401  (client is a fixture)

H1 = {"X-Init-Data": init_data(1, "Маша")}
DAY = dt.date(2026, 11, 3)


@pytest.mark.parametrize("phrase, text, amount", [
    ("оплатить интернет 650 ₽ каждый месяц 10 числа", "Оплатить интернет", 650),
    ("заплатить за садик 12 500 руб до пятницы", "Заплатить за садик", 12500),
    ("квартплата 7,5 тыс 20 числа", "Квартплата", 7500),
    ("оплатить кредит 15000р", "Оплатить кредит", 15000),
    ("вернуть долг Саше 3 тыс рублей", "Вернуть долг Саше", 3000),
])
def test_sum_is_read_and_taken_out_of_the_title(phrase, text, amount):
    [it] = parser.parse(phrase, DAY, allow_model=False)
    assert (it.kind, it.text, it.amount) == ("task", text, amount)


@pytest.mark.parametrize("phrase", ["позвонить в 18:00", "купить молоко 2 штуки", "встреча 15 ноября", "купить хлеб за 60 ₽"])
def test_numbers_that_are_not_money(phrase):
    assert all(not it.amount for it in parser.parse(phrase, DAY, allow_model=False))


def test_day_of_month_alone_is_the_nearest_one():
    [it] = parser.parse("оплатить свет до 25 числа", DAY, allow_model=False)
    assert (it.text, it.date) == ("Оплатить свет", "2026-11-25")
    [it] = parser.parse("квартплата 2-го", DAY, allow_model=False)
    assert it.date == "2026-12-02"


def test_monthly_payment_keeps_its_sum_next_month(couple, monkeypatch):
    monkeypatch.setattr(db, "local_now", lambda: dt.datetime(2026, 11, 3, 9, 0))
    rows, _ = planner.add_text(1, "оплатить интернет 650 ₽ каждый месяц 10 числа")
    assert rows[0]["amount"] == 650 and "650" in planner.line(rows[0])
    planner.complete(1, rows[0]["id"])
    nxt = [r for r in db.items(couple) if r["text"] == "Оплатить интернет"]
    assert [(r["day"], r["amount"]) for r in nxt] == [("2026-12-10", 650)]


async def test_sum_can_be_set_and_removed_in_the_app(client, couple):  # noqa: F811
    r = await (await client.post("/api/add", json={"text": "оплатить садик до пятницы"}, headers=H1)).json()
    iid = r["items"][0]["id"]
    assert r["items"][0]["amount"] is None
    assert (await client.patch(f"/api/items/{iid}", json={"amount": 4200}, headers=H1)).status == 200
    st = await (await client.get("/api/state", headers=H1)).json()
    assert next(i for i in st["items"] if i["id"] == iid)["amount"] == 4200
    for bad in (-5, 0, "много", 1.5, True, 10**10):
        assert (await client.patch(f"/api/items/{iid}", json={"amount": bad}, headers=H1)).status == 400
    assert (await client.patch(f"/api/items/{iid}", json={"amount": None}, headers=H1)).status == 200
    st = await (await client.get("/api/state", headers=H1)).json()
    assert next(i for i in st["items"] if i["id"] == iid)["amount"] is None
