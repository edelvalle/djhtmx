import sys

import djclick as click

from djhtmx.component import REGISTRY, Component


def bold(msg):
    return click.style(str(msg), bold=True)


def yellow(msg):
    return click.style(str(msg), fg="yellow")


@click.group()
def htmx():
    pass


@htmx.command("check-missing")
@click.option("--new-style-only", is_flag=True, default=False)
@click.argument("fname", type=click.File())
def check_missing(fname, new_style_only=False):
    r"""Check if there are any missing HTMX components.

    Expected usage:

    find -type f -name '*.html' | while read f; do grep -P '{% htmx .(\w+)' -o $f \
    | awk '{print $3}' | cut -b2-; done | sort -u \
    | python manage.py htmx check-missing -

    """
    names = {n.strip() for n in fname.readlines()}
    known = set(REGISTRY)
    if not new_style_only:
        known |= set(Component._all)
    missing = list(names - known)
    if missing:
        missing.sort()
        for n in missing:
            click.echo(
                f"Missing component detected {bold(yellow(n))}",
                file=sys.stderr,
            )
        sys.exit(1)
