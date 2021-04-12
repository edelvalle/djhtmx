import inspect
import typing

import pydantic


def get_model(f, ignore=()):
    params = list(inspect.signature(f).parameters.values())
    fields = {}
    for param in params:
        if param.name in ignore:
            continue
        if param.kind is not inspect.Parameter.VAR_KEYWORD:
            default = param.default
            if default is inspect._empty:
                default = ...
            annotation = param.annotation
            if annotation is inspect._empty:
                if default is ...:
                    field = (typing.Any, ...)
                else:
                    field = default
            else:
                field = (annotation, default)

            if field is None:
                continue
            fields[param.name] = field
    return pydantic.create_model(f.__name__, **fields)


# Decoder for client requests

def extract_data(request):
    data = getattr(request, request.method)
    output = {}
    for key in set(data):
        if key.endswith('[]'):
            value = data.getlist(key)
        else:
            value = data.get(key)
        _set_value_on_path(output, key, value)
    return output


def _set_value_on_path(target, path, value):
    initial = target
    fragments = path.split('.')
    for fragment in fragments[:-1]:
        fragment, default, index = _get_default_value(fragment)
        target.setdefault(fragment, default)
        target = target[fragment]
        if index is not None:
            i_need_this_length = index + 1 - len(target)
            if i_need_this_length > 0:
                target.extend({} for _ in range(i_need_this_length))
            target = target[index]

    fragment, default, index = _get_default_value(fragments[-1])
    target[fragment] = value
    return initial


def _get_default_value(fragment):
    if fragment.endswith('[]'):
        fragment = fragment[:-2]
        default = []
        index = None
    if fragment.endswith(']'):
        index = int(fragment[fragment.index('[') + 1:-1])
        fragment = fragment[:fragment.index('[')]
        default = []
    else:
        default = {}
        index = None
    return fragment, default, index
