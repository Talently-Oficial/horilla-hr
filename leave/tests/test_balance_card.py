"""Balance cards in My Leave Requests and the vacation donut: yearly periods and card kinds."""

from datetime import date, timedelta
from types import SimpleNamespace

from django.template import Context, Template
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from leave.services import (
    accrual_credits_in_year,
    accrual_periods,
    format_long_date,
    leave_card_kind,
    leave_card_title,
)


def _balance(available, carryforward, date_joining):
    return SimpleNamespace(
        available_days=available,
        carryforward_days=carryforward,
        employee_id=SimpleNamespace(
            employee_work_info=SimpleNamespace(date_joining=date_joining)
        ),
    )


class AccrualPeriodTests(SimpleTestCase):
    today = date(2026, 10, 5)

    def test_credits_start_the_month_after_joining(self):
        joined = date(2025, 8, 20)
        self.assertEqual(accrual_credits_in_year(joined, 2025, self.today), 4)  # Sep-Dec
        self.assertEqual(accrual_credits_in_year(joined, 2026, self.today), 9)  # Jan-Sep 20

    def test_credit_on_short_month_uses_last_day(self):
        joined = date(2026, 1, 31)
        # Feb 28 counts as the February anniversary.
        self.assertEqual(accrual_credits_in_year(joined, 2026, date(2026, 2, 28)), 1)

    def test_no_joining_date_means_no_credits(self):
        self.assertEqual(accrual_credits_in_year(None, 2026, self.today), 0)

    def test_periods_oldest_first_and_rows_close(self):
        periods = accrual_periods(_balance(16.47, 3.32, date(2025, 8, 20)), self.today)
        last, this = periods["rows"]
        self.assertEqual((last["year"], last["label"]), (2025, "last year"))
        self.assertEqual((last["earned"], last["used"], last["available"]), (7.32, 4.0, 3.32))
        self.assertEqual(last["expires"], "Jan 1, 2027")
        self.assertEqual((this["year"], this["label"]), (2026, "this year"))
        self.assertEqual((this["earned"], this["used"], this["available"]), (16.47, 0.0, 16.47))
        self.assertEqual(this["expires"], "Jan 1, 2028")
        self.assertEqual(periods["total"], {"earned": 23.79, "used": 4.0, "available": 19.79})

    def test_new_hire_has_no_last_year_row(self):
        periods = accrual_periods(_balance(9.15, 0, date(2026, 4, 14)), self.today)
        self.assertEqual([row["year"] for row in periods["rows"]], [2026])

    def test_negative_balance_shows_as_owed_in_used(self):
        periods = accrual_periods(_balance(-2.25, 0, date(2024, 8, 19)), self.today)
        this = periods["rows"][-1]
        self.assertEqual(this["available"], -2.25)
        self.assertEqual(this["used"], round(this["earned"] + 2.25, 2))

    def test_format_long_date_is_english(self):
        self.assertEqual(format_long_date(date(2027, 1, 1)), "Jan 1, 2027")


class CardKindTests(SimpleTestCase):
    def test_kind_follows_leave_type_config(self):
        self.assertEqual(leave_card_kind(SimpleNamespace(limit_leave=False, reset=False)), "unlimited")
        self.assertEqual(leave_card_kind(SimpleNamespace(limit_leave=True, reset=True)), "resetting")
        self.assertEqual(leave_card_kind(SimpleNamespace(limit_leave=True, reset=False)), "accrual")

    def test_titles_in_english_with_fallback(self):
        self.assertEqual(leave_card_title(SimpleNamespace(name="Vacaciones")), "Vacation")
        self.assertEqual(leave_card_title(SimpleNamespace(name="Ausencia médica")), "Medical leave")
        self.assertEqual(leave_card_title(SimpleNamespace(name="Licencia especial")), "Licencia especial")

    def test_leave_days_filter(self):
        out = Template(
            "{% load leave_balance %}{{ a|leave_days }} {{ b|leave_days }} {{ c|leave_days }} {{ d|leave_days }}"
        ).render(Context({"a": 16.47, "b": 4.0, "c": -2.25, "d": 0.0}))
        self.assertEqual(out, "16.47 4 -2.25 0")


class BalanceCardRenderTests(TestCase):
    def setUp(self):
        from employee.models import EmployeeWorkInformation
        from horilla.testkit import make_company, make_employee, make_user
        from leave.models import AvailableLeave, LeaveType

        company = make_company("Card Co")
        self.user = make_user("card_user", password="secret123")
        self.employee = make_employee(
            company=company, email="card_user@test.horilla", user=self.user
        )
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            date_joining=date.today() - timedelta(days=500)
        )
        self.vacation = LeaveType.objects.create(name="Vacaciones", total_days=0)
        self.medical = LeaveType.objects.create(name="Ausencia médica", limit_leave=False)
        self.vacation_balance = AvailableLeave.objects.create(
            employee_id=self.employee,
            leave_type_id=self.vacation,
            available_days=5,
            carryforward_days=2,
        )
        self.medical_balance = AvailableLeave.objects.create(
            employee_id=self.employee,
            leave_type_id=self.medical,
            available_days=100000,
            carryforward_days=0,
        )

    def test_cards_render_table_for_vacation_and_no_limit_for_unlimited(self):
        html = render_to_string(
            "leave/user_leave/user_leave.html",
            {"user_leaves": [self.vacation_balance, self.medical_balance]},
        )
        self.assertIn("Vacation", html)
        self.assertIn("Expires", html)
        self.assertIn(f"Jan 1, {date.today().year + 1}", html)
        self.assertIn("Medical leave", html)
        self.assertIn("No limit", html)
        self.assertIn("Total days taken", html)
        self.assertNotIn("100000", html)

    def test_dashboard_balance_only_returns_vacation(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("employee-dashboard-balance"))
        self.assertEqual(response.status_code, 200)
        balances = response.json()["balances"]
        self.assertEqual([b["name"] for b in balances], ["Vacation"])
        self.assertEqual(balances[0]["available_days"], 7)
        self.assertEqual(balances[0]["expiring_days"], 2)
        self.assertEqual(balances[0]["expiring_on"], f"Jan 1, {date.today().year + 1}")
