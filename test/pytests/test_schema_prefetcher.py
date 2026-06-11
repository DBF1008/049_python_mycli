# type: ignore

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from mycli import schema_prefetcher as schema_prefetcher_module
from mycli.schema_prefetcher import SchemaPrefetcher, parse_prefetch_config
from mycli.sqlcompleter import SQLCompleter


def test_parse_prefetch_config_never() -> None:
    assert parse_prefetch_config('never', []) == []
    assert parse_prefetch_config('NEVER', ['ignored', 'values']) == []
    assert parse_prefetch_config('  never  ', []) == []


def test_parse_prefetch_config_always() -> None:
    assert parse_prefetch_config('always', []) is None
    assert parse_prefetch_config('ALWAYS', []) is None
    assert parse_prefetch_config('  always  ', ['ignored']) is None


def test_parse_prefetch_config_listed() -> None:
    assert parse_prefetch_config('listed', ['foo', 'bar', 'baz']) == ['foo', 'bar', 'baz']
    assert parse_prefetch_config('LISTED', ['solo']) == ['solo']
    assert parse_prefetch_config('listed', []) == []


def test_parse_prefetch_config_unknown_mode_falls_back_to_always() -> None:
    assert parse_prefetch_config('unknown', ['ignored']) is None


def make_mycli(
    prefetch_mode: str = 'listed',
    prefetch_list: list[str] | None = None,
    dbname: str = 'current',
    databases=None,
):
    if prefetch_list is None:
        prefetch_list = []
    if databases is None:
        databases = ['current', 'other1', 'other2']
    completer = SQLCompleter(smart_completion=True)
    completer.set_dbname(dbname)
    sqlexecute = SimpleNamespace(
        dbname=dbname,
        user='u',
        password='p',
        host='h',
        port=3306,
        socket=None,
        character_set='utf8mb4',
        local_infile=False,
        ssl=None,
        ssh_user=None,
        ssh_host=None,
        ssh_port=22,
        ssh_password=None,
        ssh_key_filename=None,
        databases=MagicMock(return_value=list(databases)),
    )
    return SimpleNamespace(
        completer=completer,
        sqlexecute=sqlexecute,
        prefetch_schemas_mode=prefetch_mode,
        prefetch_schemas_list=prefetch_list,
        _completer_lock=threading.Lock(),
        prompt_session=None,
    )


def _fake_executor_factory(per_schema_tables, databases=None):
    """Build an executor stub whose schema-aware methods yield prebuilt rows."""

    def make(*_args, **_kwargs):
        executor = MagicMock()
        executor.databases.return_value = list(databases) if databases is not None else []
        executor.table_columns.side_effect = lambda schema=None: iter(per_schema_tables.get(schema, []))
        executor.foreign_keys.side_effect = lambda schema=None: iter([])
        executor.enum_values.side_effect = lambda schema=None: iter([])
        executor.functions.side_effect = lambda schema=None: iter([])
        executor.procedures.side_effect = lambda schema=None: iter([])
        executor.close = MagicMock()
        return executor

    return make


def test_start_configured_skips_current_and_prefetches_others(monkeypatch):
    mycli = make_mycli(prefetch_mode='listed', prefetch_list=['other1', 'current', 'other2'])
    tables = {
        'other1': [('users', 'id'), ('users', 'email')],
        'other2': [('orders', 'id')],
    }
    monkeypatch.setattr(schema_prefetcher_module, 'SQLExecute', _fake_executor_factory(tables))

    prefetcher = SchemaPrefetcher(mycli)
    prefetcher.start_configured()
    assert prefetcher._thread is not None
    prefetcher._thread.join(timeout=5)

    tables_meta = mycli.completer.dbmetadata['tables']
    assert 'other1' in tables_meta
    assert 'other2' in tables_meta
    # Current schema must be untouched by the prefetcher.
    assert 'current' not in tables_meta
    assert set(tables_meta['other1'].keys()) == {'users'}
    # Column list starts with '*' marker and contains escaped column names.
    assert tables_meta['other1']['users'][0] == '*'
    assert 'id' in tables_meta['other1']['users']


def test_start_configured_all_resolves_from_databases(monkeypatch):
    mycli = make_mycli(prefetch_mode='always', databases=['current', 'alpha', 'beta'])
    tables = {
        'alpha': [('t_a', 'c')],
        'beta': [('t_b', 'c')],
    }
    monkeypatch.setattr(
        schema_prefetcher_module,
        'SQLExecute',
        _fake_executor_factory(tables, databases=['current', 'alpha', 'beta']),
    )

    prefetcher = SchemaPrefetcher(mycli)
    prefetcher.start_configured()
    assert prefetcher._thread is not None
    prefetcher._thread.join(timeout=5)

    tables_meta = mycli.completer.dbmetadata['tables']
    assert 'alpha' in tables_meta
    assert 'beta' in tables_meta
    assert 'current' not in tables_meta


def test_start_configured_noop_when_disabled(monkeypatch):
    mycli = make_mycli(prefetch_mode='never')
    make_executor = MagicMock()
    monkeypatch.setattr(schema_prefetcher_module, 'SQLExecute', make_executor)

    prefetcher = SchemaPrefetcher(mycli)
    prefetcher.start_configured()

    assert prefetcher._thread is None
    make_executor.assert_not_called()


def test_prefetch_schema_now_loads_single_schema(monkeypatch):
    mycli = make_mycli(prefetch_mode='never')
    tables = {'target': [('t1', 'c1')]}
    monkeypatch.setattr(schema_prefetcher_module, 'SQLExecute', _fake_executor_factory(tables))

    prefetcher = SchemaPrefetcher(mycli)
    prefetcher.prefetch_schema_now('target')
    assert prefetcher._thread is not None
    prefetcher._thread.join(timeout=5)

    assert 'target' in mycli.completer.dbmetadata['tables']


def test_stop_interrupts_running_prefetch(monkeypatch):
    mycli = make_mycli(prefetch_mode='listed', prefetch_list=['a', 'b'])
    monkeypatch.setattr(
        schema_prefetcher_module,
        'SQLExecute',
        _fake_executor_factory({'a': [], 'b': []}),
    )

    prefetcher = SchemaPrefetcher(mycli)
    # Immediately cancel before any work runs.
    prefetcher._cancel.set()
    prefetcher._start(['a', 'b'])
    if prefetcher._thread is not None:
        prefetcher._thread.join(timeout=5)
    # stop() must be idempotent and leave the prefetcher ready to run again.
    prefetcher.stop()
    assert prefetcher._thread is None


def test_start_skips_schemas_already_in_completer(monkeypatch):
    """Previously-loaded schemas must not be re-fetched on refresh."""
    mycli = make_mycli(prefetch_mode='listed', prefetch_list=['keep', 'fresh'])
    # Simulate a schema that was already loaded (e.g., preserved via
    # copy_other_schemas_from after a completion refresh).
    mycli.completer.dbmetadata['tables']['keep'] = {'cached_table': ['*', 'c1']}

    executor_calls: list[str] = []

    def make(*_args, **_kwargs):
        executor = MagicMock()

        def _track(schema=None):
            executor_calls.append(schema)
            return iter([])

        executor.table_columns.side_effect = _track
        executor.foreign_keys.side_effect = lambda schema=None: iter([])
        executor.enum_values.side_effect = lambda schema=None: iter([])
        executor.functions.side_effect = lambda schema=None: iter([])
        executor.procedures.side_effect = lambda schema=None: iter([])
        executor.close = MagicMock()
        return executor

    monkeypatch.setattr(schema_prefetcher_module, 'SQLExecute', make)

    prefetcher = SchemaPrefetcher(mycli)
    prefetcher.start_configured()
    if prefetcher._thread is not None:
        prefetcher._thread.join(timeout=5)

    # Only 'fresh' is queried; 'keep' and 'current' are skipped.
    assert executor_calls == ['fresh']
    # Cached data for 'keep' is untouched.
    assert mycli.completer.dbmetadata['tables']['keep'] == {'cached_table': ['*', 'c1']}


def test_is_prefetching() -> None:
    mycli = make_mycli()
    prefetcher = SchemaPrefetcher(mycli)

    assert prefetcher.is_prefetching() is False

    class FakeThread:
        def is_alive(self) -> bool:
            return True

    prefetcher._thread = FakeThread()
    assert prefetcher.is_prefetching() is True


def test_stop_joins_alive_thread_and_resets_state() -> None:
    mycli = make_mycli()
    prefetcher = SchemaPrefetcher(mycli)
    old_cancel = prefetcher._cancel

    class FakeThread:
        def __init__(self) -> None:
            self.join_timeout: float | None = None

        def is_alive(self) -> bool:
            return True

        def join(self, timeout: float) -> None:
            self.join_timeout = timeout

    fake_thread = FakeThread()
    prefetcher._thread = fake_thread

    prefetcher.stop(timeout=1.5)

    assert old_cancel.is_set()
    assert fake_thread.join_timeout == 1.5
    assert prefetcher._thread is None
    assert prefetcher._cancel is not old_cancel


def test_prefetch_schema_now_ignores_empty_schema(monkeypatch) -> None:
    mycli = make_mycli()
    prefetcher = SchemaPrefetcher(mycli)
    stop = MagicMock()
    start = MagicMock()
    monkeypatch.setattr(prefetcher, 'stop', stop)
    monkeypatch.setattr(prefetcher, '_start', start)

    prefetcher.prefetch_schema_now('')

    stop.assert_not_called()
    start.assert_not_called()


def test_run_returns_when_database_listing_fails(monkeypatch) -> None:
    mycli = make_mycli()
    prefetcher = SchemaPrefetcher(mycli)
    executor = MagicMock()
    executor.databases.side_effect = RuntimeError('boom')
    executor.close = MagicMock()
    invalidate = MagicMock()
    monkeypatch.setattr(prefetcher, '_make_executor', lambda: executor)
    monkeypatch.setattr(prefetcher, '_invalidate_app', invalidate)

    prefetcher._run(None)

    executor.databases.assert_called_once_with()
    executor.close.assert_called_once_with()
    invalidate.assert_called_once_with()


def test_run_returns_when_cancelled_before_prefetch(monkeypatch) -> None:
    mycli = make_mycli()
    prefetcher = SchemaPrefetcher(mycli)
    executor = MagicMock()
    executor.close = MagicMock()
    prefetch = MagicMock()
    invalidate = MagicMock()
    prefetcher._cancel.set()
    monkeypatch.setattr(prefetcher, '_make_executor', lambda: executor)
    monkeypatch.setattr(prefetcher, '_prefetch_one', prefetch)
    monkeypatch.setattr(prefetcher, '_invalidate_app', invalidate)

    prefetcher._run(['schema1'])

    prefetch.assert_not_called()
    executor.close.assert_called_once_with()
    invalidate.assert_called_once_with()


def test_run_logs_prefetch_error_and_continues(monkeypatch) -> None:
    mycli = make_mycli()
    prefetcher = SchemaPrefetcher(mycli)
    executor = MagicMock()
    executor.close = MagicMock()
    invalidate = MagicMock()
    calls: list[str] = []

    def fake_prefetch(_executor, schema: str) -> None:
        calls.append(schema)
        if schema == 'bad':
            raise RuntimeError('boom')

    monkeypatch.setattr(prefetcher, '_make_executor', lambda: executor)
    monkeypatch.setattr(prefetcher, '_prefetch_one', fake_prefetch)
    monkeypatch.setattr(prefetcher, '_invalidate_app', invalidate)

    prefetcher._run(['bad', 'good'])

    assert calls == ['bad', 'good']
    executor.close.assert_called_once_with()
    invalidate.assert_called_once_with()


def test_prefetch_one_loads_foreign_keys_enums_functions_and_procedures(monkeypatch) -> None:
    mycli = make_mycli()
    load_schema_metadata = MagicMock()
    mycli.completer.load_schema_metadata = load_schema_metadata
    prefetcher = SchemaPrefetcher(mycli)
    invalidate = MagicMock()
    monkeypatch.setattr(prefetcher, '_invalidate_app', invalidate)

    executor = MagicMock()
    executor.table_columns.return_value = iter([('orders', 'id')])
    executor.foreign_keys.return_value = iter([('orders', 'user_id', 'users', 'id')])
    executor.enum_values.return_value = iter([('orders', 'status', ['pending', 'shipped'])])
    executor.functions.return_value = iter([(), ('calc_tax',), (None,)])
    executor.procedures.return_value = iter([None, ('rebuild_cache',), ('',)])

    prefetcher._prefetch_one(executor, 'analytics')

    load_schema_metadata.assert_called_once_with(
        schema='analytics',
        table_columns={'orders': ['*', 'id']},
        foreign_keys={
            'tables': {'orders': {'users'}, 'users': {'orders'}},
            'relations': [('orders', 'user_id', 'users', 'id')],
        },
        enum_values={'orders': {'status': ['pending', 'shipped']}},
        functions={'calc_tax': None},
        procedures={'rebuild_cache': None},
    )
    invalidate.assert_called_once_with()


def test_invalidate_app_calls_prompt_session_app() -> None:
    mycli = make_mycli()
    mycli.prompt_session = SimpleNamespace(app=SimpleNamespace(invalidate=MagicMock()))
    prefetcher = SchemaPrefetcher(mycli)

    prefetcher._invalidate_app()

    mycli.prompt_session.app.invalidate.assert_called_once_with()


def test_cross_schema_metadata_survives_completer_swap(monkeypatch):
    """Simulate USE + refresh + swap: previously-prefetched schemas must
    remain available in the new completer after copy_other_schemas_from."""
    mycli = make_mycli(prefetch_mode='always', dbname='db1', databases=['db1', 'db2', 'db3'])

    # Phase 1: prefetch db2 and db3 into the initial completer.
    tables = {
        'db2': [('users', 'id'), ('users', 'email')],
        'db3': [('orders', 'id')],
    }
    monkeypatch.setattr(
        schema_prefetcher_module,
        'SQLExecute',
        _fake_executor_factory(tables, databases=['db1', 'db2', 'db3']),
    )
    prefetcher = SchemaPrefetcher(mycli)
    prefetcher.start_configured()
    prefetcher._thread.join(timeout=5)

    assert 'db2' in mycli.completer.dbmetadata['tables']
    assert 'db3' in mycli.completer.dbmetadata['tables']
    old_completer = mycli.completer

    # Phase 2: simulate USE db2 → refresh_completions(reset=True).
    # Build a fresh completer for db2 (what the completion refresher does).
    new_completer = SQLCompleter(smart_completion=True)
    new_completer.extend_schemata('db2')
    new_completer.set_dbname('db2')
    # Populate db2 tables as the refresher would.
    new_completer.dbmetadata['tables']['db2'] = {'users': ['*', 'id', 'email']}

    # copy_other_schemas_from (the real callback does this under the lock).
    new_completer.copy_other_schemas_from(old_completer, exclude='db2')

    # Phase 3: swap.
    mycli.completer = new_completer
    mycli.sqlexecute.dbname = 'db2'

    # Cross-schema metadata for db1 and db3 must survive.
    assert 'db1' in mycli.completer.dbmetadata['tables']
    assert 'db3' in mycli.completer.dbmetadata['tables']
    assert 'db3' in mycli.completer.dbmetadata['tables']
    # db2 must have the fresh data (not whatever old completer had).
    assert mycli.completer.dbmetadata['tables']['db2'] == {'users': ['*', 'id', 'email']}

    # Phase 4: restart prefetch — it must NOT re-fetch db1 or db3 (already present).
    tracked: list[str] = []

    def tracking_factory(*_args, **_kwargs):
        executor = MagicMock()

        def _track(schema=None):
            tracked.append(schema)
            return iter([])

        executor.table_columns.side_effect = _track
        executor.foreign_keys.side_effect = lambda schema=None: iter([])
        executor.enum_values.side_effect = lambda schema=None: iter([])
        executor.functions.side_effect = lambda schema=None: iter([])
        executor.procedures.side_effect = lambda schema=None: iter([])
        executor.close = MagicMock()
        return executor

    monkeypatch.setattr(schema_prefetcher_module, 'SQLExecute', tracking_factory)

    prefetcher2 = SchemaPrefetcher(mycli)
    prefetcher2.start_configured()
    if prefetcher2._thread is not None:
        prefetcher2._thread.join(timeout=5)

    # Neither db1 nor db3 should be re-fetched.
    assert 'db1' not in tracked
    assert 'db3' not in tracked


def test_prefetch_one_writes_to_live_completer_after_swap(monkeypatch):
    """If the completer is swapped while _prefetch_one is building its
    payload, the metadata must be written to the *new* (live) completer,
    not the stale one captured before the lock."""
    mycli = make_mycli(prefetch_mode='never')
    old_completer = mycli.completer
    new_completer = SQLCompleter(smart_completion=True)
    new_completer.set_dbname('current')

    # Swap the completer mid-way through _prefetch_one.  We use a custom
    # lock that performs the swap on entry, simulating the race.
    swap_done = threading.Event()

    class SwapOnEnterLock:
        def __enter__(self):
            mycli.completer = new_completer
            swap_done.set()
            return self

        def __exit__(self, *exc):
            return False

    mycli._completer_lock = SwapOnEnterLock()

    prefetcher = SchemaPrefetcher(mycli)

    executor = MagicMock()
    executor.table_columns.return_value = iter([('t1', 'c1')])
    executor.foreign_keys.return_value = iter([])
    executor.enum_values.return_value = iter([])
    executor.functions.return_value = iter([])
    executor.procedures.return_value = iter([])

    prefetcher._prefetch_one(executor, 'other')

    # The metadata must be in the NEW completer, not the old one.
    assert 'other' in new_completer.dbmetadata['tables']
    assert 'other' not in old_completer.dbmetadata['tables']


def test_no_stale_loaded_set_skips_schemas(monkeypatch):
    """After a completer swap, schemas that were prefetched into the OLD
    completer but never transferred must still be fetched — there is no
    separate ``_loaded`` set that could incorrectly skip them."""
    mycli = make_mycli(prefetch_mode='listed', prefetch_list=['ghost', 'fresh'])

    # 'ghost' has NO metadata in the current completer (simulating a
    # schema whose data was lost during a swap).
    assert 'ghost' not in mycli.completer.dbmetadata['tables']

    tracked: list[str] = []

    def make(*_args, **_kwargs):
        executor = MagicMock()

        def _track(schema=None):
            tracked.append(schema)
            return iter([])

        executor.table_columns.side_effect = _track
        executor.foreign_keys.side_effect = lambda schema=None: iter([])
        executor.enum_values.side_effect = lambda schema=None: iter([])
        executor.functions.side_effect = lambda schema=None: iter([])
        executor.procedures.side_effect = lambda schema=None: iter([])
        executor.close = MagicMock()
        return executor

    monkeypatch.setattr(schema_prefetcher_module, 'SQLExecute', make)

    prefetcher = SchemaPrefetcher(mycli)
    prefetcher.start_configured()
    if prefetcher._thread is not None:
        prefetcher._thread.join(timeout=5)

    # Both 'ghost' and 'fresh' must be fetched — nothing should be skipped
    # by a stale bookkeeping set.
    assert 'ghost' in tracked
    assert 'fresh' in tracked
