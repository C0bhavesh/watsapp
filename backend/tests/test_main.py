import logging

import app.main  # noqa: F401  — importing bootstraps the app + its logging config


def test_app_namespace_logger_actually_emits_info_in_production() -> None:
    """Importing the app must configure the `app.*` namespace to emit INFO to a real handler.

    Without explicit config the root logger sits at WARNING with zero handlers, so every
    `logger.info(...)` in app code (including the owner-approved Shopify webhook debug lines)
    is silently dropped by `logging.lastResort` (WARNING+ only) and never reaches Vercel's log
    stream. This asserts the REAL runtime configuration:

    - `isEnabledFor(INFO)` on a concrete app logger - the level gate. pytest's `caplog` injects
      its OWN INFO handler + level and would mask a missing production config; this assertion
      instead reads the effective level pytest does not touch by default (root stays WARNING),
      so it fails unless the bootstrap set the `app` namespace to INFO.
    - a `StreamHandler` on the `app` logger itself - the emit gate. A handler must exist in the
      app-namespace chain, else an INFO record still hits `lastResort` and is dropped. It sits on
      the `app` logger (not root) so third-party libraries keep their default WARNING verbosity.
    """
    webhook_logger = logging.getLogger("app.channels.shopify_webhook")
    assert webhook_logger.isEnabledFor(logging.INFO)

    app_logger = logging.getLogger("app")
    assert any(isinstance(h, logging.StreamHandler) for h in app_logger.handlers)
