"""Private SQLite backup and restoration check; never prints records or secrets."""
import os, sqlite3, sys, tempfile
from pathlib import Path
from . import store

def backup(destination):
    destination=Path(destination)
    # Refuse overwrite and create private permissions before writing any data.
    fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    try:
        with store.db() as source:
            target=sqlite3.connect(destination)
            try:source.backup(target)
            finally:target.close()
        return check_restore(destination)
    except BaseException:
        destination.unlink(missing_ok=True);raise

def check_restore(path):
    with tempfile.TemporaryDirectory() as directory:
        source=sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro',uri=True)
        target=sqlite3.connect(Path(directory)/'restored.sqlite3')
        try:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Backup integrity check failed')
            for table in ('jobs','meta','limits','service_budget'):
                if source.execute(f'SELECT count(*) FROM {table}').fetchone()!=target.execute(f'SELECT count(*) FROM {table}').fetchone():raise ValueError('Restoration differs')
            return True
        finally:source.close();target.close()

if __name__=='__main__':
    backup(sys.argv[1]);print('Private database backup and isolated restore check passed.')
