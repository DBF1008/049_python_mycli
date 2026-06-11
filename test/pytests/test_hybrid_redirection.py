from typing import Generator

import pytest
import sqlglot

from mycli.packages import hybrid_redirection


def tokenize(command: str) -> list[sqlglot.Token]:
    return sqlglot.tokenize(command)


@pytest.fixture(autouse=True)
def _clear_cache() -> Generator[None, None, None]:
    """Clear the lru_cache before each test so delimiter changes don't leak."""
    hybrid_redirection.get_redirect_components.cache_clear()
    yield
    hybrid_redirection.get_redirect_components.cache_clear()


@pytest.fixture()
def reset_hybrid_redirection(monkeypatch) -> Generator[None, None, None]:
    monkeypatch.setattr(hybrid_redirection, 'WIN', False)
    yield


def test_find_token_indices_tracks_true_dollars_and_operators() -> None:
    tokens = tokenize('select 1 $| cat $>> out.txt')

    assert hybrid_redirection.find_token_indices(tokens) == {
        'raw_dollar': [2, 5],
        'true_dollar': [2, 5],
        'angle_bracket': [6],
        'pipe': [3],
    }


def test_find_sql_part_handles_valid_parse_custom_delimiter_and_invalid_sql(reset_hybrid_redirection) -> None:
    valid_tokens = tokenize('select 1 $$ $> out.txt')
    assert hybrid_redirection.find_sql_part('select 1 $$ $> out.txt', valid_tokens, [3], '$$') == 'select 1'

    invalid_tokens = tokenize('select from $> out.txt')
    assert hybrid_redirection.find_sql_part('select from $> out.txt', invalid_tokens, [2], ';') == ''

    multiple_tokens = tokenize('select 1; select 2 $> out.txt')
    assert hybrid_redirection.find_sql_part('select 1; select 2 $> out.txt', multiple_tokens, [5], ';') == ''


def test_find_command_and_file_tokens_extract_expected_parts() -> None:
    tokens = tokenize('select 1 $| cat $>> out.txt')
    indices = hybrid_redirection.find_token_indices(tokens)

    file_tokens, file_index, operator = hybrid_redirection.find_file_tokens(tokens, indices['angle_bracket'])
    command_tokens = hybrid_redirection.find_command_tokens(tokens[0:file_index], indices['true_dollar'])

    assert operator == '>>'
    assert file_index == 6
    assert hybrid_redirection.assemble_tokens(file_tokens, ';') == 'out.txt'
    assert hybrid_redirection.assemble_tokens(command_tokens, ';') == 'cat'


def test_find_file_tokens_returns_empty_when_no_redirect_file() -> None:
    tokens = tokenize('select 1 $| cat')

    file_tokens, file_index, operator = hybrid_redirection.find_file_tokens(tokens, [])

    assert file_tokens == []
    assert file_index == len(tokens)
    assert operator is None


def test_assemble_tokens_quotes_identifier_and_string() -> None:
    identifier_tokens = tokenize('echo hi $> "quoted.txt"')[4:]
    string_tokens = tokenize("echo hi $| 'printf'")[4:]

    assert hybrid_redirection.assemble_tokens(identifier_tokens, ';') == '"quoted.txt"'
    assert hybrid_redirection.assemble_tokens(string_tokens, ';') == "'printf'"


@pytest.mark.parametrize(
    ('file_part', 'command_part', 'expected'),
    [
        ('two words.txt', None, True),
        ('bad>file.txt', None, True),
        (None, None, True),
        ('out.txt', None, False),
        (None, 'cat', False),
    ],
)
def test_invalid_shell_part(file_part: str | None, command_part: str | None, expected: bool) -> None:
    assert hybrid_redirection.invalid_shell_part(file_part, command_part) is expected


def test_get_redirect_components_valid_paths_and_logging() -> None:
    assert hybrid_redirection.get_redirect_components('select 1 $>> out.txt') == (
        'select 1',
        None,
        '>>',
        'out.txt',
    )
    assert hybrid_redirection.get_redirect_components('select 1 $| cat $> out.txt') == (
        'select 1',
        'cat',
        '>',
        'out.txt',
    )


def test_get_redirect_components_returns_none_on_token_error(monkeypatch) -> None:
    monkeypatch.setattr(
        hybrid_redirection.sqlglot, 'tokenize', lambda command: (_ for _ in ()).throw(sqlglot.errors.TokenError('bad token'))
    )

    assert hybrid_redirection.get_redirect_components('select 1 $> out.txt') == (None, None, None, None)


def test_get_redirect_components_rejects_invalid_forms() -> None:
    assert hybrid_redirection.get_redirect_components('select 1') == (None, None, None, None)
    assert hybrid_redirection.get_redirect_components('select 1 $> out.txt $> other.txt') == (None, None, None, None)
    assert hybrid_redirection.get_redirect_components('select 1 $> out.txt $| cat') == (None, None, None, None)
    assert hybrid_redirection.get_redirect_components('select from $> out.txt') == (None, None, None, None)
    assert hybrid_redirection.get_redirect_components('select 1 $> "two words.txt"') == (None, None, None, None)


def test_get_redirect_components_rejects_multiple_pipes_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(hybrid_redirection, 'WIN', True)

    assert hybrid_redirection.get_redirect_components('select 1 $| cat $| more') == (
        None,
        None,
        None,
        None,
    )


def test_is_redirect_command_reflects_component_parsing() -> None:
    assert hybrid_redirection.is_redirect_command('select 1 $| cat') is True
    assert hybrid_redirection.is_redirect_command('select 1') is False


# ---------------------------------------------------------------------------
# Custom delimiter tests
# ---------------------------------------------------------------------------


def test_custom_delimiter_redirect_output() -> None:
    """``select 1 $$ $> out.txt`` with delimiter ``$$`` should redirect to file."""
    assert hybrid_redirection.get_redirect_components('select 1 $$ $> out.txt', '$$') == (
        'select 1',
        None,
        '>',
        'out.txt',
    )


def test_custom_delimiter_redirect_append() -> None:
    """``select 1 $$ $>> out.txt`` with delimiter ``$$`` should append to file."""
    assert hybrid_redirection.get_redirect_components('select 1 $$ $>> out.txt', '$$') == (
        'select 1',
        None,
        '>>',
        'out.txt',
    )


def test_custom_delimiter_redirect_pipe() -> None:
    """``select 1 $$ $| cat`` with delimiter ``$$`` should pipe output."""
    assert hybrid_redirection.get_redirect_components('select 1 $$ $| cat', '$$') == (
        'select 1',
        'cat',
        None,
        None,
    )


def test_custom_delimiter_pipe_and_append() -> None:
    """``select 1 $$ $| cat $>> out.txt`` should pipe then append."""
    assert hybrid_redirection.get_redirect_components('select 1 $$ $| cat $>> out.txt', '$$') == (
        'select 1',
        'cat',
        '>>',
        'out.txt',
    )


def test_custom_delimiter_consistency_with_semicolon() -> None:
    """Same SQL body should produce identical components regardless of delimiter."""
    semi = hybrid_redirection.get_redirect_components('select 1 $> out.txt', ';')
    dollar = hybrid_redirection.get_redirect_components('select 1 $> out.txt', '$$')
    assert semi == dollar


def test_cache_invalidation_on_delimiter_change() -> None:
    """Switching delimiter between calls with the same command must not return stale results."""
    hybrid_redirection.get_redirect_components.cache_clear()

    # With ';' delimiter this is a valid redirect (sql = "select 1 $$")
    # but sqlglot treats $$ as a parameter marker so it parses as 1 statement.
    result_semi = hybrid_redirection.get_redirect_components('select 1 $$ $> out.txt', ';')

    # With '$$' delimiter, the SQL is "select 1" (delimiter stripped).
    result_dollar = hybrid_redirection.get_redirect_components('select 1 $$ $> out.txt', '$$')

    # Both should succeed, but the sql_part must differ.
    assert result_semi[0] != result_dollar[0]
    assert result_dollar[0] == 'select 1'


def test_custom_delimiter_rejects_multi_statement() -> None:
    """``select 1$$ select 2 $> out.txt`` with delimiter ``$$`` is multi-statement → reject."""
    assert hybrid_redirection.get_redirect_components('select 1$$ select 2 $> out.txt', '$$') == (
        None,
        None,
        None,
        None,
    )


def test_default_delimiter_pipe_and_append() -> None:
    """``select 1 $| cat $>> out.txt`` with default ``;`` delimiter."""
    assert hybrid_redirection.get_redirect_components('select 1 $| cat $>> out.txt', ';') == (
        'select 1',
        'cat',
        '>>',
        'out.txt',
    )


def test_is_redirect_command_with_custom_delimiter() -> None:
    assert hybrid_redirection.is_redirect_command('select 1 $$ $> out.txt', '$$') is True
    assert hybrid_redirection.is_redirect_command('select 1 $$ $> out.txt', ';') is True
    assert hybrid_redirection.is_redirect_command('select 1', '$$') is False
