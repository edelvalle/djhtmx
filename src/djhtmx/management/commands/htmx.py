import sys
from collections import defaultdict

import djclick as click
from xotl.tools.future.itertools import delete_duplicates
from xotl.tools.objects import get_branch_subclasses as get_final_subclasses

from djhtmx.component import REGISTRY, HtmxComponent


def bold(msg):
    return click.style(str(msg), bold=True)


def yellow(msg):
    return click.style(str(msg), fg="yellow")


@click.group()
def htmx():
    pass


@htmx.command("check-missing")  # type: ignore
@click.argument("fname", type=click.File())
def check_missing(fname):
    r"""Check if there are any missing HTMX components.

    Expected usage:

    find -type f -name '*.html' | while read f; do grep -P '{% htmx .(\w+)' -o $f \
    | awk '{print $3}' | cut -b2-; done | sort -u \
    | python manage.py htmx check-missing -

    """
    names = {n.strip() for n in fname.readlines()}
    known = set(REGISTRY)
    missing = list(names - known)
    if missing:
        missing.sort()
        for n in missing:
            click.echo(
                f"Missing component detected {bold(yellow(n))}",
                file=sys.stderr,
            )
        sys.exit(1)


@htmx.command("check-shadowing")  # type: ignore
def check_shadowing():
    "Checks if there are components that might shadow one another."
    clashes = defaultdict(list)
    for cls in get_final_subclasses(
        HtmxComponent,  # type: ignore
        without_duplicates=True,
    ):
        name = cls.__name__
        registered = REGISTRY.get(name)
        if registered is not cls and registered is not None:
            clashes[name].append(cls)
            clashes[name].append(registered)

    if clashes:
        for name, shadows in clashes.items():
            shadows = delete_duplicates(shadows)
            if shadows:
                click.echo(f"HtmxComponent {bold(name)} might be shadowed by:")
                for shadow in shadows:
                    click.echo(f"  -  {bold(shadow.__module__)}.{bold(shadow.__name__)}")

        sys.exit(1)
