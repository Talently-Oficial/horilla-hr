"""
leave/services.py

Centralised business-logic helpers for the leave app.
Condition evaluation follows the same pattern as payroll allowance eligibility checks.
"""

import calendar
from collections import defaultdict
from datetime import date

from django.conf import settings
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

# Leave types (lowercased names) that follow that policy. limit_leave=True,
# reset=False are the LeaveType defaults, so the configuration alone can't
# tell vacation apart from e.g. the auto-created compensatory type.
LEAVE_ACCRUAL_TYPES = getattr(settings, "LEAVE_ACCRUAL_TYPES", ("vacaciones",))

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


def format_long_date(value):
    """date(2027, 1, 1) -> 'Jan 1, 2027' (%b follows LC_TIME, which Django never changes)."""
    return f"{value:%b} {value.day}, {value.year}"


def is_accrual_leave_type(leave_type):
    return (
        leave_type is not None
        and leave_type.limit_leave
        and not getattr(leave_type, "is_compensatory_leave", False)
        and (leave_type.name or "").strip().lower() in LEAVE_ACCRUAL_TYPES
    )


def leave_card_kind(leave_type):
    """
    How a balance card is shown: 'accrual' (yearly period table, the types in
    LEAVE_ACCRUAL_TYPES), 'unlimited' (no limit) or 'limited' (everything else,
    including balances whose leave type was deleted).
    """
    if is_accrual_leave_type(leave_type):
        return "accrual"
    if leave_type is not None and not leave_type.limit_leave:
        return "unlimited"
    return "limited"


def leave_card_title(leave_type):
    if leave_type is None:
        return "-"
    return LEAVE_CARD_TITLES.get((leave_type.name or "").strip().lower(), leave_type.name)


def accrual_credit_dates(date_joining, until):
    """Monthly credit dates from the month after joining up to `until` (short months use their last day)."""
    if not date_joining:
        return []
    dates = []
    year, month = date_joining.year, date_joining.month
    while True:
        month += 1
        if month > 12:
            year, month = year + 1, 1
        credit = date(year, month, min(date_joining.day, calendar.monthrange(year, month)[1]))
        if credit > until:
            return dates
        dates.append(credit)


def accrual_credits_in_year(date_joining, year, today):
    """Monthly accrual credits dated in `year`, up to today."""
    return sum(1 for d in accrual_credit_dates(date_joining, today) if d.year == year)


def approved_leave_days(employee_id, leave_type_id=None):
    """(start_date, requested_days, leave_type_id) of the employee's approved requests, one query."""
    from leave.models import LeaveRequest

    qs = LeaveRequest.objects.filter(employee_id=employee_id, status="approved")
    if leave_type_id is not None:
        qs = qs.filter(leave_type_id=leave_type_id)
    return list(qs.values_list("start_date", "requested_days", "leave_type_id"))


def accrual_periods(available_leave, today=None, date_joining=None, approved=None):
    """
    Accrual balance split into last year's and this year's period, oldest first.

    Earned and Used come from the policy, not from how the balance happens to be
    split between available_days and carryforward_days: monthly credits by
    year, and approved requests consuming the oldest period still valid (days
    approved for the future count from today, when they were deducted). Days
    asked for with nothing left are owed against this year. Whatever the real
    balance holds above that (allocations, manual top-ups) is shown as earned
    this year, and whatever it lacks (manual cuts, history missing from
    Horilla) as used this year. So Earned and Used are never negative, every
    row closes as Earned - Used = Available, and the total is the real balance.

    `date_joining` and `approved` ((start_date, requested_days, ...) tuples) can
    be passed in to avoid the queries.
    """
    today = today or date.today()
    if date_joining is None:
        work_info = getattr(available_leave.employee_id, "employee_work_info", None)
        date_joining = getattr(work_info, "date_joining", None)
    if approved is None:
        approved = approved_leave_days(
            available_leave.employee_id, available_leave.leave_type_id
        )

    events = [(d, 0, LEAVE_MONTHLY_ACCRUAL) for d in accrual_credit_dates(date_joining, today)]
    events += [(min(row[0], today), 1, float(row[1] or 0)) for row in approved]
    events.sort(key=lambda event: (event[0], event[1]))

    earned, used, left = defaultdict(float), defaultdict(float), {}
    owed = 0.0
    for when, is_request, days in events:
        for year in [y for y in left if y < when.year - 1]:
            del left[year]  # expired on Jan 1 of year + 2
        if not is_request:
            earned[when.year] += days
            repay = min(owed, days)
            owed -= repay
            used[when.year] += repay
            left[when.year] = left.get(when.year, 0.0) + days - repay
            continue
        for year in sorted(left):
            take = min(left[year], days)
            if take > 0:
                left[year] -= take
                used[year] += take
                days -= take
        owed += days

    last, this = today.year - 1, today.year
    used[this] += owed
    balance = (available_leave.available_days or 0) + (available_leave.carryforward_days or 0)
    adjustment = balance - sum(earned[y] - used[y] for y in (last, this))
    if adjustment >= 0:
        earned[this] += adjustment  # allocations, manual top-ups
    else:
        used[this] -= adjustment  # manual cuts, history missing from Horilla

    rows = []
    for year, label in ((last, "last year"), (this, "this year")):
        if label == "last year" and not round(earned[year], 2) and not round(used[year], 2):
            continue
        rows.append(
            {
                "year": year,
                "label": label,
                "earned": round(earned[year], 2),
                "used": round(used[year], 2),
                "available": round(earned[year] - used[year], 2),
                "expires": format_long_date(date(year + 2, 1, 1)),
            }
        )
    total = {
        key: round(sum(row[key] for row in rows), 2)
        for key in ("earned", "used", "available")
    }
    return {"rows": rows, "total": total}


def total_days_taken(available_leave, approved=None):
    """
    Approved days of this leave type since the employee joined (lifetime, on
    purpose; AvailableLeave.leave_taken() only counts from assigned_date).
    """
    if approved is None:
        approved = approved_leave_days(
            available_leave.employee_id, available_leave.leave_type_id
        )
    return round(sum(float(row[1] or 0) for row in approved), 2)


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
