import argparse
import contextlib
import json
import os
import random
import shutil
import sqlite3
import string
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

import requests
import tqdm
from packaging import version


@contextlib.contextmanager
def temporary_directory(dir: str = None, delete: bool = True):
    def random_string() -> str:
        return ''.join(random.choices(string.ascii_letters + string.digits, k=8))

    dir_temp = Path(dir or tempfile.gettempdir()).joinpath(random_string())

    while dir_temp.exists():
        dir_temp = dir_temp.parent.joinpath(random_string())
    os.mkdir(dir_temp, 0o777)

    try:
        yield dir_temp
    finally:
        if delete:
            shutil.rmtree(dir_temp, True)


class Updater:

    dir_backup = Path('.backup')

    def __init__(self, version_from: str, version_to: str, binary_type: str, webview: bool):
        if self.dir_backup.exists():
            shutil.rmtree(self.dir_backup)
        self.dir_backup.mkdir()

        self.version_from = version.parse(version_from)
        self.version_to = version.parse(version_to)
        self.binary_type = binary_type
        self.webview = webview
        
        if self.version_from.major > self.version_to.major:
            raise ValueError('Downgrade is not supported!')

    def __enter__(self):
        self.backup()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.restore()
        else:
            self.cleanup()
        return False

    def backup(self):
        print('▶ Creating safety backup...')
        if self.dir_backup.exists():
            shutil.rmtree(self.dir_backup)
        self.dir_backup.mkdir()

        for filename in ('install-it.exe', 'conf'):
            path = Path(filename)
            if not path.exists():
                continue
            shutil.move(str(path), str(self.dir_backup.joinpath(filename)))

    def restore(self):
        print('▶ Restoring from backup due to failure...')
        for filename in ('install-it.exe', 'conf'):
            newfile = Path(filename)
            if newfile.exists():
                if newfile.is_dir():
                    shutil.rmtree(newfile, True)
                else:
                    newfile.unlink()

            backup_file = self.dir_backup.joinpath(filename)
            if not backup_file.exists():
                continue
            shutil.move(str(backup_file), str(newfile))

        shutil.rmtree(self.dir_backup, ignore_errors=True)

    def cleanup(self):
        print('▶ Cleaning up temporary update backup artifacts...')
        shutil.rmtree(self.dir_backup, ignore_errors=True)

    def replace_executable(self):
        print('▶ Downloading updates from GitHub releases...')

        if self.version_to.major >= 2:
            filename = f'install-it.{self.binary_type}-bundled.zip' if self.webview else f'install-it.{self.binary_type}.zip'
        else:
            filename = f'install-it.{self.binary_type}-wv2.zip' if self.webview else f'install-it.{self.binary_type}.zip'

        url = f'https://github.com/install-it/install-it/releases/download/v{self.version_to}/{filename}'
        resp = requests.get(url, stream=True)

        if resp.headers.get('content-type') not in ('application/zip', 'application/octet-stream'):
            raise ValueError('Invalid version or binary target type configuration')

        with temporary_directory(dir=os.getcwd()) as tmpdir:
            fpath = tmpdir.joinpath(filename)

            print(f'  ↳ Downloading payload: {filename}')
            with (tqdm.tqdm(total=int(resp.headers.get('Content-Length', 0)), unit='B', unit_scale=True) as progress,
                  open(fpath, 'wb') as f):
                for chunk in resp.iter_content(1024):
                    f.write(chunk)
                    progress.update(len(chunk))
                    progress.display()

            print('  ↳ Unpacking archive payload contents...')
            with zipfile.ZipFile(fpath, 'r') as z:
                for archive in tqdm.tqdm(z.filelist, unit='file'):
                    z.extract(archive.filename, str(tmpdir))

            print('  ↳ Deploying new files...')
            
            if self.version_to.major >= 2 and Path('bin').exists():
                shutil.rmtree('bin', ignore_errors=True)

            for item in tmpdir.iterdir():
                if item.name == filename:
                    continue
                
                dest_path = Path(item.name)
                time.sleep(0.5)
                
                if item.is_dir():
                    if item.name == 'internals':
                        if dest_path.exists():
                            shutil.rmtree(dest_path, True)
                        shutil.move(str(item), str(dest_path))
                else:
                    if dest_path.exists():
                        dest_path.unlink()
                    shutil.move(str(item), str(dest_path))

    def restore_and_migrate_config(self):
        print('▶ Restoring configuration profiles...')
        
        backup_conf = self.dir_backup.joinpath('conf')
        if backup_conf.exists():
            if Path('conf').exists():
                shutil.rmtree('conf', ignore_errors=True)
            shutil.move(str(backup_conf), 'conf')
            
        # ONE-TIME ONE-WAY SQLITE MIGRATION HACK
        # Converts legacy conf/groups.json directly into conf/data.db before Go boots up
        conf_dir = Path('conf')
        json_path = conf_dir / 'groups.json'
        db_path = conf_dir / 'data.db'

        if json_path.exists():
            print('  ↳ Found legacy groups.json. Migrating data to SQLite data.db...')
            try:
                conn = sqlite3.connect(str(db_path))
                cursor = conn.cursor()
                
                # Pre-initialize table layout in case Go migrations haven't initialized yet
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS driver_groups (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        type TEXT NOT NULL,
                        description TEXT
                    )
                ''')
                
                with open(json_path, 'r', encoding='utf-8') as f:
                    legacy_groups = json.load(f)
                    
                for group in legacy_groups:
                    name = group.get('name', '')
                    g_type = group.get('type', '')
                    desc = group.get('description', '')
                    
                    # Prevent duplication collisions
                    cursor.execute('SELECT 1 FROM driver_groups WHERE name = ? AND type = ?', (name, g_type))
                    if not cursor.fetchone():
                        cursor.execute(
                            'INSERT INTO driver_groups (name, type, description) VALUES (?, ?, ?)',
                            (name, g_type, desc)
                        )
                
                conn.commit()
                conn.close()
                
                # De-activate migration file safely via appending .bak suffix
                json_path.rename(json_path.with_suffix('.json.bak'))
                print('  ↳ Migration complete! groups.json converted to groups.json.bak')
            except Exception as e:
                print(f'  ⚠ Migration warning: Failed to convert legacy database assets: {e}')

    def update(self) -> None:
        self.print_summary()
        self.replace_executable()
        self.restore_and_migrate_config()

    def print_summary(self):
        print('+', '-'*26, '+')
        print('| {:13s}{:^13s} |'.format('Update From', str(self.version_from)))
        print('| {:13s}{:^13s} |'.format('Update To', str(self.version_to)))
        print('| {:13s}{:^13s} |'.format('Binary System', self.binary_type))
        print('| {:13s}{:^13s} |'.format('Payload Mode', 'Bundled Zip' if self.webview else 'Standard Zip'))
        print('+', '-'*26, '+', end='\n\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='install-it Legacy Transition Updater Bridge')
    parser.add_argument('-d', '--app-directory', type=str, help='Root directory of install-it')
    parser.add_argument('-s', '--version-from', type=str, required=True, help='Update from version')
    parser.add_argument('-t', '--version-to', type=str, required=True, help='Update to version')
    parser.add_argument('-b', '--binary-type', type=str, required=True, help='Binary target')
    parser.add_argument('-w', '--webview', action='store_true', help='Download built-in runtime dependencies')
    args = parser.parse_args()

    print(r'''
 _           _        _ _       _ _                        _       _            
(_)_ __  ___| |_ __ _| | |     (_) |_      _   _ _ __   __| | __ _| |_ ___ _ __ 
| | '_ \/ __| __/ _` | | |_____| | __|____| | | | '_ \ / _` |/ _` | __/ _ \ '__|
| | | | \__ \ || (_| | | |_____| | ||_____| |_| | |_) | (_| | (_| | ||  __/ |   
|_|_| |_|___/\__\__,_|_|_|     |_|\__|     \__,_| .__/ \__,_|\__,_|\__\___|_|   
                                                |_|                             
''')

    if args.app_directory:
        os.chdir(args.app_directory)

    try:
        with Updater(args.version_from, args.version_to, args.binary_type, args.webview) as updater:
            updater.update()
        print('✔ Update migration successful. Standalone updater execution cycle complete.')
    except Exception as e:
        print(f'✘ Update failed: {e}')
        input('Press any key to close and restore application elements...')
        exit(1)

    if input('Launch updated application now? [Y]/N: ').lower() in ('y', ''):
        DETACHED_PROCESS = 0x00000008
        subprocess.Popen(['install-it.exe'], creationflags=DETACHED_PROCESS, close_fds=True)
