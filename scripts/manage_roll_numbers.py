"""
Safe migration script to add a `roll_number` column and optionally populate it
in the users table. Works with SQLite and Postgres (Supabase).

Usage examples:
  # Dry-run using DATABASE_URL from environment
  python scripts/manage_roll_numbers.py --dry-run

  # Add column only (prompt for confirmation)
  python scripts/manage_roll_numbers.py --add-column

  # Add column and populate roll numbers (prompt for confirmation)
  python scripts/manage_roll_numbers.py --add-column --populate

  # Non-interactive (force yes)
  DATABASE_URL=postgresql://... python scripts/manage_roll_numbers.py --add-column --populate --yes

The script is careful: it checks whether the column already exists before altering
and supports a safe Python-based population for SQLite when window functions are not
available.
"""
from __future__ import annotations
import argparse
import os
import sys
from dotenv import load_dotenv
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.engine import Engine

# Load .env from project root if present
load_dotenv()

def get_engine(database_url: str) -> Engine:
    return create_engine(database_url)


def column_exists(engine: Engine, table_name: str, column_name: str) -> bool:
    insp = inspect(engine)
    if table_name not in insp.get_table_names():
        return False
    cols = [c['name'] for c in insp.get_columns(table_name)]
    return column_name in cols


def add_column(engine: Engine, table_name: str, column_name: str) -> None:
    dialect = engine.dialect.name
    if dialect == 'postgresql':
        sql = f'ALTER TABLE "{table_name}" ADD COLUMN IF NOT EXISTS {column_name} VARCHAR(64);'
        with engine.begin() as conn:
            conn.execute(text(sql))
            print('Added column (if missing) on Postgres.')
    elif dialect == 'sqlite':
        # SQLite does not support IF NOT EXISTS for ALTER TABLE ADD COLUMN
        # so ensure it's missing before adding.
        if column_exists(engine, table_name, column_name):
            print('Column already exists in SQLite; skipping ALTER TABLE.')
            return
        sql = f'ALTER TABLE "{table_name}" ADD COLUMN {column_name} TEXT;'
        with engine.begin() as conn:
            conn.execute(text(sql))
            print('Added column to SQLite database.')
    else:
        raise RuntimeError(f'Unsupported dialect: {dialect}')


def populate_roll_numbers_postgres(engine: Engine, table_name: str) -> None:
    # Uses SQL window function to assign sequential numbers per section alphabetically
    sql = f"""
    WITH ordered AS (
        SELECT id, section, username,
               ROW_NUMBER() OVER (PARTITION BY section ORDER BY username) AS rn
        FROM "{table_name}"
    )
    UPDATE "{table_name}" u
    SET roll_number = ('ES24AD' || LPAD(ordered.rn::text, 3, '0'))
    FROM ordered
    WHERE u.id = ordered.id;
    """
    with engine.begin() as conn:
        conn.execute(text(sql))
    print('Populated roll_number values on Postgres.')


def populate_roll_numbers_sqlite(engine: Engine, table_name: str) -> None:
    # SQLite: fetch ids ordered by section, username and update sequentially per section
    with engine.begin() as conn:
        rows = conn.execute(text(f"SELECT id, section, username FROM \"{table_name}\" ORDER BY section, username")).fetchall()
        if not rows:
            print('No rows found to populate.')
            return
        current_section = None
        rn = 0
        updates = []
        for r in rows:
            _id, section, username = r
            if section != current_section:
                current_section = section
                rn = 1
            else:
                rn += 1
            roll = f'ES24AD{rn:03d}'
            updates.append((_id, roll))
        for _id, roll in updates:
            conn.execute(text(f"UPDATE \"{table_name}\" SET roll_number = :roll WHERE id = :id"), {'roll': roll, 'id': _id})
    print('Populated roll_number values on SQLite.')


def preview_roll_numbers_sqlite(engine: Engine, table_name: str, limit: int = 20) -> None:
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT id, section, username FROM \"{table_name}\" ORDER BY section, username LIMIT :l"), {'l': limit}).fetchall()
        current_section = None
        rn = 0
        print('Preview (first', limit, 'rows ordered by section, username):')
        for r in rows:
            _id, section, username = r
            if section != current_section:
                current_section = section
                rn = 1
            else:
                rn += 1
            print(f'  id={_id} section={section} username={username} => ES24AD{rn:03d}')


def preview_roll_numbers_postgres(engine: Engine, table_name: str, limit: int = 20) -> None:
    sql = f"""
    SELECT id, section, username, ('ES24AD' || LPAD(rn::text,3,'0')) AS preview_roll
    FROM (
      SELECT id, section, username, ROW_NUMBER() OVER (PARTITION BY section ORDER BY username) rn
      FROM "{table_name}"
    ) s
    ORDER BY section, username
    LIMIT :l
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {'l': limit}).fetchall()
        print('Preview (first', limit, 'rows):')
        for r in rows:
            print(' ', r)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--database-url', default=os.environ.get('DATABASE_URL'), help='Database URL (SQLAlchemy style). If omitted, reads DATABASE_URL env var.')
    p.add_argument('--table', default='user', help='Users table name (default: user)')
    p.add_argument('--add-column', action='store_true', help='Add roll_number column if missing')
    p.add_argument('--populate', action='store_true', help='Populate roll_number values after adding column')
    p.add_argument('--dry-run', action='store_true', help='Show a preview of the changes without applying')
    p.add_argument('--yes', action='store_true', help='Do not prompt for confirmation')
    args = p.parse_args(argv)

    if not args.database_url:
        print('ERROR: DATABASE_URL must be provided via --database-url or DATABASE_URL env var')
        sys.exit(2)

    engine = get_engine(args.database_url)
    dialect = engine.dialect.name
    print('Connected to database, dialect=', dialect)

    if args.dry_run:
        # Show preview of roll numbers
        if dialect == 'postgresql':
            preview_roll_numbers_postgres(engine, args.table)
        else:
            preview_roll_numbers_sqlite(engine, args.table)
        return

    if args.add_column:
        if not args.yes:
            ok = input(f"About to add column 'roll_number' to table '{args.table}' in the configured database. Proceed? [y/N]: ")
            if ok.strip().lower() not in ('y','yes'):
                print('Aborted by user')
                return
        add_column(engine, args.table, 'roll_number')

    if args.populate:
        if not args.yes:
            ok = input(f"About to populate roll numbers for table '{args.table}' in the configured database. Proceed? [y/N]: ")
            if ok.strip().lower() not in ('y','yes'):
                print('Aborted by user')
                return
        if dialect == 'postgresql':
            populate_roll_numbers_postgres(engine, args.table)
        else:
            populate_roll_numbers_sqlite(engine, args.table)

if __name__ == '__main__':
    main()
