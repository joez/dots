#!/usr/bin/env python
# author: joe.zheng

import argparse
import logging
import subprocess
import re
import sys
import json
import os
import urllib.request
import shutil
import zipfile

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
    def __init__(self, url='http://localhost:3000/', root='app'):
        # the remote repository URL
        self.repo_url = url
        if self.repo_url[-1] != '/':
            self.repo_url = self.repo_url + '/'

        # the local repository directory
        self.root_dir = root
        self.cache_dir = os.path.join(self.root_dir, 'cache')
        self.installed_dir = os.path.join(self.root_dir, 'installed')
        self.manifest = os.path.join(self.cache_dir, 'index.json')

        logger.debug("url: " + self.repo_url + ", root: " + self.root_dir)

        # apk information, apk name is the dict key
        self.apk_info = {}

        # see also: libcore/libart/src/main/java/dalvik/system/VMRuntime.java
        self.abi2isa = {'x86': 'x86', 'x86_64': 'x86_64',
                        'armeabi': 'arm', 'armeabi-v7a': 'arm', 'arm64-v8a': 'arm64'}
        self.abis = ['x86', 'x86_64', 'armeabi',
                     'armeabi-v7a', 'arm64-v8a']  # the order matters

    def ensure_dirs(self):
        for d in (self.root_dir, self.cache_dir, self.installed_dir):
            if not os.path.exists(d):
                os.makedirs(d)

    def ensure_parent_dir(self, path):
        d = os.path.dirname(path)
        if d and not os.path.exists(d):
            os.makedirs(d)

    def ensure_apk_info(self):
        if not self.apk_info:
            if not self.load_apk_info():
                if not self.update():
                    raise RuntimeError(
                        "apk manifest can't be loaded successfully")

    def remote_apk_url(self, name):
        info = self.get_apk_info(name)
        if info and 'path' in info:
            return os.path.join(self.repo_url, info['path'])

    def cached_apk_path(self, name):
        info = self.get_apk_info(name)
        if info and 'path' in info:
            return os.path.join(self.cache_dir, info['path'])

    def installed_apk_path(self, name):
        return os.path.join(self.installed_dir, name, name + '.apk')

    def get_cached_apk(self, name):
        dst = self.cached_apk_path(name)
        if dst:
            if self.is_apk_cached(name):
                return dst
            else:
                src = self.remote_apk_url(name)
                size, info = None, self.get_apk_info(name)
                if info and 'size' in info:
                    size = info['size']
                if src and self.download_apk(src, dst, size=size):
                    return dst

    def is_apk_cached(self, name):
        path = self.cached_apk_path(name)
        if path and os.path.exists(path):
            # check the cached file is valid by file size
            # SHA256 or MD5 is too heavy for such a simple case
            info = self.get_apk_info(name)
            if info and 'size' in info:
                if os.path.getsize(path) == info['size']:
                    return True

    def is_apk_installed(self, name):
        return os.path.exists(self.installed_apk_path(name))

    def download_apk(self, src, dst, size=None):
        chunk = 1024 * 256
        try:
            with urllib.request.urlopen(src) as res:
                meta = res.info()
                logger.debug(meta)
                self.ensure_parent_dir(dst)
                length = int(meta.get('Content-Length', '0')
                             ) if size is None else size
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
        except urllib.error.URLError as e:
            logger.warning('error to download ' + src + ': ' + str(e))

    def deploy_apk_lib(self, src, dst):
        if not os.path.exists(dst):
            os.makedirs(dst)
        with zipfile.ZipFile(src) as zf:
            for f in zf.namelist():
                if f.startswith('lib/'):
                    zf.extract(f, dst)
        # convert ABI to ISA
        for abi in self.abis:
            if abi in self.abi2isa:
                isa = self.abi2isa[abi]
                if abi != isa:
                    abi_dir = os.path.join(dst, 'lib', abi)
                    isa_dir = os.path.join(dst, 'lib', isa)
                    if os.path.exists(abi_dir):
                        if os.path.exists(isa_dir):
                            logger.debug(
                                "the target ISA is already deployed, skip " + abi)
                        else:
                            os.rename(abi_dir, isa_dir)
            else:
                logger.warning("ABI " + abi + ' is not supported, skip')
        return True

    def get_apk_info(self, name):
        self.ensure_apk_info()
        if name in self.apk_info:
            return self.apk_info[name]

    def load_apk_info(self):
        if os.path.exists(self.manifest):
            with open(self.manifest, 'r') as f:
                info, raw_info = {}, json.load(f)
                for item in raw_info:
                    name = '-'.join([item['package'], item['version']])
                    info[name] = item
                self.apk_info = info
                return True

    def clean(self):
        if os.path.exists(self.cache_dir):
            # move the manifest to the root dir to prevent it from being deleted
            # and move back later
            m = os.path.join(self.root_dir, os.path.basename(self.manifest))
            try:
                os.rename(self.manifest, m)
                shutil.rmtree(self.cache_dir)
            finally:
                self.ensure_dirs()
                os.rename(m, self.manifest)
        return True

    def update(self):
        self.ensure_dirs()

        src = self.repo_url + 'index.json'
        dst = self.manifest
        try:
            with urllib.request.urlopen(src) as res:
                with open(dst, 'w') as f:
                    f.write(res.read().decode('utf-8'))
        except urllib.error.URLError as e:
            logger.warning('error to download ' + src + ': ' + str(e))
        return self.load_apk_info()

    def search(self, pattern):
        self.ensure_apk_info()
        pat = re.compile(pattern, flags=re.I)
        names = []
        for name, info in self.apk_info.items():
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
        result = [(k, self.apk_info[k]) for k in sorted(names)]
        return result

    def install(self, name, reinstall=False):
        self.ensure_dirs()
        path = self.get_cached_apk(name)
        logger.debug("install " + name + " from " + str(path))
        if path:
            if self.is_apk_installed(name):
                if reinstall:
                    self.uninstall(name, force=True)
                else:
                    logger.warning(name + " has already been installed")
                    return

            to = self.installed_apk_path(name)
            self.ensure_parent_dir(to)
            os.link(path, to)
            return self.deploy_apk_lib(to, os.path.dirname(to))
        else:
            logger.warning("can't find: " + name)

    def uninstall(self, name, all=False, force=False):
        if all:
            logger.debug("uninstall all")
            shutil.rmtree(self.installed_dir)
            self.ensure_dirs()
            return True
        else:
            path = self.installed_apk_path(name)
            logger.debug("uninstall " + name + " at " + path)
            if os.path.exists(path):
                shutil.rmtree(os.path.dirname(path))
                return True
            elif force:
                return True
            else:
                logger.warning("can't find: " + path)


def parse_args():
    parser = argparse.ArgumentParser(
        description='A simple apk manager', epilog="""It is similiar with the apt-get, 
        update the manifest from remote repository,
        search apk in the manifest, download it into the cache and install locally""",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-v', '--version', action='version',
                        version='%(prog)s 0.8.0')
    parser.add_argument('-u', '--url', help='URL of the remote repository',
                        default='http://localhost:3000/')
    parser.add_argument(
        '-r', '--root', help='root directory of the local repository', default='app')

    subps = parser.add_subparsers(
        dest='cmd', title='supported commands', metavar='COMMAND')

    p = subps.add_parser('clean', help='clean the cache')
    p = subps.add_parser('update', help='update manifest')
    p = subps.add_parser('search', help='search apk')
    p.add_argument('pattern', help='regex pattern', default='.', nargs='?')
    p.add_argument('-v', '--verbose', action='store_true',
                   help='more information')
    p = subps.add_parser('list', help='list installed apk')
    p.add_argument('pattern', help='regex pattern', default='.', nargs='?')
    p.add_argument('-v', '--verbose', action='store_true',
                   help='more information')
    p = subps.add_parser('install', help='install apk')
    p.add_argument('name', help='apk name', nargs='+')
    p.add_argument('-r', '--reinstall', action='store_true',
                   help='reinstall if necessary')
    p = subps.add_parser('uninstall', help='uninstall apk')
    p.add_argument('name', help='apk name or "all"', nargs='+')
    p.add_argument('-f', '--force', action='store_true',
                   help='no error if not found')

    return parser.parse_args()


def show_apks(apk_info, verbose=False):
    for name, info in apk_info:
        if verbose:
            print(name + ": " + json.dumps(info, ensure_ascii=False, indent=4))
        else:
            print("{name:50s} {title:28s}".format(
                name=name, title=info['title']))


def main():
    args = parse_args()
    repo = Repo(url=args.url, root=args.root)
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
        result = repo.search(args.pattern)
        show_apks(result, verbose=args.verbose)
    elif args.cmd == 'list':
        result = [(n, i) for (n, i) in repo.search(
            args.pattern) if repo.is_apk_installed(n)]
        show_apks(result, verbose=args.verbose)
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
            names = ['all']

        for name in names:
            if repo.uninstall(name, all=True if name is 'all' else False, force=args.force):
                print("uninstall " + name + " success")
            else:
                print("uninstall " + name + " fail")
                sys.exit(1)
    else:
        print('no such command')
        sys.exit(1)


if __name__ == '__main__':
    main()
