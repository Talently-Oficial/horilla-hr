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

TODAY = date(2026, 10, 5)


def _balance(available, carryforward=0):
    return SimpleNamespace(available_days=available, carryforward_days=carryforward)


def _periods(balance, joined, approved=()):
    return accrual_periods(
        balance, today=TODAY, date_joining=joined, approved=[(d, days, 1) for d, days in approved]
    )


class AccrualPeriodTests(SimpleTestCase):
    def test_credits_start_the_month_after_joining(self):
        joined = date(2025, 8, 20)
        self.assertEqual(accrual_credits_in_year(joined, 2025, TODAY), 4)  # Sep-Dec
        self.assertEqual(accrual_credits_in_year(joined, 2026, TODAY), 9)  # Jan-Sep 20

    def test_credit_on_short_month_uses_last_day(self):
        # Feb 28 counts as the February anniversary of a Jan 31 joiner.
        self.assertEqual(accrual_credits_in_year(date(2026, 1, 31), 2026, date(2026, 2, 28)), 1)

    def test_no_joining_date_means_no_credits(self):
        self.assertEqual(accrual_credits_in_year(None, 2026, TODAY), 0)

    def test_periods_oldest_first_and_rows_close(self):
        periods = _periods(_balance(16.47, 3.32), date(2025, 8, 20), [(date(2025, 12, 24), 4)])
        last, this = periods["rows"]
        self.assertEqual((last["year"], last["label"]), (2025, "last year"))
        self.assertEqual((last["earned"], last["used"], last["available"]), (7.32, 4, 3.32))
        self.assertEqual(last["expires"], "Jan 1, 2027")
        self.assertEqual((this["year"], this["label"]), (2026, "this year"))
        self.assertEqual((this["earned"], this["used"], this["available"]), (16.47, 0, 16.47))
        self.assertEqual(this["expires"], "Jan 1, 2028")
        self.assertEqual(periods["total"], {"earned": 23.79, "used": 4, "available": 19.79})

    def test_requests_use_the_oldest_period_still_valid(self):
        # In March 2026 the 2024 days have expired, so the 2025 days go first.
        periods = _periods(_balance(33.43), date(2024, 1, 10), [(date(2026, 3, 2), 5)])
        last, this = periods["rows"]
        self.assertEqual((last["year"], last["used"]), (2025, 5))
        self.assertEqual(this["used"], 0)

    def test_used_ignores_how_the_balance_is_split(self):
        # Joined in 2024, all 30 remaining days sitting in available_days.
        periods = _periods(_balance(30, 0), date(2024, 1, 10))
        last, this = periods["rows"]
        self.assertEqual((last["earned"], last["used"], last["available"]), (21.96, 0, 21.96))
        # Policy says 38.43 left; the 8.43 the balance lacks shows as used this year.
        self.assertEqual((this["earned"], this["used"], this["available"]), (16.47, 8.43, 8.04))
        self.assertEqual(periods["total"]["available"], 30)

    def test_allocation_outside_policy_shows_as_earned_not_negative_used(self):
        # 5 credits (9.15) plus a 3-day allocation approved by HR.
        periods = _periods(_balance(12.15), date(2026, 4, 14))
        (this,) = periods["rows"]
        self.assertEqual((this["earned"], this["used"], this["available"]), (12.15, 0, 12.15))

    def test_days_asked_beyond_the_balance_are_owed_this_year(self):
        periods = _periods(_balance(-2.85), date(2026, 4, 14), [(date(2026, 11, 2), 12)])
        (this,) = periods["rows"]
        self.assertEqual((this["earned"], this["used"], this["available"]), (9.15, 12, -2.85))

    def test_new_hire_has_no_last_year_row(self):
        periods = _periods(_balance(9.15), date(2026, 4, 14))
        self.assertEqual([row["year"] for row in periods["rows"]], [2026])

    def test_format_long_date_is_english(self):
        self.assertEqual(format_long_date(date(2027, 1, 1)), "Jan 1, 2027")


class CardKindTests(SimpleTestCase):
    def _type(self, name, limit_leave=True, compensatory=False):
        return SimpleNamespace(
            name=name, limit_leave=limit_leave, reset=False, is_compensatory_leave=compensatory
        )

    def test_only_configured_vacation_types_get_the_period_table(self):
        self.assertEqual(leave_card_kind(self._type("Vacaciones")), "accrual")
        # Same config as vacation (model defaults), but not vacation.
        self.assertEqual(
            leave_card_kind(self._type("Compensatory Leave Type", compensatory=True)), "limited"
        )
        self.assertEqual(leave_card_kind(self._type("Licencia especial")), "limited")
        self.assertEqual(leave_card_kind(self._type("Otro", limit_leave=False)), "unlimited")
        self.assertEqual(leave_card_kind(None), "limited")

    def test_titles_in_english_with_fallback(self):
        self.assertEqual(leave_card_title(self._type("Vacaciones")), "Vacation")
        self.assertEqual(leave_card_title(self._type("Ausencia médica")), "Medical leave")
        self.assertEqual(leave_card_title(self._type("Licencia especial")), "Licencia especial")
        self.assertEqual(leave_card_title(None), "-")

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
        self.compensatory = LeaveType.objects.create(
            name="Compensatory Leave Type", is_compensatory_leave=True
        )
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
        self.compensatory_balance = AvailableLeave.objects.create(
            employee_id=self.employee,
            leave_type_id=self.compensatory,
            available_days=2,
            carryforward_days=0,
        )

    def test_cards_render_table_for_vacation_and_no_limit_for_unlimited(self):
        html = render_to_string(
            "leave/user_leave/user_leave.html",
            {"user_leaves": [self.vacation_balance, self.medical_balance, self.compensatory_balance]},
        )
        self.assertIn("Vacation", html)
        self.assertEqual(html.count("Expires"), 1)  # only vacation gets the table
        self.assertIn(f"Jan 1, {date.today().year + 1}", html)
        self.assertIn("Medical leave", html)
        self.assertIn("No limit", html)
        self.assertIn("Total days taken", html)
        self.assertNotIn("100000", html)

    def test_balance_without_leave_type_does_not_break_the_list(self):
        from leave.models import AvailableLeave

        AvailableLeave.objects.filter(pk=self.compensatory_balance.pk).update(leave_type_id=None)
        orphan = AvailableLeave.objects.get(pk=self.compensatory_balance.pk)
        html = render_to_string(
            "leave/user_leave/user_leave.html",
            {"user_leaves": [self.vacation_balance, orphan]},
        )
        self.assertIn("Vacation", html)
        self.assertIn("Available leave days", html)

    def test_dashboard_balance_only_returns_vacation(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("employee-dashboard-balance"))
        self.assertEqual(response.status_code, 200)
        balances = response.json()["balances"]
        self.assertEqual([b["name"] for b in balances], ["Vacation"])
        self.assertEqual(balances[0]["available_days"], 7)
        self.assertGreaterEqual(balances[0]["used_days"], 0)
        self.assertEqual(balances[0]["expiring_on"], f"Jan 1, {date.today().year + 1}")
