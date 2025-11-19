import argparse
import contextlib
import os
import random
import shutil
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
        # if exc_type:
        #     self.restore()
        # else:
        #     self.cleanup()
        return False

    def backup(self):
        print('▶ Creating backup...')

        if self.dir_backup.exists():
            shutil.rmtree(self.dir_backup)
        self.dir_backup.mkdir()

        for filename in ('install-it.exe', 'bin', 'conf'):
            if not (path := Path(filename)).exists():
                continue
            shutil.move(path, self.dir_backup.joinpath(filename))

    def restore(self):
        print('▶ Restoring from backup...')

        for filename in ('install-it.exe', 'bin', 'conf'):
            if (newfile := Path(filename)).exists():
                if newfile.is_dir():
                    shutil.rmtree(newfile, True)
                else:
                    newfile.unlink()

            if not self.dir_backup.joinpath(filename).exists():
                continue
            shutil.move(self.dir_backup.joinpath(filename), newfile)

        shutil.rmtree(self.dir_backup, ignore_errors=True)

    def cleanup(self):
        print('▶ Cleaning up backup...')
        shutil.rmtree(self.dir_backup, ignore_errors=True)

    def replace_executable(self):
        print('▶ Downloading updates...')

        filename = f'install-it.{self.binary_type}-wv2.zip' if self.webview else f'install-it.{self.binary_type}.zip'
        url = f'https://github.com/install-it/install-it/releases/download/v{self.version_to}/{filename}'
        resp = requests.get(url, stream=True)

        if resp.headers.get('content-type') not in ('application/zip', 'application/octet-stream'):
            raise ValueError('Invalid version or binary type')

        with temporary_directory(dir=os.getcwd()) as tmpdir:
            fpath = tmpdir.joinpath(filename)

            print(f'  ↳ Downloading: {filename}')
            with (tqdm.tqdm(total=int(resp.headers['Content-Length']), unit='B', unit_scale=True) as progress,
                  open(fpath, 'wb') as f):
                for chunk in resp.iter_content(1024):
                    f.write(chunk)
                    progress.update(len(chunk))
                    progress.display()

            print('  ↳ Unpacking...')
            with zipfile.ZipFile(fpath, 'r') as z:
                for archive in tqdm.tqdm(z.filelist, unit='file'):
                    z.extract(archive.filename, str(tmpdir))

            print('  ↳ Updating files...')
            paths = (('install-it.exe', 'bin')
                     if self.webview else ('install-it.exe',))
            for path in map(Path, paths):
                if path.exists():
                    if path.is_dir():
                        shutil.rmtree(path, True)
                    else:
                        path.unlink()
                time.sleep(1)  # add wait time to avoid WinError5
                if tmpdir.joinpath(path).exists():
                    shutil.move(tmpdir.joinpath(path), Path(path))

    def migrate_config(self):
        print('▶ Migrating configuration...')
        shutil.copytree(self.dir_backup.joinpath('conf'), Path('conf'))
        
        if self.version_from.major == self.version_to.major:
            return
        if self.version_from.major == 1 and (self.version_to.major == 2 or self.version_to.major >= 5):
            raise NotImplementedError(
                f'Auto update from v{self.version_from.major} to v{self.version_to.major} is not supported yet.')

    def update(self) -> None:
        self.print_summary()
        self.replace_executable()
        self.migrate_config()

    def print_summary(self):
        print('+', '-'*26, '+')
        print('| {:13s}{:^13s} |'.format(
            'Update From', str(self.version_from)))
        print('| {:13s}{:^13s} |'.format('Update To', str(self.version_to)))
        print('| {:13s}{:^13s} |'.format('Binary', self.binary_type))
        print('| {:13s}{:^13s} |'.format(
            'WebView2', 'Yes' if self.webview else 'No'))
        print('+', '-'*26, '+', end='\n\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='install-it Updater')
    parser.add_argument('-d', '--app-directory', type=str,
                        help='Root directory of install-it')
    parser.add_argument('-s', '--version-from', type=str,
                        required=True, help='Update from which version')
    parser.add_argument('-t', '--version-to', type=str,
                        required=True, help='Update to which version')
    parser.add_argument('-b', '--binary-type', type=str,
                        required=True, help='Binary target')
    parser.add_argument('-w', '--webview', action='store_true',
                        help='Download built-in WebView2 version')
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

        print('✔ Update successful.')
    except Exception as e:
        print(f'✘ Update failed: {e}')
        input('Press any key to exit...')

    if input('Open the app? [Y]/N: ').lower() in ('y', ''):
        subprocess.Popen('install-it.exe')
