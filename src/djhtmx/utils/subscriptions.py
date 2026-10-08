from collections.abc import Sequence

from django.db import models

from .names import get_model_full_label


def get_instance_subscriptions(
    obj: models.Model,
    actions: Sequence[str] = ("created", "updated", "deleted"),
):
    """Get the subscriptions to actions of a single instance of a model.

    This won't return model-level subscriptions.

    The `actions` is the set of actions to subscribe to, including any possible relation (e.g
    'users.deleted').  If actions is empty, return only instance-level subscription.

    """
    label = get_model_full_label(obj)
    prefix = f"{label}.{obj.pk}"
    if not actions:
        return {prefix}
    else:
        return {f"{prefix}.{action}" for action in actions}


def get_model_subscriptions(
    obj: type[models.Model] | models.Model,
    actions: Sequence[str | None] = (),
) -> set[str]:
    """Get the subscriptions to actions of the model.

    If the `obj` is an instance of the model, return all the subscriptions
    from actions.  If `obj` is just the model class, return the top-level
    subscription.

    The `actions` is the set of actions to subscribe to, including any
    possible relation (e.g 'users.deleted').

    """
    actions = actions or (None,)
    label = get_model_full_label(obj)
    if isinstance(obj, models.Model):
        instance: models.Model | None = obj
    else:
        instance = None
    prefix = f"{label}.{instance.pk}" if instance else label
    result = {(f"{prefix}.{action}" if action else prefix) for action in actions}
    return result
