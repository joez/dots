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

    def ensure_apk_info(self):
        if not self.apk_info:
            if not self.load_apk_info():
                if not self.update():
                    raise RuntimeError(
                        "apk manifest can't be loaded successfully")

    def remote_apk_url(self, name):
        path = self.get_apk_info(name)['path']
        return os.path.join(self.repo_url, path)

    def downloaded_apk_path(self, name):
        path = self.get_apk_info(name)['path']
        return os.path.join(self.cache_dir, path)

    def installed_apk_path(self, name):
        return os.path.join(self.installed_dir, name, name + '.apk')

    def get_downloaded_apk(self, name):
        dst = self.downloaded_apk_path(name)
        if os.path.exists(dst):
            return dst
        else:
            src = self.remote_apk_url(name)
            if self.download_apk(src, dst):
                return dst

    def is_apk_installed(self, name):
        return os.path.exists(self.installed_apk_path(name))

    def download_apk(self, src, dst):
        chunk = 1024 * 1024
        with urllib.request.urlopen(src) as res:
            d = os.path.dirname(dst)
            if not os.path.exists(d):
                os.makedirs(d)
            logger.info("downloading...")
            with open(dst, 'wb') as f:
                shutil.copyfileobj(res, f, chunk)
        return True

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
        with urllib.request.urlopen(src) as res:
            with open(dst, 'w') as f:
                f.write(res.read().decode('utf-8'))

        return self.load_apk_info()

    def search(self, pattern):
        self.ensure_apk_info()
        pat = re.compile(pattern, flags=re.I)
        names = []
        for k, v in self.apk_info.items():
            # search name first
            m = pat.search(k)
            if m:
                names.append(k)
                continue

            # search the other fields
            for v in ("title", "path"):
                m = pat.search(v)
                if m:
                    names.append(k)
                    break
        result = [(k, self.apk_info[k]) for k in sorted(names)]
        return result

    def installed(self):
        self.ensure_apk_info()
        info = self.apk_info
        if info:
            result = [(n, info[n])
                      for n in sorted(info.keys()) if self.is_apk_installed(n)]
            return result

    def install(self, name, reinstall=False):
        self.ensure_dirs()
        path = self.get_downloaded_apk(name)
        logger.debug("install " + name + " from " + str(path))
        if path:
            if self.is_apk_installed(name):
                if reinstall:
                    self.uninstall(name, force=True)
                else:
                    logger.warning(name + " has already been installed")
                    return

            to = self.installed_apk_path(name)
            d = os.path.dirname(to)
            if not os.path.exists(d):
                os.makedirs(d)
            os.link(path, to)
            return self.deploy_apk_lib(to, d)
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
        description='A simple package manager for the Android apk', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-v', '--version', action='version',
                        version='%(prog)s 0.1.0')
    parser.add_argument('-u', '--url', help='the remote repository',
                        default='http://localhost:3000/')
    parser.add_argument(
        '-l', '--local', help='the local repository', default='app')

    subps = parser.add_subparsers(
        dest='cmd', title='supported commands', metavar='COMMAND')

    p = subps.add_parser('clean', help='clean the cache')
    p = subps.add_parser('update', help='update manifest')
    p = subps.add_parser('search', help='search apk')
    p.add_argument('pattern', help='regex pattern', default='.', nargs='?')
    p.add_argument('-v', '--verbose', action='store_true',
                   help='more information')
    p = subps.add_parser('list', help='list installed apk')
    p = subps.add_parser('install', help='install apk')
    p.add_argument('name', help='apk name', nargs='+')
    p.add_argument('-r', '--reinstall', action='store_true',
                   help='reinstall if necessary')
    p = subps.add_parser('uninstall', help='uninstall apk')
    p.add_argument('name', help='apk name', nargs='*')
    p.add_argument('-a', '--all', action='store_true', help='uninstall all')
    p.add_argument('-f', '--force', action='store_true',
                   help='no error if not found')

    return parser.parse_args()


def main():
    args = parse_args()
    repo = Repo(url=args.url, root=args.local)
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
        for name, info in result:
            if args.verbose:
                print(name + ": " + json.dumps(info, ensure_ascii=False, indent=4))
            else:
                print("{name:50s} {title:28s}".format(
                    name=name, title=info['title']))
    elif args.cmd == 'list':
        result = repo.installed()
        for name, info in result:
            print("{name:50s} {title:28s}".format(
                name=name, title=info['title']))
    elif args.cmd == 'install':
        for name in args.name:
            if repo.install(name, reinstall=args.reinstall):
                print("install " + name + " success")
            else:
                print("install " + name + " fail")
                sys.exit(1)
    elif args.cmd == 'uninstall':
        for name in args.name:
            if repo.uninstall(name, all=args.all, force=args.force):
                print("uninstall " + name + " success")
            else:
                print("uninstall " + name + " fail")
                sys.exit(1)
    else:
        print('no such command')
        sys.exit(1)


if __name__ == '__main__':
    main()
