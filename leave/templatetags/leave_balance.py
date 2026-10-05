"""Template helpers for the leave balance cards in My Leave Requests."""

from django import template

from leave.services import (
    accrual_periods,
    leave_card_kind,
    leave_card_title,
    total_days_taken,
)

register = template.Library()


@register.simple_tag
def leave_card(available_leave):
    """
    Everything a balance card needs: its kind ('accrual', 'resetting' or
    'unlimited'), an English title, and the yearly periods (accrual) or the
    total days taken (the rest).
    """
    leave_type = available_leave.leave_type_id
    kind = leave_card_kind(leave_type)
    card = {"kind": kind, "title": leave_card_title(leave_type)}
    if kind == "accrual":
        card["periods"] = accrual_periods(available_leave)
    else:
        card["total_taken"] = total_days_taken(available_leave)
    return card


@register.filter
def leave_days(value):
    """16.4700 -> '16.47', 4.0 -> '4', -2.25 -> '-2.25'."""
    try:
        text = f"{float(value):.2f}"
    except (TypeError, ValueError):
        return value
    text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text
