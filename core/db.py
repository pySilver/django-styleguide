from typing import TYPE_CHECKING, Any

from asgiref.sync import sync_to_async
from django.core.exceptions import ValidationError
from django.db import models, router
from django.db.models import UniqueConstraint
from django.utils import timezone
from project.core.exceptions import OptimisticLockError
from project.core.types import DjangoModelType

if TYPE_CHECKING:
    from django.db.models.manager import BaseManager

    _RelatedManager = BaseManager[Any]


def model_update(  # noqa: PLR0913
    *,
    instance: DjangoModelType,
    fields: list[str],
    data: dict[str, Any],
    auto_modified: bool = True,
    modified_field: str = "modified",
    where: dict[str, Any] | None = None,
) -> tuple[DjangoModelType, bool]:
    """
    Generic update service for Django models.

    **Use this helper by default for all single-instance updates.**

    Benefits over manual save():
    - Auto-calls full_clean() before save
    - Auto-includes modified field in update_fields
    - Dirty checking: only saves if values actually changed
    - Returns has_updated flag for conditional logic
    - Optimized UPDATE query with only changed fields

    Use direct modification only when:
    - Read-modify-write pattern (new value depends on old)
    - Bulk update preparation (use prepare_instance_for_bulk_update)
    - Explicitly skipping validation (rare, document why)

    **Optimistic locking (``where``):**

    When ``where`` is provided, the update uses a conditional
    ``Model.objects.filter(pk=..., **where).update(...)`` instead of
    ``instance.save()``. If 0 rows are updated (the ``where`` condition no
    longer matches), ``OptimisticLockError`` is raised. ``full_clean()`` and
    dirty checking are skipped in this mode because OOL writes are targeted
    atomic updates (FK assignments, not full model saves). M2M fields are
    incompatible with the ``where`` path. A ``ValueError`` is raised if
    ``data`` has no keys matching ``fields`` (programming bug, not OOL).

    Args:
        instance: The Django model instance to be updated.
        fields: List of field names that may be updated.
        data: Dictionary containing the new values for the fields.
        auto_modified: Whether to automatically update the modified
            timestamp field if it exists.
        modified_field: Name of the modified timestamp field
            (default: "modified").
        where: Optional dict of OOL conditions. When provided, switches to
            conditional ``filter().update()`` instead of ``instance.save()``.

    Returns:
        A tuple containing:
        - The updated Django model instance.
        - A boolean indicating whether any updates were performed.

    Example:
        # Preferred: use helper for all updates
        instance, has_updated = model_update(
            instance=user,
            fields=["first_name", "last_name"],
            data={"first_name": "John"},
        )

        # Optimistic locking: conditional update
        instance, has_updated = model_update(
            instance=product,
            fields=["category_prediction"],
            data={"category_prediction": prediction},
            where={"content_hash": product.content_hash},
        )

    Notes:
        - Only keys present in both `fields` and `data` are updated.
        - Fields in `fields` but not in `data` are skipped.
        - Asserts all values in `fields` are actual model fields.
        - M2M fields are handled after the instance save (not with ``where``).
    """

    if where is not None:
        instance = _model_update_where(
            instance=instance,
            fields=fields,
            data=data,
            auto_modified=auto_modified,
            modified_field=modified_field,
            where=where,
        )
        return instance, True

    model_fields = {field.name: field for field in instance._meta.get_fields()}  # noqa: SLF001
    has_updated = False
    m2m_data = {}
    update_fields = []

    for field in fields:
        # Skip if a field is not present in the actual data
        if field not in data:
            continue

        # If field is not an actual model field, raise an error
        model_field = model_fields.get(field)

        assert model_field is not None, (
            f"{field} is not part of {instance.__class__.__name__} fields."
        )

        # If we have m2m field, handle differently
        if isinstance(model_field, models.ManyToManyField):
            m2m_data[field] = data[field]
            continue

        if getattr(instance, field) != data[field]:
            has_updated = True
            update_fields.append(field)
            setattr(instance, field, data[field])

    # Perform an update only if any of the fields were actually changed
    if has_updated:
        if auto_modified:
            # We want to take care of the modified timestamp field,
            # Only if the model has that field
            # And if no value for the modified field has been provided
            if modified_field in model_fields and modified_field not in update_fields:
                update_fields.append(modified_field)
                setattr(instance, modified_field, timezone.now())

        instance.full_clean()
        # Update only the fields that are meant to be updated.
        # Django docs reference:
        # https://docs.djangoproject.com/en/dev/ref/models/instances/#specifying-which-fields-to-save
        instance.save(update_fields=update_fields)

    for field_name, value in m2m_data.items():
        related_manager: _RelatedManager = getattr(instance, field_name)
        related_manager.set(value)

        # Still not sure about this.
        # What if we only update m2m relations & nothing on the model?
        # Is this still considered as updated?
        has_updated = True

    return instance, has_updated


def _model_update_where(  # noqa: PLR0913
    *,
    instance: DjangoModelType,
    fields: list[str],
    data: dict[str, Any],
    auto_modified: bool,
    modified_field: str,
    where: dict[str, Any],
) -> DjangoModelType:
    """Conditional update for optimistic locking (OOL).

    Builds an UPDATE ... WHERE pk=... AND <where conditions> query.
    Skips full_clean() and dirty checking — OOL writes are targeted atomic
    updates. M2M fields are not supported.

    Raises:
        OptimisticLockError: If the where condition no longer matches
            (0 rows updated) — the row was concurrently modified.
        ValueError: If ``data`` has no keys matching ``fields`` —
            programming bug, not an OOL race.
    """
    model_fields = {field.name: field for field in instance._meta.get_fields()}  # noqa: SLF001
    update_data: dict[str, Any] = {}

    for field in fields:
        if field not in data:
            continue

        model_field = model_fields.get(field)
        assert model_field is not None, (
            f"{field} is not part of {instance.__class__.__name__} fields."
        )
        assert not isinstance(model_field, models.ManyToManyField), (
            f"M2M field {field!r} is incompatible with the `where` parameter."
        )

        update_data[field] = data[field]

    if not update_data:
        raise ValueError(
            f"model_update(where=...) called with no updatable fields for "
            f"{type(instance).__name__}(pk={instance.pk}): "
            f"fields={fields!r}, data keys={list(data)!r}",
        )

    if (
        auto_modified
        and modified_field in model_fields
        and modified_field not in update_data
    ):
        update_data[modified_field] = timezone.now()

    rows = type(instance).objects.filter(pk=instance.pk, **where).update(**update_data)

    if rows == 0:
        raise OptimisticLockError(
            f"OOL failed: {type(instance).__name__}(pk={instance.pk}) where {where!r}",
        )

    for field, value in update_data.items():
        setattr(instance, field, value)

    return instance


async def amodel_update(  # noqa: PLR0913
    *,
    instance: DjangoModelType,
    fields: list[str],
    data: dict[str, Any],
    auto_modified: bool = True,
    modified_field: str = "modified",
    where: dict[str, Any] | None = None,
) -> tuple[DjangoModelType, bool]:
    return await sync_to_async(model_update)(
        instance=instance,
        fields=fields,
        data=data,
        auto_modified=auto_modified,
        modified_field=modified_field,
        where=where,
    )


def prepare_instance_for_bulk_update(
    *,
    instance: DjangoModelType,
    fields: list[str],
    data: dict[str, Any],
    auto_modified: bool = True,
    modified_field: str = "modified",
) -> DjangoModelType:
    """
    Prepare an instance for bulk update by setting values
    for given fields from data dict.

    Args:
        instance: The Django model instance to be updated.
        fields: List of field names to be updated.
        data: Dictionary containing the new values for the fields.
        auto_modified: Whether to automatically update the modified
            timestamp field if it exists.
        modified_field: Name of the modified timestamp field
            (default: "modified").

    Returns:
        The updated Django model instance.

    Raises:
        AssertionError: If a field in 'fields' is not part of the
            model's fields.
    """

    model_fields = {field.name: field for field in instance._meta.get_fields()}  # noqa: SLF001

    for field in fields:
        if field not in data:
            continue

        model_field = model_fields.get(field)
        assert model_field is not None, (
            f"{field} is not part of {instance.__class__.__name__} fields."
        )

        if not isinstance(model_field, models.ManyToManyField):
            setattr(instance, field, data[field])

    if auto_modified and modified_field in model_fields:
        setattr(instance, modified_field, timezone.now())

    instance.full_clean()
    return instance


async def aprepare_instance_for_bulk_update(
    *,
    instance: DjangoModelType,
    fields: list[str],
    data: dict[str, Any],
    auto_modified: bool = True,
    modified_field: str = "modified",
) -> DjangoModelType:
    return await sync_to_async(prepare_instance_for_bulk_update)(
        instance=instance,
        fields=fields,
        data=data,
        auto_modified=auto_modified,
        modified_field=modified_field,
    )


def _validate_non_unique_constraints(instance: models.Model) -> None:
    """Validate non-uniqueness constraints only.

    Mirrors Django's ``validate_constraints()`` but skips
    ``UniqueConstraint``s. Used by the validated helpers where
    uniqueness is handled atomically by the database, but check
    constraints from composite fields (both-or-null, interval
    ordering) should still be validated at the Python layer.
    """
    using = router.db_for_write(instance.__class__, instance=instance)
    errors: dict[str, list[ValidationError]] = {}
    for model_class, model_constraints in instance.get_constraints():
        for constraint in model_constraints:
            if isinstance(constraint, UniqueConstraint):
                continue
            try:
                constraint.validate(model_class, instance, exclude=set(), using=using)
            except ValidationError as e:
                errors = e.update_error_dict(errors)
    if errors:
        raise ValidationError(errors)


def validated_get_or_create(
    model: type[DjangoModelType],
    *,
    lookup: dict[str, Any],
    defaults: dict[str, Any] | None = None,
) -> tuple[DjangoModelType, bool]:
    """
    Like Django's get_or_create, but calls full_clean before creating.

    Builds a throwaway instance from lookup + defaults and validates via
    full_clean(validate_unique=False, validate_constraints=False) followed
    by non-uniqueness constraint validation (CheckConstraints from composite
    fields), then delegates to Django's get_or_create. Uniqueness checks are
    skipped because get_or_create handles uniqueness atomically via
    INSERT...ON CONFLICT.

    Lookup keys must be plain field names (no ``__`` lookups) and default
    values must be concrete (no callables). This is intentionally narrower
    than Django's full API — the helpers target the service-layer pattern
    where lookup/defaults are always resolved before the call.

    Callers own @pgtrigger_ignore and @transaction.atomic.

    Raises:
        ValidationError: If the throwaway instance fails full_clean.
    """
    resolved_defaults = defaults or {}
    instance = model(**lookup, **resolved_defaults)
    instance.full_clean(validate_unique=False, validate_constraints=False)
    _validate_non_unique_constraints(instance)
    return model.objects.get_or_create(**lookup, defaults=resolved_defaults)


async def avalidated_get_or_create(
    model: type[DjangoModelType],
    *,
    lookup: dict[str, Any],
    defaults: dict[str, Any] | None = None,
) -> tuple[DjangoModelType, bool]:
    return await sync_to_async(validated_get_or_create)(
        model,
        lookup=lookup,
        defaults=defaults,
    )


def validated_update_or_create(
    model: type[DjangoModelType],
    *,
    lookup: dict[str, Any],
    defaults: dict[str, Any] | None = None,
    create_defaults: dict[str, Any] | None = None,
) -> tuple[DjangoModelType, bool]:
    """
    Like Django's update_or_create, but validates the create path.

    Validates a throwaway instance representing the create payload:
    lookup + create_defaults when provided, otherwise lookup + defaults
    (matching Django's internal create_defaults fallback). Uniqueness
    checks are skipped because update_or_create handles uniqueness
    atomically; non-uniqueness constraints (CheckConstraints from
    composite fields) are still validated.

    Lookup keys must be plain field names (no ``__`` lookups) and default
    values must be concrete (no callables). This is intentionally narrower
    than Django's full API — the helpers target the service-layer pattern
    where lookup/defaults are always resolved before the call.

    Update-path validation belongs to model_update.

    Callers own @pgtrigger_ignore and @transaction.atomic.

    Raises:
        ValidationError: If the create-path instance fails full_clean.
    """
    effective_create = (defaults or {}) if create_defaults is None else create_defaults
    instance = model(**lookup, **effective_create)
    instance.full_clean(validate_unique=False, validate_constraints=False)
    _validate_non_unique_constraints(instance)
    return model.objects.update_or_create(
        **lookup,
        defaults=defaults,
        create_defaults=create_defaults,
    )


async def avalidated_update_or_create(
    model: type[DjangoModelType],
    *,
    lookup: dict[str, Any],
    defaults: dict[str, Any] | None = None,
    create_defaults: dict[str, Any] | None = None,
) -> tuple[DjangoModelType, bool]:
    return await sync_to_async(validated_update_or_create)(
        model,
        lookup=lookup,
        defaults=defaults,
        create_defaults=create_defaults,
    )
