from django import template

register = template.Library()


@register.filter
def strformatted(value, arg):
    """Perform non-magical format of `value` with the format in `arg`.

    This is similar to Django's stringformat filter, but don't add the initial
    '%' they do.  Which allows you to put your value in the middle of other
    string.

    """
    try:
        return str(arg) % value
    except (ValueError, TypeError):
        return ''
