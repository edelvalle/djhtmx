from collections.abc import Sequence

from django.db import models

__all__ = ("get_instance_subscriptions", "get_model_subscriptions")


def get_instance_subscriptions(
    obj: models.Model,
    actions: Sequence[str] = ("created", "updated", "deleted"),
):
    """Get the subscriptions to actions of a single instance of a model.

    This won't return model-level subscriptions.

    The `actions` is the set of actions to subscribe to, including any possible relation (e.g
    'users.deleted').  If actions is empty, return only instance-level subscription.

    """
    cls = type(obj)
    app = cls._meta.app_label
    name = cls._meta.model_name
    prefix = f"{app}.{name}.{obj.pk}"
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
    if isinstance(obj, models.Model):
        cls = type(obj)
        instance = obj
    else:
        cls = obj
        instance = None
    app = cls._meta.app_label
    name = cls._meta.model_name
    model_prefix = f"{app}.{name}"
    prefix = f"{model_prefix}.{instance.pk}" if instance else model_prefix
    result = {(f"{prefix}.{action}" if action else prefix) for action in actions}
    return result
