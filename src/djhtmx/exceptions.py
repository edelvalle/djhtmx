class ComponentNotFound(LookupError):
    pass


class LoginRequired(Exception):
    """A component that requires a logged-in user was built without one.

    Raised while validating a component whose `user` field is annotated non-optionally (`user:
    User` rather than `user: User | None`) when the repository has no authenticated user: the
    session expired while the page stayed open, the user logged out in another tab, or the request
    reached the `csrf_exempt` `/_htmx/` endpoints without cookies.

    Both places that build components turn it into a trip to the login page instead of a 500:
    `CommandProcessor.process` answers the request with a `Redirect` to `settings.LOGIN_URL`, and
    `djhtmx.middleware` answers a full page render with a redirect to the login page.  Anything
    else that builds components (a management command, say) sees the exception.

    """

    def __init__(self, component_name: str):
        super().__init__(f"{component_name} requires a logged-in user")
        self.component_name = component_name
