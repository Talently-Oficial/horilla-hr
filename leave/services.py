"""
leave/services.py

Centralised business-logic helpers for the leave app.
Condition evaluation follows the same pattern as payroll allowance eligibility checks.
"""

import calendar
from datetime import date

from django.conf import settings
from django.db.models import Sum
from django.utils.translation import gettext_lazy as _


def evaluate_leave_type_conditions(leave_type, employee):
    """
    Evaluate all conditions configured on a LeaveType against an employee.

    Returns a (is_eligible, error_message) tuple.  When all conditions pass,
    returns (True, None).  On the first failing condition it returns
    (False, <translated error string>).

    Usage::

        is_eligible, msg = evaluate_leave_type_conditions(leave_type, employee)
        if not is_eligible:
            raise ValidationError(msg)
    """
    from leave.models import AvailableLeave

    for condition in leave_type.conditions.all():
        ctype = condition.condition_type

        if ctype == "gender":
            emp_gender = (getattr(employee, "gender", None) or "").lower()
            required_gender = (condition.value or "").lower()
            if emp_gender and required_gender and emp_gender != required_gender:
                return False, _(
                    "This leave type is restricted to {gender} employees only."
                ).format(gender=condition.value)

        elif ctype == "once_per_employment":
            already_assigned = AvailableLeave.objects.filter(
                employee_id=employee,
                leave_type_id=leave_type,
            ).exists()
            if already_assigned:
                return False, _(
                    "'{leave_type}' can only be assigned once per employment and has already been assigned to this employee."
                ).format(leave_type=leave_type.name)

        elif ctype == "marital_status":
            emp_status = (getattr(employee, "marital_status", None) or "").lower()
            required_status = (condition.value or "").lower()
            if emp_status and required_status and emp_status != required_status:
                return False, _(
                    "This leave type is restricted to employees with marital status: {status}."
                ).format(status=condition.value)

        elif ctype == "nationality":
            emp_country = (getattr(employee, "country", None) or "").lower()
            required_country = (condition.value or "").lower()
            if emp_country and required_country and emp_country != required_country:
                return False, _(
                    "This leave type is restricted to employees with nationality: {nationality}."
                ).format(nationality=condition.value)

        elif ctype == "department":
            dept = None
            work_info = getattr(employee, "employee_work_info", None)
            if work_info:
                dept_obj = getattr(work_info, "department_id", None)
                if dept_obj:
                    dept = str(dept_obj).lower()
            required_dept = (condition.value or "").lower()
            if dept and required_dept and dept != required_dept:
                return False, _(
                    "This leave type is restricted to employees in the {department} department."
                ).format(department=condition.value)

        elif ctype == "employment_type":
            emp_type = None
            work_info = getattr(employee, "employee_work_info", None)
            if work_info:
                emp_type_obj = getattr(work_info, "employee_type_id", None)
                if emp_type_obj:
                    emp_type = str(emp_type_obj).lower()
            required_type = (condition.value or "").lower()
            if emp_type and required_type and emp_type != required_type:
                return False, _(
                    "This leave type is restricted to employees with employment type: {emp_type}."
                ).format(emp_type=condition.value)

        elif ctype == "grade":
            grade = None
            work_info = getattr(employee, "employee_work_info", None)
            if work_info:
                grade_obj = getattr(work_info, "job_position_id", None)
                if grade_obj:
                    grade = str(grade_obj).lower()
            required_grade = (condition.value or "").lower()
            if grade and required_grade and grade != required_grade:
                return False, _(
                    "This leave type is restricted to employees with grade: {grade}."
                ).format(grade=condition.value)

    return True, None


def has_sufficient_leave_balance(available_leave, requested_days) -> bool:
    """
    Gate used by leave_request_approve before deducting balance.

    Returns True when available_days + carryforward_days covers requested_days.
    """
    total = (available_leave.available_days or 0) + (
        available_leave.carryforward_days or 0
    )
    return total >= float(requested_days or 0)


def deduct_leave_balance(leave_request, available_leave):
    """
    Deduct an approved request from the balance, oldest days first.

    carryforward_days hold last year's days, which expire first, so they are
    used before available_days (this year's). Sets approved_carryforward_days
    and approved_available_days on the request, which reject/cancel use to give
    the days back. Does not save either object.
    """
    requested = leave_request.requested_days
    if requested > available_leave.carryforward_days:
        from_available = requested - available_leave.carryforward_days
        leave_request.approved_carryforward_days = available_leave.carryforward_days
        leave_request.approved_available_days = from_available
        available_leave.carryforward_days = 0
        available_leave.available_days = available_leave.available_days - from_available
    else:
        leave_request.approved_carryforward_days = requested
        leave_request.approved_available_days = 0
        available_leave.carryforward_days = available_leave.carryforward_days - requested


# Talently accrual policy: days are credited each month on the joining-date
# anniversary; days earned in year N can be used until Dec 31 of N+1.
LEAVE_MONTHLY_ACCRUAL = getattr(settings, "LEAVE_MONTHLY_ACCRUAL", 1.83)

# Card titles in English for the leave types Talently uses; any other type
# shows its own name.
LEAVE_CARD_TITLES = getattr(
    settings,
    "LEAVE_CARD_TITLES",
    {
        "vacaciones": "Vacation",
        "cumpleaños": "Birthday",
        "ausencia médica": "Medical leave",
        "otro": "Other",
    },
)

_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def format_long_date(value):
    """date(2027, 1, 1) -> 'Jan 1, 2027' (English regardless of the active locale)."""
    return f"{_MONTH_ABBR[value.month - 1]} {value.day}, {value.year}"


def leave_card_kind(leave_type):
    """
    How a balance card is shown, from the leave type configuration:
    'unlimited' (no limit), 'resetting' (fixed days that reset, e.g. birthday)
    or 'accrual' (monthly accrual with yearly periods, e.g. vacation).
    """
    if not leave_type.limit_leave:
        return "unlimited"
    if leave_type.reset:
        return "resetting"
    return "accrual"


def leave_card_title(leave_type):
    return LEAVE_CARD_TITLES.get((leave_type.name or "").strip().lower(), leave_type.name)


def accrual_credits_in_year(date_joining, year, today):
    """Monthly accrual credits dated in `year`, from the month after joining up to today."""
    if not date_joining:
        return 0
    credits = 0
    for month in range(1, 13):
        day = min(date_joining.day, calendar.monthrange(year, month)[1])
        credit_date = date(year, month, day)
        if date_joining < credit_date <= today:
            credits += 1
    return credits


def accrual_periods(available_leave, today=None):
    """
    Balance of an accrual leave split by yearly period, oldest first.

    available_leave.carryforward_days is last year's period and available_days
    this year's (deduct_leave_balance and the yearly rollover keep them that
    way). Earned comes from the accrual policy; Used is Earned - Available.
    """
    today = today or date.today()
    work_info = getattr(available_leave.employee_id, "employee_work_info", None)
    date_joining = getattr(work_info, "date_joining", None)

    rows = []
    for year, label, available in (
        (today.year - 1, "last year", available_leave.carryforward_days or 0),
        (today.year, "this year", available_leave.available_days or 0),
    ):
        earned = round(accrual_credits_in_year(date_joining, year, today) * LEAVE_MONTHLY_ACCRUAL, 2)
        if label == "last year" and not earned and not available:
            continue
        rows.append(
            {
                "year": year,
                "label": label,
                "earned": earned,
                "used": round(earned - available, 2),
                "available": round(available, 2),
                "expires": format_long_date(date(year + 2, 1, 1)),
            }
        )
    total = {
        key: round(sum(row[key] for row in rows), 2)
        for key in ("earned", "used", "available")
    }
    return {"rows": rows, "total": total}


def total_days_taken(available_leave):
    """Approved days of this leave type since the employee joined."""
    from leave.models import LeaveRequest

    taken = LeaveRequest.objects.filter(
        employee_id=available_leave.employee_id,
        leave_type_id=available_leave.leave_type_id,
        status="approved",
    ).aggregate(total=Sum("requested_days"))["total"]
    return round(taken or 0, 2)


def get_condition_display_choices():
    """
    Returns a dict of {condition_type: suggested value choices} for UI hints.
    """
    return {
        "gender": [("male", _("Male")), ("female", _("Female")), ("other", _("Other"))],
        "marital_status": [
            ("single", _("Single")),
            ("married", _("Married")),
            ("divorced", _("Divorced")),
        ],
        "once_per_employment": [],
        "nationality": [],
        "department": [],
        "employment_type": [],
        "grade": [],
        "service_duration": [],
    }
