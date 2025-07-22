from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from django.db import models
from django.http import QueryDict
from django.shortcuts import resolve_url
from django.utils.safestring import SafeString

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SendHtml:
    content: SafeString

    # debug trace for troubleshooting
    debug_trace: str | None = None


@dataclass(slots=True)
class PushURL:
    url: str
    command: Literal["push_url"] = "push_url"

    @classmethod
    def from_params(cls, params: QueryDict):
        return cls("?" + params.urlencode())

    @classmethod
    def to(cls, to: Callable[..., Any] | models.Model | str, *args, **kwargs):
        return cls(resolve_url(to, *args, **kwargs))


@dataclass(slots=True)
class ReplaceURL:
    url: str
    command: Literal["replace_url"] = "replace_url"

    @classmethod
    def from_params(cls, params: QueryDict):
        return cls("?" + params.urlencode())

    @classmethod
    def to(cls, to: Callable[..., Any] | models.Model | str, *args, **kwargs):
        return cls(resolve_url(to, *args, **kwargs))
