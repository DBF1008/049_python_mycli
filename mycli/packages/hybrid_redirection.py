import functools
import logging

import sqlglot
import sqlparse

from mycli.compat import WIN

logger = logging.getLogger(__name__)


def find_token_indices(tokens: list[sqlglot.Token]) -> dict[str, list[int]]:
    token_indices: dict[str, list[int]] = {
        'raw_dollar': [],
        'true_dollar': [],
        'angle_bracket': [],
        'pipe': [],
    }

    for i, tok in enumerate(tokens):
        if tok.token_type == sqlglot.TokenType.VAR and tok.text == '$':
            token_indices['raw_dollar'].append(i)
            continue
        if tok.token_type == sqlglot.TokenType.GT and (i - 1) in token_indices['raw_dollar']:
            token_indices['angle_bracket'].append(i)
            continue
        if tok.token_type == sqlglot.TokenType.PIPE and (i - 1) in token_indices['raw_dollar']:
            token_indices['pipe'].append(i)
            continue

    for i in token_indices['raw_dollar']:
        if (i + 1) in token_indices['angle_bracket'] or (i + 1) in token_indices['pipe']:
            token_indices['true_dollar'].append(i)

    return token_indices


def _count_statements(sql: str, delimiter: str) -> int:
    """Count SQL statements respecting the current delimiter.

    sqlglot.parse only understands ``;`` as a statement separator, so when the
    user has switched to a custom delimiter (e.g. ``$$``) we substitute it for
    ``;`` via sqlparse before counting.
    """
    if delimiter == ';':
        return len(sqlglot.parse(sql, read='mysql'))

    placeholder = '\ufffc'
    while placeholder in sql:
        placeholder += placeholder[0]
    substituted = sql.replace(';', placeholder).replace(delimiter, ';')
    split = sqlparse.split(substituted)
    return len([s for s in split if s.strip()])


def find_sql_part(
    command: str,
    tokens: list[sqlglot.Token],
    true_dollar_indices: list[int],
    delimiter: str,
):
    leftmost_dollar_pos = tokens[true_dollar_indices[0]].start
    sql_part = command[0:leftmost_dollar_pos].strip().removesuffix(delimiter).rstrip()
    try:
        statement_count = _count_statements(sql_part, delimiter)
    except sqlglot.errors.ParseError:
        return ''
    if statement_count != 1:
        return ''
    return sql_part


def find_command_tokens(
    tokens: list[sqlglot.Token],
    true_dollar_indices: list[int],
) -> list[sqlglot.Token]:
    command_part_tokens = []

    for i, tok in enumerate(tokens):
        if i < true_dollar_indices[0]:
            continue
        if i in true_dollar_indices:
            continue
        command_part_tokens.append(tok)

    if command_part_tokens:
        _operator = command_part_tokens.pop(0)

    return command_part_tokens


def find_file_tokens(
    tokens: list[sqlglot.Token],
    angle_bracket_indices: list[int],
) -> tuple[list[sqlglot.Token], int, str | None]:
    file_part_tokens: list[sqlglot.Token] = []
    file_part_index = len(tokens)

    if not angle_bracket_indices:
        return file_part_tokens, file_part_index, None

    file_part_tokens = tokens[angle_bracket_indices[-1] :]
    file_part_index = angle_bracket_indices[-1]

    file_operator_part = file_part_tokens.pop(0).text
    if file_operator_part == '>' and file_part_tokens[0].token_type == sqlglot.TokenType.GT:
        file_part_tokens.pop(0)
        file_operator_part = '>>'

    return file_part_tokens, file_part_index, file_operator_part


def assemble_tokens(tokens: list[sqlglot.Token], delimiter: str) -> str:
    assembled_string = ' ' * (tokens[-1].end + 10)
    for tok in tokens:
        if tok.token_type == sqlglot.TokenType.IDENTIFIER:
            text = f'"{tok.text}"'
            offset = 2
        elif tok.token_type == sqlglot.TokenType.STRING:
            text = f"'{tok.text}'"
            offset = 2
        else:
            text = tok.text
            offset = 0
        assembled_string = assembled_string[0 : tok.start] + text + assembled_string[tok.end + offset :]
    return assembled_string.strip().removesuffix(delimiter).rstrip()


def invalid_shell_part(
    file_part: str | None,
    command_part: str | None,
) -> bool:
    if file_part and ' ' in file_part:
        return True

    if file_part and '>' in file_part:
        return True

    if not file_part and not command_part:
        return True

    return False


@functools.lru_cache(maxsize=1)
def get_redirect_components(
    command: str,
    delimiter: str = ';',
) -> tuple[str | None, str | None, str | None, str | None]:
    """Get the parts of a hybrid shell-style redirect command.

    *delimiter* is part of the cache key so that switching the statement
    delimiter automatically invalidates any stale cached result.
    """

    try:
        tokens = sqlglot.tokenize(command)
    except sqlglot.errors.TokenError:
        return None, None, None, None

    token_indices = find_token_indices(tokens)

    if not token_indices['true_dollar']:
        return None, None, None, None

    if len(token_indices['angle_bracket']) > 1:
        return None, None, None, None

    if WIN and len(token_indices['pipe']) > 1:
        # how to give better feedback here?
        return None, None, None, None

    if token_indices['angle_bracket'] and token_indices['pipe']:
        if token_indices['pipe'][-1] > token_indices['angle_bracket'][-1]:
            return None, None, None, None

    sql_part = find_sql_part(
        command,
        tokens,
        token_indices['true_dollar'],
        delimiter,
    )
    if not sql_part:
        return None, None, None, None

    (
        file_part_tokens,
        file_part_index,
        file_operator_part,
    ) = find_file_tokens(
        tokens,
        token_indices['angle_bracket'],
    )

    command_part_tokens = find_command_tokens(
        tokens[0:file_part_index],
        token_indices['true_dollar'],
    )

    if file_part_tokens:
        file_part = assemble_tokens(file_part_tokens, delimiter)
    else:
        file_part = None

    if command_part_tokens:
        command_part = assemble_tokens(command_part_tokens, delimiter)
    else:
        command_part = None

    if invalid_shell_part(file_part, command_part):
        return None, None, None, None

    logger.debug('redirect parse sql_part: "{}"'.format(sql_part))
    logger.debug('redirect parse command_part: "{}"'.format(command_part))
    logger.debug('redirect parse file_operator_part: "{}"'.format(file_operator_part))
    logger.debug('redirect parse file_part: "{}"'.format(file_part))

    return sql_part, command_part, file_operator_part, file_part


def is_redirect_command(command: str, delimiter: str = ';') -> bool:
    """Is this a shell-style redirect to command or file?

    :param command: string
    :param delimiter: current statement delimiter

    """
    sql_part, _command_part, _file_operator_part, _file_part = get_redirect_components(command, delimiter)
    return bool(sql_part)
