from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def _default_db_path() -> Path:
    return Path(__file__).resolve().parents[1] / 'data' / 'server_manager.db'


def _checkpoint(db_path: Path) -> None:
    if not db_path.exists():
        return
    conn = sqlite3.connect(db_path)
    try:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.commit()
    finally:
        conn.close()


def _backup(db_path: Path) -> Path | None:
    if not db_path.exists():
        return None
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = db_path.with_name(f'{db_path.stem}.{stamp}.bak{db_path.suffix}')
    shutil.copy2(db_path, backup_path)
    return backup_path


def main() -> int:
    db_path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else _default_db_path()
    os.environ['SERVER_MANAGER_DB_PATH'] = str(db_path)

    _checkpoint(db_path)
    backup_path = _backup(db_path)

    script_dir = Path(__file__).resolve().parents[1]
    project_root = script_dir.parent
    for candidate in (project_root, script_dir):
        candidate_str = str(candidate)
        if candidate_str not in sys.path:
            sys.path.insert(0, candidate_str)

    import database

    reports = database.init_db()

    print(f'db_path={database.get_db_path()}')
    if backup_path is not None:
        print(f'backup={backup_path}')
    else:
        print('backup=SKIPPED (database not found before migration)')

    if not reports:
        print('migration=NOOP (already at target version)')
        return 0

    for idx, report in enumerate(reports, 1):
        print(f'migration[{idx}]={report}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
