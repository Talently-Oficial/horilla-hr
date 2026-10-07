"""Template helpers for the leave balance cards in My Leave Requests."""

from django import template

from employee.models import EmployeeWorkInformation
from leave.services import (
    accrual_periods,
    approved_leave_days,
    leave_card_kind,
    leave_card_title,
    total_days_taken,
)

register = template.Library()


def _employee_data(context, employee_id):
    """Joining date and approved requests by leave type, fetched once per employee per render."""
    cache = context.render_context.setdefault("leave_balance_cards", {})
    if employee_id not in cache:
        date_joining = (
            EmployeeWorkInformation.objects.filter(employee_id=employee_id)
            .values_list("date_joining", flat=True)
            .first()
        )
        by_type = {}
        for row in approved_leave_days(employee_id):
            by_type.setdefault(row[2], []).append(row)
        cache[employee_id] = (date_joining, by_type)
    return cache[employee_id]


def _leave_card(context, available_leave):
    """
    Everything a balance card needs: its kind ('accrual', 'unlimited' or
    'limited'), an English title, and the yearly periods (accrual) or the
    total days taken (the rest).
    """
    leave_type = available_leave.leave_type_id
    kind = leave_card_kind(leave_type)
    card = {"kind": kind, "title": leave_card_title(leave_type)}
    date_joining, by_type = _employee_data(context, available_leave.employee_id_id)
    approved = by_type.get(available_leave.leave_type_id_id, [])
    if kind == "accrual":
        card["periods"] = accrual_periods(
            available_leave, date_joining=date_joining, approved=approved
        )
        # A negative balance is shown as days owed, same number as the Total row.
        card["owed"] = max(-card["periods"]["total"]["available"], 0)
    else:
        card["total_taken"] = total_days_taken(available_leave, approved=approved)
    return card


# Unknown kinds sort last instead of breaking the page.
_CARD_ORDER = {"accrual": 0, "limited": 1, "unlimited": 2}


@register.simple_tag(takes_context=True)
def leave_cards(context, user_leaves):
    """
    Cards for a list of balances, split for the layout: the accrual (vacation)
    cards, which get the wide column, and the rest, ordered limited first
    (e.g. birthday) and unlimited last.
    """
    items = [
        {"leave": available_leave, "card": _leave_card(context, available_leave)}
        for available_leave in user_leaves
    ]
    items.sort(key=lambda item: _CARD_ORDER.get(item["card"]["kind"], len(_CARD_ORDER)))
    return {
        "accrual": [item for item in items if item["card"]["kind"] == "accrual"],
        "others": [item for item in items if item["card"]["kind"] != "accrual"],
    }


@register.filter
def leave_days(value):
    """16.4700 -> '16.47', 4.0 -> '4', -2.25 -> '-2.25'."""
    try:
        text = f"{float(value):.2f}"
    except (TypeError, ValueError):
        return value
    text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text
