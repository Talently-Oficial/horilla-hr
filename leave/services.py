"""
leave/services.py

Centralised business-logic helpers for the leave app.
Condition evaluation follows the same pattern as payroll allowance eligibility checks.
"""

import calendar
from collections import defaultdict
from datetime import date, datetime, timedelta

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


def employee_date_joining(employee):
    work_info = getattr(employee, "employee_work_info", None)
    return getattr(work_info, "date_joining", None)


def usable_carryforward_days(available_leave):
    """Carryforward days a request may use, capped like LeaveRequest.clean() does."""
    leave_type = available_leave.leave_type_id
    carryforward_days = available_leave.carryforward_days or 0
    if leave_type.carryforward_type in ["carryforward", "carryforward expire"]:
        return min(carryforward_days, leave_type.carryforward_max or 0)
    if leave_type.carryforward_type == "no carryforward":
        return 0
    return carryforward_days


def upcoming_accrual_credits(available_leave, start_date, today, date_joining):
    """
    Monthly credits the accrual job will still add up to `start_date`.

    Today's credit counts until the job has run: it stamps reset_date with the
    day it credits, and skips a balance whose reset_date is already today.
    """
    credited_today = available_leave.reset_date == today
    return sum(
        1
        for d in accrual_credit_dates(date_joining, start_date)
        if d > today or (d == today and not credited_today)
    )


def leave_balance_limit(available_leave, start_date, today=None, date_joining=None):
    """
    Most days a request starting on `start_date` may take.

    That is the current balance (available_days + carryforward_days) and, for
    accrual types (vacation), the monthly credits still to come up to the start
    date: the job credits them on each joining-date anniversary, which Horilla's
    own forecast doesn't know about. A request within that limit may leave the
    balance negative when approved; the upcoming credits pay it back.

    `date_joining` can be passed in to avoid the queries.
    """
    leave_type = getattr(available_leave, "leave_type_id", None)
    if not is_accrual_leave_type(leave_type):
        return (available_leave.available_days or 0) + (
            available_leave.carryforward_days or 0
        )
    if isinstance(start_date, datetime):
        start_date = start_date.date()
    today = today or date.today()
    if date_joining is None:
        date_joining = employee_date_joining(available_leave.employee_id)
    upcoming = upcoming_accrual_credits(
        available_leave, start_date, today, date_joining
    )
    return (
        (available_leave.available_days or 0)
        + usable_carryforward_days(available_leave)
        + upcoming * LEAVE_MONTHLY_ACCRUAL
    )


def pending_leave_days(available_leave, exclude_request=None):
    """
    Days already asked for in requests still waiting for approval.

    Only for accrual types: their limit includes future credits, so without
    this two pending requests could each be promised the same credits.
    """
    from leave.models import LeaveRequest

    if not is_accrual_leave_type(getattr(available_leave, "leave_type_id", None)):
        return 0
    pending = LeaveRequest.objects.filter(
        employee_id=available_leave.employee_id,
        leave_type_id=available_leave.leave_type_id,
        status="requested",
    )
    if exclude_request is not None and exclude_request.pk:
        pending = pending.exclude(pk=exclude_request.pk)
    return sum(pending.values_list("requested_days", flat=True))


def request_balance_limit(available_leave, start_date, exclude_request=None, today=None):
    """
    Limit for creating or editing a request: leave_balance_limit() minus the
    days of the employee's other pending requests of the same type.
    """
    return leave_balance_limit(available_leave, start_date, today) - pending_leave_days(
        available_leave, exclude_request
    )


def has_sufficient_leave_balance(
    available_leave, requested_days, start_date, today=None, date_joining=None
) -> bool:
    """
    Gate used before approving (deducting) a request.

    Returns True when the balance, projected to `start_date` for accrual types,
    covers requested_days.
    """
    limit = leave_balance_limit(available_leave, start_date, today, date_joining)
    return limit >= float(requested_days or 0)


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
# anniversary, in arrears, and belong to the labor year they were worked in --
# one joining-date anniversary to the next, the closing anniversary included.
# Every labor year is therefore worth 12 credits, the 22 business days of the
# policy. What a labor year earns expires two labor years after it started, so
# a day lives between one and two years.
LEAVE_MONTHLY_ACCRUAL = getattr(settings, "LEAVE_MONTHLY_ACCRUAL", 1.83)

# Labor years a day stays usable, counted from the start of the labor year that
# earned it: joined Mar 15, 2025 -> what that year earns expires Mar 15, 2027.
LEAVE_EXPIRY_LABOR_YEARS = getattr(settings, "LEAVE_EXPIRY_LABOR_YEARS", 2)

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


def anniversary(date_joining, years):
    """
    The joining-date anniversary `years` years after joining.

    A Feb 29 joiner falls back to Feb 28 on common years, the same way the
    monthly credits use the last day of a short month.
    """
    year = date_joining.year + years
    day = min(date_joining.day, calendar.monthrange(year, date_joining.month)[1])
    return date(year, date_joining.month, day)


def labor_year_index(date_joining, when):
    """
    Which labor year `when` falls in: 0 from the joining date through the first
    anniversary, 1 through the second, and so on.

    The monthly credits are paid in arrears -- the credit dated on an
    anniversary pays for the month that ends there, worked inside the year that
    closes -- so it belongs to that year and not to the one that opens. That is
    what makes every labor year worth 12 credits, the first one included.
    """
    years = when.year - date_joining.year
    if years > 0 and anniversary(date_joining, years) >= when:
        years -= 1
    return max(years, 0)


def labor_year_label(date_joining, index):
    """'Mar 15, 2025 - Mar 14, 2026' for the labor year at `index`."""
    start = anniversary(date_joining, index)
    end = anniversary(date_joining, index + 1) - timedelta(days=1)
    return f"{format_long_date(start)} - {format_long_date(end)}"


def approved_leave_days(employee_id, leave_type_id=None):
    """(start_date, requested_days, leave_type_id) of the employee's approved requests, one query."""
    from leave.models import LeaveRequest

    qs = LeaveRequest.objects.filter(employee_id=employee_id, status="approved")
    if leave_type_id is not None:
        qs = qs.filter(leave_type_id=leave_type_id)
    return list(qs.values_list("start_date", "requested_days", "leave_type_id"))


def _periods_without_joining_date(balance):
    """
    Without a joining date there are no labor years to split the balance into:
    one open period, with no expiry, so the card still adds up to the balance.
    """
    row = {
        "index": 0,
        "label": "current",
        "period": "",
        "earned": round(max(balance, 0), 2),
        "used": round(max(-balance, 0), 2),
        "available": round(balance, 2),
        "expires": "",
    }
    return {
        "rows": [row],
        "total": {key: row[key] for key in ("earned", "used", "available")},
    }


def accrual_periods(available_leave, today=None, date_joining=None, approved=None):
    """
    Accrual balance split into the previous and the current labor year, oldest
    first.

    A labor year runs from one joining-date anniversary to the next, and the
    days it earns expire two years after it started: joined Mar 15, 2025, the
    12 credits of that labor year (Apr 15, 2025 through Mar 15, 2026) can be
    taken until Mar 14, 2027.

    Earned and Used come from the policy, not from how the balance happens to
    be split between available_days and carryforward_days: monthly credits by
    labor year, and approved requests consuming the oldest period still valid
    (days approved for the future count from today, when they were deducted).
    Days asked for with nothing left are owed against the current period.
    Whatever the real balance holds above that (allocations, manual top-ups,
    days a period has lost to expiry that the balance has not given up yet) is
    shown as earned in the current period, and whatever it lacks (manual cuts,
    history missing from Horilla) as used there. So Earned and Used are never
    negative, every row closes as Earned - Used = Available, and the total is
    the real balance.

    `date_joining` and `approved` ((start_date, requested_days, ...) tuples) can
    be passed in to avoid the queries.
    """
    today = today or date.today()
    if date_joining is None:
        date_joining = employee_date_joining(available_leave.employee_id)
    if approved is None:
        approved = approved_leave_days(
            available_leave.employee_id, available_leave.leave_type_id
        )
    balance = (available_leave.available_days or 0) + (
        available_leave.carryforward_days or 0
    )
    if not date_joining:
        return _periods_without_joining_date(balance)

    def expires_on(index):
        return anniversary(date_joining, index + LEAVE_EXPIRY_LABOR_YEARS)

    events = [
        (d, 0, LEAVE_MONTHLY_ACCRUAL)
        for d in accrual_credit_dates(date_joining, today)
    ]
    events += [(min(row[0], today), 1, float(row[1] or 0)) for row in approved]
    events.sort(key=lambda event: (event[0], event[1]))

    earned, used, left = defaultdict(float), defaultdict(float), {}
    owed = 0.0
    for when, is_request, days in events:
        for index in [i for i in left if expires_on(i) <= when]:
            del left[index]
        if not is_request:
            index = labor_year_index(date_joining, when)
            earned[index] += days
            repay = min(owed, days)
            owed -= repay
            used[index] += repay
            left[index] = left.get(index, 0.0) + days - repay
            continue
        for index in sorted(left):
            take = min(left[index], days)
            if take > 0:
                left[index] -= take
                used[index] += take
                days -= take
        owed += days

    current = labor_year_index(date_joining, today)
    previous = current - 1
    used[current] += owed
    adjustment = balance - sum(
        earned[i] - used[i] for i in (previous, current) if i >= 0
    )
    if adjustment >= 0:
        earned[current] += adjustment  # allocations, top-ups, days still on the balance
    else:
        used[current] -= adjustment  # manual cuts, history missing from Horilla

    rows = []
    for index in (previous, current):
        if index < 0:
            continue
        if index == previous and not round(earned[index], 2) and not round(used[index], 2):
            continue
        rows.append(
            {
                "index": index,
                "label": "previous" if index == previous else "current",
                "period": labor_year_label(date_joining, index),
                "earned": round(earned[index], 2),
                "used": round(used[index], 2),
                "available": round(earned[index] - used[index], 2),
                "expires": format_long_date(expires_on(index)),
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
