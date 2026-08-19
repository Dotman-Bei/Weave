"""Vercel entrypoint.

Vercel's Python runtime imports ``app`` from this module and invokes it as an
ASGI application, so this file exists only to expose Weave's FastAPI app at the
path Vercel expects. All configuration is environment-driven -- see
``weave/config.py`` and the deployment notes in the README.

Two settings are not optional on Vercel and are defaulted here rather than left
to the dashboard, because getting either wrong fails at request time with an
error that does not name the cause:

* the filesystem is read-only apart from ``/tmp``, so the SQLite graph and any
  model cache must live there;
* a cold start has no warm cache, so the optional embedding model would be
  downloaded on the first request of every new instance.

Both are still overridable: an explicitly configured value always wins.
"""

from __future__ import annotations

import os

os.environ.setdefault("WEAVE_BACKEND", "embedded")
os.environ.setdefault("WEAVE_DB_PATH", "/tmp/weave.db")
os.environ.setdefault("WEAVE_EMBEDDINGS", "off")
os.environ.setdefault("HF_HOME", "/tmp/hf")
# /tmp is per-instance and ephemeral, so a cold start would otherwise serve an
# empty graph and abstain on every question.
os.environ.setdefault("WEAVE_AUTOSEED", "1")

from weave.api import app  # noqa: E402  (imported after the environment is set)

__all__ = ["app"]
