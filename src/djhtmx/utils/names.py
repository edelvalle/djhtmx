from django.db import models


def get_fqn(which):
    """Return the fully-qualified name of the object's class.

    If `which` is a type, use it directly; otherwise, look at it's class.  If we cannot know the
    module of the type, nor the name, fallback to the repr of the type.

    """
    cls = type(which) if not isinstance(which, type) else which
    try:
        mod = cls.__module__
    except AttributeError:
        mod = ""
    try:
        name = cls.__name__
    except AttributeError:
        return repr(cls)
    else:
        return f"{mod}.{name}" if mod else name


def get_model_full_label(model: models.Model | type[models.Model]) -> str:
    """Return `model`'s app label and model name, dotted, e.g. `todo.item`.

    `model` may be the class or one of its instances.

    """
    model_cls = model if isinstance(model, type) else type(model)
    return f"{model_cls._meta.app_label}.{model_cls._meta.model_name}"
