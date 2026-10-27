"""Shared dry-run and confirmation infrastructure for risky document changes."""

from __future__ import annotations

from typing import Any

from ..errors import ConfirmationRequiredError, InvalidParamsError
from .transaction import new_transaction


def _strict_bool(value: Any, name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise InvalidParamsError(f"{name} must be a boolean")
    return value


def prepare_destructive_action(
    *,
    action: str,
    affected_datablocks: list[dict[str, Any]],
    journal: list[dict[str, Any]],
    dry_run: Any = None,
    confirm: Any = None,
    require_confirm: bool,
    state_token: str,
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Build a transaction and decide whether an operation may apply.

    The returned boolean is true only for a real run that passed every gate.
    Confirmation failures carry the same affected-datablock report as a dry run.
    """
    normalized_dry_run = _strict_bool(dry_run, "dry_run", True)
    if confirm is not None and not isinstance(confirm, bool):
        raise InvalidParamsError("confirm must be a boolean")

    transaction = new_transaction(affected_datablocks)
    transaction.update(
        action=action,
        dry_run=normalized_dry_run,
        confirmation_required=bool(require_confirm),
        journal=journal,
        state_token=state_token,
    )
    if extra:
        transaction.update(extra)
    if normalized_dry_run:
        return transaction, False
    if require_confirm and confirm is not True:
        transaction["confirmation_required"] = True
        raise ConfirmationRequiredError(
            f"{action} requires dry_run: false and confirm: true",
            transaction,
        )
    return transaction, True
