import typing as t

from django.db import models


def get_model_subscriptions(
    obj: t.Type[models.Model] | models.Model,
    actions: t.Sequence[str] = ("created", "updated", "deleted"),
) -> set[str]:
    """Get the subscriptions to actions of the model.

    If the `obj` is an instance of the model, return all the subscriptions
    from actions.  If `obj` is just the model class, return the top-level
    subscription.

    The `actions` is the set of actions to subscribe to, including any
    possible relation (e.g 'users.deleted').

    """
    if isinstance(obj, models.Model):
        cls = type(obj)
        instance = obj
    else:
        cls = obj
        instance = None
    app = cls._meta.app_label
    name = cls._meta.model_name
    result = {(model_prefix := f"{app}.{name}")}
    if instance:
        result.add(prefix := f"{model_prefix}.{instance.pk}")
        for action in actions:
            result.add(f"{prefix}.{action}")
    return result
