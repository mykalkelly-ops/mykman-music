"""Read-only source access; consistent SQLite backup and comparison verification.

Does not import the application, initialize schema, or restore databases.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def open_readonly(path):
    path = Path(path).resolve(strict=True)
    return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=60)


def comparisons(connection):
    cursor = connection.execute('SELECT * FROM comparisons ORDER BY id')
    columns = [column[0] for column in cursor.description]
    return columns, cursor.fetchall()


def integrity(connection):
    result = connection.execute('PRAGMA integrity_check').fetchall()
    if result != [('ok',)]:
        raise RuntimeError(f'SQLite integrity check failed: {result}')


def backup(source, destination):
    destination = Path(destination)
    # Exclusive creation prevents replacing any existing file, including source.
    with destination.open('xb'):
        pass
    try:
        with closing(open_readonly(source)) as src:
            dst = sqlite3.connect(destination)
            try:
                src.backup(dst)
                integrity(dst)
                columns, rows = comparisons(dst)
            finally:
                dst.close()
        export = destination.with_suffix(destination.suffix + '.comparisons.json')
        with export.open('x', encoding='utf-8') as handle:
            json.dump({'columns': columns, 'rows': rows, 'count': len(rows)}, handle, indent=2)
        return {'backup': str(destination), 'comparison_count': len(rows), 'export': str(export)}
    except Exception:
        # Keep failed artifacts for inspection; never label them successful.
        raise


def verify(source, baseline):
    with closing(open_readonly(baseline)) as old, closing(open_readonly(source)) as current:
        integrity(old)
        integrity(current)
        columns, before = comparisons(old)
        current_columns, after = comparisons(current)
        if columns != current_columns:
            raise RuntimeError('Comparison schema differs; manual review required')
        id_index = columns.index('id')
        by_id = {row[id_index]: row for row in after}
        changed = [row[id_index] for row in before if by_id.get(row[id_index]) != row]
        if changed:
            raise RuntimeError(f'{len(changed)} baseline comparisons missing or changed; first IDs: {changed[:10]}')
        return {'preserved_comparisons': len(before), 'current_comparisons': len(after),
                'additional_comparisons': len(after) - len(before)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    for operation, second in [('backup', 'destination'), ('verify', 'baseline')]:
        command = sub.add_parser(operation)
        command.add_argument('source', type=Path)
        command.add_argument(second, type=Path)
    args = parser.parse_args()
    result = (backup(args.source, args.destination) if args.operation == 'backup'
              else verify(args.source, args.baseline))
    print(json.dumps(result, indent=2))
