"""
Async-compatible replacement for ``pgtrigger.ignore()``.

``pgtrigger.ignore()`` tracks ignored triggers in ``threading.local()``,
which is invisible across ``sync_to_async`` boundaries.  This module
replaces that with a single ``ContextVar``-based mechanism that works in
both sync and async contexts:

* **Sync**: ``ContextVar`` is readable in the same thread — behaves like
  a thread-local.
* **Async → sync_to_async**: ``asyncio`` (and asgiref) copies the current
  context into executor threads via ``contextvars.copy_context().run()``,
  so the ``ContextVar`` set in the async caller is visible inside the
  thread where Django executes SQL.

A permanent execute wrapper, installed on every PostgreSQL connection via
``connection_created`` signal, reads the ``ContextVar`` and prepends
``SELECT set_config('pgtrigger.ignore', …, true)`` before each SQL
statement — the same injection ``pgtrigger.runtime`` performs.

Exit safety
-----------
Unlike ``pgtrigger.ignore()``, exiting a ``pgtrigger_ignore`` block
performs no SQL — it is a pure ``ContextVar.reset()``.  This means it is
safe to catch database errors (``IntegrityError``, etc.) inside the block
without risking a failed flush on exit.
"""

import functools
import inspect
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from types import TracebackType
from typing import Any, ParamSpec, Self, TypeVar, cast, overload

import pgtrigger.utils  # pyright: ignore[reportMissingModuleSource]
from django.db.backends.signals import connection_created
from pgtrigger import registry as _pgtrigger_registry

P = ParamSpec("P")
T = TypeVar("T")
R = TypeVar("R")

# ---------------------------------------------------------------------------
# State: single ContextVar replacing pgtrigger's threading.local()
# ---------------------------------------------------------------------------

_ignore_pgids: ContextVar[frozenset[str]] = ContextVar(
    "_pgtrigger_ignore_pgids",
    default=frozenset(),
)

_PSYCOPG3 = pgtrigger.utils.psycopg_maj_version == 3  # noqa: PLR2004

_psycopg_pq: Any = None
if _PSYCOPG3:
    import psycopg.pq as _psycopg_pq


def _resolve_uris_to_pgids(uris: tuple[str, ...]) -> frozenset[str]:
    """Resolve pgtrigger URIs to the pgid strings used by set_config.

    Both ``table:pgid`` and bare ``pgid`` formats are emitted for
    backward compatibility with pgtrigger's trigger functions.
    """
    pgids: set[str] = set()
    for model, trigger in _pgtrigger_registry.registered(*uris):
        pgid = trigger.get_pgid(model)
        pgids.add(f"{model._meta.db_table}:{pgid}")  # noqa: SLF001
        pgids.add(pgid)
    return frozenset(pgids)


# ---------------------------------------------------------------------------
# Execute wrapper (permanent, installed on every PG connection)
# ---------------------------------------------------------------------------


def _can_inject(cursor: Any, sql: Any) -> bool:
    """Check whether it is safe to prepend ``set_config`` to *sql*.

    Mirrors ``pgtrigger.runtime._can_inject_variable``:

    * Named cursors (server-side iterators) prepend ``NO SCROLL CURSOR
      WITHOUT HOLD FOR`` — injection would produce invalid SQL.
    * ``CREATE … CONCURRENTLY`` cannot run inside a transaction, and
      ``set_config(…, true)`` requires a transaction context.
    * If the current transaction is in an errored state, any SQL
      (including ``set_config``) would fail with *"current transaction is
      aborted"*.
    """
    if getattr(cursor, "name", None):
        return False

    if isinstance(sql, bytes):
        sql_str = sql.decode()
    elif not isinstance(sql, str):
        sql_str = str(sql)
    else:
        sql_str = sql
    stripped = sql_str.strip().lower()
    if stripped.startswith("create") and "concurrently" in stripped:
        return False

    if _PSYCOPG3:
        conn_info = cursor.connection.info  # type: ignore[union-attr]
        if conn_info.transaction_status == _psycopg_pq.TransactionStatus.INERROR:
            return False

    return True


def _pgtrigger_execute_wrapper(
    execute: Callable[..., Any],
    sql: Any,
    params: Any,
    many: bool,  # noqa: FBT001
    context: dict[str, Any],
) -> Any:
    """Inject ``set_config('pgtrigger.ignore', …)`` when pgids are active.

    Mirrors ``pgtrigger.runtime._inject_pgtrigger_ignore`` but reads
    from a ``ContextVar`` instead of ``threading.local()``.

    Cost when idle: one ``ContextVar.get()`` + falsy check per statement.
    """
    pgids = _ignore_pgids.get()
    if not pgids:
        return execute(sql, params, many, context)

    if not _can_inject(context["cursor"], sql):
        return execute(sql, params, many, context)

    if not isinstance(sql, str):
        sql = sql.decode() if isinstance(sql, bytes) else str(sql)

    serialized = "{" + ",".join(pgids) + "}"
    sql = f"SELECT set_config('pgtrigger.ignore', %s, true); {sql}"
    params = [serialized, *(params or ())]

    result = execute(sql, params, many, context)

    # psycopg3: consume the extra result set from the SELECT statement.
    if _PSYCOPG3 and result is not None and hasattr(result, "nextset"):
        while result.nextset():
            pass

    return result


def _on_connection_created(
    sender: Any,
    connection: Any,
    **kwargs: Any,
) -> None:
    if (
        connection.vendor == "postgresql"
        and _pgtrigger_execute_wrapper not in connection.execute_wrappers
    ):
        connection.execute_wrappers.insert(0, _pgtrigger_execute_wrapper)


connection_created.connect(_on_connection_created)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class pgtrigger_ignore:  # noqa: N801
    """Async-compatible ``pgtrigger.ignore()`` replacement.

    Works as context manager (sync **and** async) and as decorator
    (sync **and** async).  Both paths use the same ``ContextVar``.

    Usage::

        # Context manager
        with pgtrigger_ignore("app.Model:trigger_name"):
            instance.save()

        async with pgtrigger_ignore("app.Model:trigger_name"):
            await instance.asave()

        # Decorator
        @pgtrigger_ignore("app.Model:trigger_name")
        def sync_fn(): ...

        @pgtrigger_ignore("app.Model:trigger_name")
        async def async_fn(): ...
    """

    def __init__(self, *uris: str) -> None:
        self._uris = uris
        self._token: Token[frozenset[str]] | None = None

    # -- context manager (shared logic) ------------------------------------

    def _enter(self) -> None:
        current = _ignore_pgids.get()
        new = _resolve_uris_to_pgids(self._uris)
        self._token = _ignore_pgids.set(current | new)

    def _exit(self) -> None:
        if self._token is not None:
            _ignore_pgids.reset(self._token)
            self._token = None

    def __enter__(self) -> Self:
        self._enter()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._exit()

    async def __aenter__(self) -> Self:
        self._enter()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._exit()

    # -- decorator ---------------------------------------------------------

    @overload
    def __call__(
        self,
        func: Callable[P, Awaitable[R]],
    ) -> Callable[P, Awaitable[R]]: ...

    @overload
    def __call__(
        self,
        func: Callable[P, T],
    ) -> Callable[P, T]: ...

    def __call__(
        self,
        func: Callable[P, T] | Callable[P, Awaitable[R]],
    ) -> Callable[P, T] | Callable[P, Awaitable[R]]:
        if inspect.iscoroutinefunction(func):
            async_func = cast("Callable[P, Awaitable[R]]", func)

            @functools.wraps(async_func)
            async def async_wrapper(
                *args: P.args,
                **kwargs: P.kwargs,
            ) -> R:
                async with pgtrigger_ignore(*self._uris):
                    return await async_func(*args, **kwargs)

            return async_wrapper

        sync_func = cast("Callable[P, T]", func)

        @functools.wraps(sync_func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            with pgtrigger_ignore(*self._uris):
                return sync_func(*args, **kwargs)

        return sync_wrapper
