import logging
import typing as t

from django.apps import apps
from django.db import models
from django.db.models.signals import m2m_changed, post_save, pre_delete
from django.dispatch import Signal

from .schemas import ModelAction
from .settings import AUTO_BROADCAST

__all__ = []

log = logging.getLogger("htmx")


senders = (
    [
        apps.get_model(app_label, model_name)
        for app_label, model_name in AUTO_BROADCAST.senders
    ]
    if AUTO_BROADCAST.senders
    else [None]
)

mutation_happened = Signal()


def receiver(signal: Signal, *, is_active: bool):
    def _decorator(f):
        if is_active:
            for sender in senders:
                signal.connect(f, sender=sender)
        return f

    return _decorator


@receiver(
    post_save,
    is_active=(
        AUTO_BROADCAST.model or AUTO_BROADCAST.pk or AUTO_BROADCAST.related
    ),
)
def broadcast_post_save(sender, instance, created=False, **kwargs):
    name = sender._meta.model_name
    action = ModelAction.CREATED if created else ModelAction.UPDATED
    if AUTO_BROADCAST.model:
        notify_mutation([name], action, instance)

    if instance.pk is not None:
        if AUTO_BROADCAST.pk:
            notify_mutation(
                [f"{name}.{instance.pk}"],
                action,
                instance,
            )
        if AUTO_BROADCAST.related:
            broadcast_related(
                sender,
                action,
                instance,
            )


@receiver(
    pre_delete,
    is_active=(
        AUTO_BROADCAST.model or AUTO_BROADCAST.pk or AUTO_BROADCAST.related
    ),
)
def broadcast_pre_delete(sender, instance, **kwargs):
    name = sender._meta.model_name
    if AUTO_BROADCAST.model:
        notify_mutation([name], ModelAction.DELETED, instance)

    if instance.pk is not None:
        if AUTO_BROADCAST.pk:
            notify_mutation(
                [f"{name}.{instance.pk}"],
                ModelAction.DELETED,
                instance,
            )
        if AUTO_BROADCAST.related:
            broadcast_related(
                sender,
                ModelAction.DELETED,
                instance,
            )


def broadcast_related(sender, action: ModelAction, instance):
    for field in get_related_fields(sender):
        if field["is_m2m"]:
            fk_ids = getattr(instance, field["name"]).values_list(
                "id", flat=True
            )
        else:
            fk_ids = filter(None, [getattr(instance, field["name"])])

        if fk_ids:
            group_names = [
                f'{field["related_model_name"]}.{fk_id}.{field["related_name"]}'
                for fk_id in fk_ids
            ]
            notify_mutation(group_names, action, instance)


MODEL_RELATED_FIELDS = {}


def get_related_fields(model):
    related_fields = MODEL_RELATED_FIELDS.get(model)
    if related_fields is None:
        fields = []
        for field in model._meta.get_fields():
            if isinstance(field, (models.ForeignKey, models.ManyToManyField)):
                related_name = field.related_query_name()
                if related_name != "+":
                    is_m2m = isinstance(field, models.ManyToManyField)
                    if not is_m2m or AUTO_BROADCAST.m2m and is_m2m:
                        fields.append(
                            {
                                "is_m2m": is_m2m,
                                "name": field.attname,
                                "related_name": related_name,
                                "related_model_name": field.related_model._meta.model_name,  # noqa
                            }
                        )
        related_fields = MODEL_RELATED_FIELDS[model] = tuple(fields)
    return related_fields


@receiver(m2m_changed, is_active=AUTO_BROADCAST.m2m)
def broadcast_m2m_changed(sender, instance, action, model, pk_set, **kwargs):
    if action.startswith("post_") and instance.pk:
        if action.endswith("_add"):
            action = ModelAction.ADDED
        elif action.endswith("_remove"):
            action = ModelAction.REMOVED
        elif action.endswith("_clear"):
            action = ModelAction.CLEARED
        else:
            assert False, f"Unknown action `{action}`"

        model_name = model._meta.model_name
        attr_name = get_name_of(sender, model)
        updates = [f"{model_name}.{pk}.{attr_name}" for pk in pk_set or []]
        notify_mutation(updates, action, instance)

        model = type(instance)
        model_name = model._meta.model_name
        attr_name = get_name_of(sender, model)
        update = f"{model_name}.{instance.pk}.{attr_name}"
        notify_mutation(
            [f"{update}.{pk}" for pk in pk_set or []],
            action,
            instance,
        )


def get_name_of(through, model):
    for model_field in model._meta.get_fields():
        found = getattr(model_field, "through", None) or getattr(
            getattr(model, model_field.name, None), "through", None
        )
        if through is found:
            return model_field.name


def notify_mutation(names: t.Iterable[str], action: ModelAction, instance: str):
    for name in (n.replace("_", "-") for n in names):
        log.debug(f"<-> {action} {name}")
        mutation_happened.send(
            sender=None,
            channel=name,
            action=action,
            instance=instance,
        )
