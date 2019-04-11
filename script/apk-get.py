#!/usr/bin/env python3
# author: joe.zheng

import argparse
import logging
import json
import os
import re
import sys
import shutil
import zipfile

from contextlib import contextmanager
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.request import urlopen
from urllib.error import URLError

# hack here to ensure the current locale supports unicode correctly
import locale
if locale.getpreferredencoding() != 'UTF-8':
    locale.setlocale(locale.LC_CTYPE, 'en_US.UTF-8')

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)


class Progress:
    file = sys.stderr
    width = 32
    suffix = '%(percent)d%%'
    bar_prefix = ' |'
    bar_suffix = '| '
    empty_fill = ' '
    fill = '#'

    def __init__(self, message='', max=100, **kwargs):
        self.index = 0
        self.max = max
        for key, val in kwargs.items():
            setattr(self, key, val)

        self.message = message

    def __getitem__(self, key):
        if key.startswith('_'):
            return None
        return getattr(self, key, None)

    @property
    def progress(self):
        return min(1, self.index / self.max)

    @property
    def remaining(self):
        return max(self.max - self.index, 0)

    @property
    def percent(self):
        return self.progress * 100

    def update(self):
        filled_length = int(self.width * self.progress)
        empty_length = self.width - filled_length

        message = self.message % self
        bar = self.fill * filled_length
        empty = self.empty_fill * empty_length
        suffix = self.suffix % self
        line = ''.join([message, self.bar_prefix, bar, empty, self.bar_suffix,
                        suffix])
        self.writeln(line)

    def start(self):
        self.update()

    def writeln(self, line):
        if self.file:
            print('\r\x1b[K' + line, end='', file=self.file)
            self.file.flush()

    def finish(self):
        if self.file:
            print(file=self.file)

    def next(self, n=1):
        self.index = self.index + n
        self.update()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.finish()


class Repo:
    def __init__(self, index_url='http://localhost:3000/index.json', root_dir='app'):
        logger.debug("index_url: " + index_url + ", root: " + root_dir)

        self.index_url = index_url
        self.repo_url = index_url[:index_url.rindex('/')+1]
        logger.debug("repo_url: " + self.repo_url)

        # the local repository
        self.root_dir = root_dir
        self.cache_dir = os.path.join(self.root_dir, 'cache')
        self.installed_dir = os.path.join(self.root_dir, 'installed')
        self.index_file = os.path.join(self.cache_dir, 'index.json')

        # the loaded index, apk name is the dict key
        self.index = {}

        # see also: libcore/libart/src/main/java/dalvik/system/VMRuntime.java
        self.abi2isa = {'x86': 'x86', 'x86_64': 'x86_64',
                        'armeabi': 'arm', 'armeabi-v7a': 'arm', 'arm64-v8a': 'arm64'}
        self.abis = ['x86', 'x86_64', 'armeabi',
                     'armeabi-v7a', 'arm64-v8a']  # the order matters

    def _ensure_dirs(self):
        for d in (self.root_dir, self.cache_dir, self.installed_dir):
            if not os.path.exists(d):
                os.makedirs(d)

    def _ensure_parent_dir(self, path):
        d = os.path.dirname(path)
        if d and not os.path.exists(d):
            os.makedirs(d)

    def _ensure_index(self):
        if not self.index:
            if not self._load_index():
                if not self.update():
                    raise RuntimeError(
                        "apk index can't be loaded successfully")

    def _remote_url(self, name):
        info = self.get_info(name)
        if info and 'path' in info:
            return os.path.join(self.repo_url, info['path'])

    def _cached_path(self, name):
        info = self.get_info(name)
        if info and 'path' in info:
            return os.path.join(self.cache_dir, info['path'])

    def _installed_path(self, name):
        return os.path.join(self.installed_dir, name, name + '.apk')

    def _get_cached(self, name):
        dst = self._cached_path(name)
        if dst:
            if self.is_cached(name):
                return dst
            else:
                src = self._remote_url(name)
                size, info = None, self.get_info(name)
                if info and 'size' in info:
                    size = info['size']
                if src and self._download(src, dst, size=size):
                    return dst

    def is_cached(self, name):
        path = self._cached_path(name)
        if path and os.path.exists(path):
            # check the cached file is valid by file size
            # SHA256 or MD5 is too heavy for such a simple case
            info = self.get_info(name)
            if info and 'size' in info:
                if os.path.getsize(path) == info['size']:
                    return True
        return False

    def is_installed(self, name):
        return os.path.exists(self._installed_path(name))

    def _download(self, src, dst, size=None):
        chunk = 1024 * 256
        try:
            with urlopen(src) as res:
                logger.debug(str(res.info()))
                self._ensure_parent_dir(dst)
                length = res.length if size is None else size
                blocks = max(length // chunk, 1)
                prompt = "downloading " + os.path.basename(dst)
                with Progress(message=prompt, max=blocks) as bar:
                    with open(dst, 'wb') as f:
                        while True:
                            data = res.read(chunk)
                            if not data:
                                break
                            f.write(data)
                            bar.next()
            return True
        except URLError as e:
            logger.warning('error to download ' + src + ': ' + str(e))

    def _deploy_lib(self, apk):
        d = os.path.dirname(apk)
        with zipfile.ZipFile(apk) as zf:
            for f in zf.namelist():
                if f.startswith('lib/'):
                    zf.extract(f, d)
        # convert ABI to ISA
        for abi in self.abis:
            if abi in self.abi2isa:
                isa = self.abi2isa[abi]
                if abi != isa:
                    abi_dir = os.path.join(d, 'lib', abi)
                    isa_dir = os.path.join(d, 'lib', isa)
                    if os.path.exists(abi_dir):
                        if os.path.exists(isa_dir):
                            logger.debug(
                                "the target ISA is already deployed, skip " + abi)
                        else:
                            os.rename(abi_dir, isa_dir)
            else:
                logger.warning("ABI " + abi + ' is not supported, skip')
        return True

    def get_info(self, name):
        self._ensure_index()
        if name in self.index:
            return self.index[name]

    def _load_index(self):
        if os.path.exists(self.index_file):
            with open(self.index_file, 'r') as f:
                info, raw_info = {}, json.load(f)
                for item in raw_info:
                    name = '-'.join([item['package'], item['version']])
                    info[name] = item
                self.index = info
                return True

    def clean(self):
        if os.path.exists(self.cache_dir):
            # move the index to the root dir to prevent it from being deleted
            # and move back later
            m = os.path.join(self.root_dir, os.path.basename(self.index_file))
            try:
                os.rename(self.index_file, m)
                shutil.rmtree(self.cache_dir)
            finally:
                self._ensure_dirs()
                os.rename(m, self.index_file)
        return True

    def update(self):
        self._ensure_dirs()
        try:
            with urlopen(self.index_url) as res:
                with open(self.index_file, 'w') as f:
                    f.write(res.read().decode('utf-8'))
        except URLError as e:
            logger.warning('error to download ' +
                           self.index_url + ': ' + str(e))
            return
        return self._load_index()

    def search(self, pattern='.'):
        self._ensure_index()
        pat = re.compile(pattern, flags=re.I)
        names = []
        for name, info in self.index.items():
            # search name first
            m = pat.search(name)
            if m:
                names.append(name)
                continue

            # search the other fields
            for f in ("title", "path"):
                m = pat.search(info[f])
                if m:
                    names.append(name)
                    break
        result = []
        for n in sorted(names):
            i = dict(cached=self.is_cached(n), installed=self.is_installed(n))
            i.update(self.get_info(n))
            result.append((n, i))
        return result

    def installed(self, pattern='.'):
        return filter(lambda x: x[1]['installed'], self.search(pattern))

    def install(self, name, reinstall=False):
        self._ensure_dirs()
        path = self._get_cached(name)
        logger.debug("install " + name + " from " + str(path))
        if path:
            if self.is_installed(name):
                if reinstall:
                    self.uninstall(name, force=True)
                else:
                    logger.warning(name + " has already been installed")
                    return

            to = self._installed_path(name)
            self._ensure_parent_dir(to)
            os.link(path, to)
            return self._deploy_lib(to)
        else:
            logger.warning("can't find: " + name)

    def uninstall(self, name, force=False):
        path = self._installed_path(name)
        logger.debug("uninstall " + name + " at " + path)
        if os.path.exists(path):
            shutil.rmtree(os.path.dirname(path))
            return True
        elif force:
            return True
        else:
            logger.warning("can't find: " + path)

    def download(self, name, force=False):
        self._ensure_dirs()
        if force and self.is_cached(name):
            logger.debug(name + "is cached, unlink first")
            os.unlink(self._cached_path(name))
        if self._get_cached(name):
            return True


@contextmanager
def cd(newdir):
    prevdir = os.getcwd()
    os.chdir(os.path.expanduser(newdir))
    try:
        yield
    finally:
        os.chdir(prevdir)


def serve_dir(directory, bind='', port=3000):
    """Serve the directory as a HTTP web server

    It is not recommended for production
    """
    address = (bind, port)
    with cd(directory):
        with HTTPServer(address, SimpleHTTPRequestHandler) as httpd:
            sa = httpd.socket.getsockname()
            msg = "Serving HTTP on http://{host}:{port} for {root} ..."
            print(msg.format(host=sa[0], port=sa[1], root=directory))
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                sys.exit(0)


def show_apks(apk_info, pretty='default'):
    if pretty in ['s', 'short']:
        for name, _ in apk_info:
            print(name)
    elif pretty == ['v', 'verbose']:
        width = None
        for name, info in apk_info:
            if width is None:
                width = max(list(map(len, info.keys()))) + 1
            print(name + ":")
            for k in sorted(info.keys()):
                print("  {k:{w}} {v!s}".format(k=k+':', v=info[k], w=width))
            print('')
    else:
        for name, info in apk_info:
            stat = 'c' if info['cached'] else ' '
            if info['installed']:
                stat = stat + 'i'
            print("{n:48} {s:2} {t:28}".format(
                n=name, s=stat, t=info['title']))


def parse_args():
    fmt = argparse.ArgumentDefaultsHelpFormatter
    parser = argparse.ArgumentParser(
        description='A simple apk manager', epilog="""It is similiar with the apt-get, 
        update the index from remote repository,
        search apk in the index, download it into the cache and install locally""",
        formatter_class=fmt)
    parser.add_argument('-v', '--version', action='version',
                        version='%(prog)s 0.9.0')
    parser.add_argument('-i', '--index', help='index URL at the remote repository',
                        default='http://localhost:3000/index.json')
    parser.add_argument(
        '-r', '--root', help='root directory of the local repository', default='app')

    subps = parser.add_subparsers(
        dest='cmd', title='supported commands', metavar='COMMAND')

    p = subps.add_parser('clean', help='clean the cache', formatter_class=fmt)
    p = subps.add_parser('update', help='update index', formatter_class=fmt)
    p = subps.add_parser('search', help='search apk', formatter_class=fmt)
    p.add_argument('pattern', help='regex pattern', default='.', nargs='?')
    p.add_argument('-p', '--pretty', help='output format',
                   choices=['s', 'short', 'd', 'default', 'v', 'verbose'], default='d')
    p = subps.add_parser(
        'list', help='list installed apk', formatter_class=fmt)
    p.add_argument('pattern', help='regex pattern', default='.', nargs='?')
    p.add_argument('-p', '--pretty', help='output format',
                   choices=['s', 'short', 'd', 'default', 'v', 'verbose'], default='d')
    p = subps.add_parser('install', help='install apk', formatter_class=fmt)
    p.add_argument('name', help='apk name', nargs='+')
    p.add_argument('-r', '--reinstall', action='store_true',
                   help='reinstall if necessary')
    p = subps.add_parser(
        'uninstall', help='uninstall apk', formatter_class=fmt)
    p.add_argument('name', help='apk name or "all"', nargs='+')
    p.add_argument('-f', '--force', action='store_true',
                   help='no error if not found')
    p = subps.add_parser(
        'download', help='download apk into cache', formatter_class=fmt)
    p.add_argument('name', help='apk name or "all"', nargs='+')
    p.add_argument('-f', '--force', action='store_true',
                   help='download even if already cached')
    p = subps.add_parser(
        'serve', help='serve the cache as a remote repository', formatter_class=fmt)
    p.add_argument('-p', '--port', help='port number', type=int, default=3000)
    p.add_argument('-b', '--bind', help='bind address',
                   metavar='ADDRESS', default='')

    return parser.parse_args()


def main():
    args = parse_args()
    repo = Repo(index_url=args.index, root_dir=args.root)
    if args.cmd == 'clean':
        if repo.clean():
            print("clean success")
        else:
            print("clean fail")
            sys.exit(1)
    elif args.cmd == 'update':
        if repo.update():
            print("update success")
        else:
            print("update fail")
            sys.exit(1)
    elif args.cmd == 'search':
        result = repo.search(pattern=args.pattern)
        show_apks(result, pretty=args.pretty)
    elif args.cmd == 'list':
        result = repo.installed(pattern=args.pattern)
        show_apks(result, pretty=args.pretty)
    elif args.cmd == 'install':
        for name in args.name:
            if repo.install(name, reinstall=args.reinstall):
                print("install " + name + " success")
            else:
                print("install " + name + " fail")
                sys.exit(1)
    elif args.cmd == 'uninstall':
        names = args.name
        if 'all' in args.name:
            names = [name for name, _ in repo.installed()]

        for name in names:
            if repo.uninstall(name, force=args.force):
                print("uninstall " + name + " success")
            else:
                print("uninstall " + name + " fail")
                sys.exit(1)
    elif args.cmd == 'download':
        names = args.name
        if 'all' in args.name:
            names = [name for name, _ in repo.search()]

        for name in names:
            if repo.download(name, force=args.force):
                print("download " + name + " success")
            else:
                print("download " + name + " fail")
                sys.exit(1)
    elif args.cmd == 'serve':
        serve_dir(repo.cache_dir, bind=args.bind, port=args.port)
    else:
        print('no such command')
        sys.exit(1)


if __name__ == '__main__':
    main()
