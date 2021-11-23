import typing as t

from django.core.cache import InvalidCacheBackendError, caches


class CacheMixin:
    """Common mixin for FE and HTMX components to cache responses.

    Subclasses MUST implement the method `_compute_cache_key`:meth: to compute
    the key to use for the cache.

    Also the following class-level attributes allow to configure the cache and
    how we use it:

    ``cache_name``: The name of the cache (in setting `CACHE`) where values are
           goint to be cached.

    ``always_refresh_cached_values``: If True every request (re)sets the cached
           value, even if it was actually fetched from the cache in the first
           place.  If False, values that were fetched from the cache are not
           refreshed and thus they will expire after the timeout is reached.

           The default is False, which allows for dummy keys (maybe just some
           varying parameter) that expire after some time.  If you have a more
           sofisticated key, you can switch this to True.

    ``cache_timeout``: The amount of seconds the cached items are alive.

    """

    cache_name: t.ClassVar[str] = 'default'
    always_refresh_cached_values: t.ClassVar[bool] = False
    cache_timeout: t.ClassVar[int] = 300  # 5 minutes

    def _compute_cache_key(self, ns: str) -> t.Optional[str]:
        """Returns a unique cache key.

        Cache only works if this method returns a non-empty string, and if
        Django is properly configured to cache data.

        Subclasses MUST take into account that the underlying data might have
        changed and implement its own mecanism to get a key that is attached to
        the liveness of the underlying data.

        The `ns` is a namespace you can use to emit keys for different methods
        in your component.

        """
        raise NotImplementedError

    @classmethod
    @property
    def _cache(cls):
        try:
            return caches[cls.cache_name]
        except InvalidCacheBackendError:
            return None

    def _with_cache(self, fn, *args, **kwargs):
        """Calls `fn(*args, **kwargs)` only on cache misses.

        The key namespace (the one we pass to `_compute_cache_key`:meth:) is the
        name of the method.

        """
        return self._with_named_cache(fn.__name__, fn, *args, **kwargs)

    def _with_named_cache(self, ns, fn, *args, **kwargs):
        cache = self._cache
        if cache:
            component_key = self._compute_cache_key(ns)
            if component_key:
                key = f"{self._fqn}::{ns}::{component_key}"
                result = cache.get(key, Unset)
            else:
                result = Unset
                key = None
        else:
            result = Unset
            key = None
        if result is Unset:
            result = fn(*args, **kwargs)
            if key and cache:
                cache.set(key, result, timeout=self.cache_timeout)
        elif self.always_refresh_cached_values and key and cache:
            # NB: Touching might silently fail because in the small window of
            # time between `cache.get` and `cache.touch` the key might have been
            # expired in the cache server.
            #
            # However, I prefer not to use `cache.set` here and let the cache to
            # be filled the next time we hit the same key.
            cache.touch(key, timeout=self.cache_timeout)
        return result


Unset = object()
