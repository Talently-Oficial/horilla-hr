"""Tests for leave approve balance deduction and sufficiency gate."""

from datetime import date, timedelta

from django.test import TestCase


class LeaveRequestApproveBalanceTests(TestCase):
    def setUp(self):
        from horilla.testkit import make_company, make_employee
        from leave.models import AvailableLeave, LeaveType

        company = make_company("Approve Bal Co")
        self.employee = make_employee(company=company, email="approvebal@test.horilla")
        self.leave_type = LeaveType.objects.create(
            name="Approve Balance Leave",
            total_days=20,
        )
        self.AvailableLeave = AvailableLeave

    def _request(self, days):
        from leave.models import LeaveRequest

        today = date.today()
        return LeaveRequest(
            employee_id=self.employee,
            leave_type_id=self.leave_type,
            start_date=today + timedelta(days=3),
            end_date=today + timedelta(days=3),
            start_date_breakdown="full_day",
            end_date_breakdown="full_day",
            requested_days=days,
            description="approve balance test",
            status="requested",
            approved_available_days=0,
            approved_carryforward_days=0,
        )

    def test_has_sufficient_leave_balance_gate(self):
        from leave.services import has_sufficient_leave_balance

        avail = self.AvailableLeave(
            available_days=2,
            carryforward_days=1,
        )
        self.assertTrue(has_sufficient_leave_balance(avail, 3))
        self.assertFalse(has_sufficient_leave_balance(avail, 4))

    def test_approve_reduces_available_days(self):
        avail = self.AvailableLeave.objects.create(
            employee_id=self.employee,
            leave_type_id=self.leave_type,
            available_days=5,
            carryforward_days=0,
            total_leave_days=5,
        )
        req = self._request(3)
        req.no_approval()
        avail.refresh_from_db()
        self.assertEqual(avail.available_days, 2)
        self.assertEqual(req.approved_available_days, 3)
        self.assertEqual(req.status, "approved")

    def test_approve_uses_carryforward_first(self):
        # Carryforward (last year's days) expires first, so it goes first.
        avail = self.AvailableLeave.objects.create(
            employee_id=self.employee,
            leave_type_id=self.leave_type,
            available_days=1,
            carryforward_days=4,
            total_leave_days=5,
        )
        req = self._request(3)
        req.no_approval()
        avail.refresh_from_db()
        self.assertEqual(avail.available_days, 1)
        self.assertEqual(avail.carryforward_days, 1)
        self.assertEqual(req.approved_available_days, 0)
        self.assertEqual(req.approved_carryforward_days, 3)
        self.assertEqual(req.status, "approved")

    def test_approve_spills_from_carryforward_into_available(self):
        avail = self.AvailableLeave.objects.create(
            employee_id=self.employee,
            leave_type_id=self.leave_type,
            available_days=5,
            carryforward_days=2,
            total_leave_days=7,
        )
        req = self._request(3)
        req.no_approval()
        avail.refresh_from_db()
        self.assertEqual(avail.carryforward_days, 0)
        self.assertEqual(avail.available_days, 4)
        self.assertEqual(req.approved_carryforward_days, 2)
        self.assertEqual(req.approved_available_days, 1)

    def test_api_approve_uses_carryforward_first(self):
        # Same rule on the API path the Slack bot uses.
        from horilla_api.api_views.leave.views import LeaveRequestApproveAPIView

        avail = self.AvailableLeave.objects.create(
            employee_id=self.employee,
            leave_type_id=self.leave_type,
            available_days=5,
            carryforward_days=2,
            total_leave_days=7,
        )
        req = self._request(3)
        LeaveRequestApproveAPIView().leave_approve_calculation(req, avail)
        avail.refresh_from_db()
        self.assertEqual(avail.carryforward_days, 0)
        self.assertEqual(avail.available_days, 4)
        self.assertEqual(req.approved_carryforward_days, 2)
        self.assertEqual(req.approved_available_days, 1)

    def test_insufficient_balance_gate_blocks(self):
        from leave.services import has_sufficient_leave_balance

        avail = self.AvailableLeave.objects.create(
            employee_id=self.employee,
            leave_type_id=self.leave_type,
            available_days=1,
            carryforward_days=0,
            total_leave_days=1,
        )
        self.assertFalse(has_sufficient_leave_balance(avail, 3))
        # Balances unchanged when gate fails (caller must not approve).
        avail.refresh_from_db()
        self.assertEqual(avail.available_days, 1)


class ProjectedVacationBalanceTests(TestCase):
    """
    Vacation (an accrual type) may be requested up to the balance it will have
    on the start date: today's balance plus the monthly credits still to come.
    """

    def setUp(self):
        from employee.models import EmployeeWorkInformation
        from horilla.testkit import make_company, make_employee
        from leave.models import AvailableLeave, LeaveType

        company = make_company("Projected Bal Co")
        self.employee = make_employee(company=company, email="projbal@test.horilla")
        # Joined today: credits fall one, two, three... months from now.
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            date_joining=date.today()
        )
        self.employee.employee_work_info.refresh_from_db()
        self.vacation = LeaveType.objects.create(name="Vacaciones", total_days=0)
        self.other = LeaveType.objects.create(name="Approve Balance Leave", total_days=0)
        self.vacation_balance = AvailableLeave.objects.create(
            employee_id=self.employee,
            leave_type_id=self.vacation,
            available_days=2,
            carryforward_days=0,
        )
        self.other_balance = AvailableLeave.objects.create(
            employee_id=self.employee,
            leave_type_id=self.other,
            available_days=2,
            carryforward_days=0,
        )

    def _request(self, leave_type, days, offset):
        # LeaveRequest.save() recomputes requested_days from the dates, so the
        # range spans `days` calendar days.
        from leave.models import LeaveRequest

        start = date.today() + timedelta(days=offset)
        return LeaveRequest(
            employee_id=self.employee,
            leave_type_id=leave_type,
            start_date=start,
            end_date=start + timedelta(days=days - 1),
            start_date_breakdown="full_day",
            end_date_breakdown="full_day",
            requested_days=days,
            description="projected balance test",
            status="requested",
            approved_available_days=0,
            approved_carryforward_days=0,
        )

    def test_limit_adds_the_credits_still_to_come_up_to_the_start_date(self):
        from employee.models import EmployeeWorkInformation
        from leave.services import leave_balance_limit

        # Flor: joined Jul 6, 5.49 days on Oct 13.
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            date_joining=date(2026, 7, 6)
        )
        self.vacation_balance.employee_id.employee_work_info.refresh_from_db()
        self.vacation_balance.available_days = 5.49
        today = date(2026, 10, 13)
        # Nov 6, Dec 6 and Jan 6 (the start date itself) are still to come.
        self.assertAlmostEqual(
            leave_balance_limit(self.vacation_balance, date(2027, 1, 6), today), 10.98
        )
        self.assertAlmostEqual(
            leave_balance_limit(self.vacation_balance, date(2026, 12, 1), today), 7.32
        )
        self.assertAlmostEqual(
            leave_balance_limit(self.vacation_balance, date(2026, 10, 20), today), 5.49
        )
        # Other types and calls without a date keep today's balance.
        self.assertEqual(
            leave_balance_limit(self.other_balance, date(2027, 1, 6), today), 2
        )
        self.assertAlmostEqual(leave_balance_limit(self.vacation_balance), 5.49)

    def test_gate_uses_the_projected_balance(self):
        from leave.services import has_sufficient_leave_balance

        far = date.today() + timedelta(days=95)  # three credits away: 2 + 5.49
        near = date.today() + timedelta(days=10)  # no credit yet
        self.assertTrue(has_sufficient_leave_balance(self.vacation_balance, 7, far))
        self.assertFalse(has_sufficient_leave_balance(self.vacation_balance, 8, far))
        self.assertFalse(has_sufficient_leave_balance(self.vacation_balance, 3, near))
        self.assertFalse(has_sufficient_leave_balance(self.other_balance, 3, far))

    def test_approve_leaves_the_deficit_as_negative_available_days(self):
        from horilla_api.api_views.leave.views import LeaveRequestApproveAPIView

        req = self._request(self.vacation, 5, offset=95)
        LeaveRequestApproveAPIView().leave_approve_calculation(
            req, self.vacation_balance
        )
        self.vacation_balance.refresh_from_db()
        self.assertEqual(self.vacation_balance.available_days, -3)
        self.assertEqual(self.vacation_balance.total_leave_days, 0)
        self.assertEqual(req.approved_available_days, 5)

    def test_api_approve_serializer_uses_the_projected_balance(self):
        from horilla_api.api_serializers.leave.serializers import (
            LeaveRequestApproveSerializer,
        )

        within = self._request(self.vacation, 5, offset=95)
        within.save()
        self.assertTrue(LeaveRequestApproveSerializer(within, data={}).is_valid())

        beyond = self._request(self.vacation, 8, offset=110)
        beyond.save()
        self.assertFalse(LeaveRequestApproveSerializer(beyond, data={}).is_valid())

        other = self._request(self.other, 5, offset=130)
        other.save()
        self.assertFalse(LeaveRequestApproveSerializer(other, data={}).is_valid())

    def test_api_create_validation_uses_the_projected_balance(self):
        # The path the Slack bot uses to create requests.
        from types import SimpleNamespace

        from rest_framework import serializers

        from horilla_api.api_serializers.leave.serializers import leave_Validations

        stub = SimpleNamespace(instance=None)

        def data(leave_type, days, offset):
            start = date.today() + timedelta(days=offset)
            return {
                "employee_id": self.employee,
                "leave_type_id": leave_type,
                "start_date": start,
                "end_date": start + timedelta(days=days - 1),
            }

        leave_Validations(stub, data(self.vacation, 5, 95))
        with self.assertRaises(serializers.ValidationError):
            leave_Validations(stub, data(self.vacation, 8, 95))
        with self.assertRaises(serializers.ValidationError):
            leave_Validations(stub, data(self.vacation, 5, 10))
        with self.assertRaises(serializers.ValidationError):
            leave_Validations(stub, data(self.other, 5, 95))
