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

parser = argparse.ArgumentParser(
    description="""A simple package manager for the Android apk""")
parser.add_argument("-v", "--version", action="version",
                    version="%(prog)s 0.1.0")
parser.add_argument("-u", "--url", help="URL of the remote repository (default: %(default)s)",
                    default='http://localhost:3000/')
parser.add_argument(
    "-r", "--root", help="root of the local repository (default: %(default)s)", default='app')
subparsers = parser.add_subparsers(dest='cmd')
subparsers.add_parser('update', help='update index from remote repository')
subparsers.add_parser('search', help='search apk').add_argument(
    "pattern", help="regex pattern to search", default='.', nargs="?")
subparsers.add_parser('list', help='list installed apk')
subparsers.add_parser('install', help='install apk').add_argument(
    "name", help="apk name", nargs="+")
subparsers.add_parser('uninstall', help='uninstall apk').add_argument(
    "name", help="apk name", nargs="+")


class Repo:
    def __init__(self, url='http://localhost:3000/', root='app'):
        # the remote repository URL
        self.repo_url = url
        if self.repo_url[-1] != '/':
            self.repo_url = self.repo_url + '/'

        # the local repository directory
        self.root_dir = root
        self.repo_dir = os.path.join(self.root_dir, 'repo')
        self.manifest = os.path.join(self.repo_dir, 'index.json')
        self.install_dir = os.path.join(self.root_dir, 'install')

        logger.debug("url: " + self.repo_url + ", root: " + self.root_dir)

        # apk information, apk name is the dict key
        self.apk_info = {}

        # see also: libcore/libart/src/main/java/dalvik/system/VMRuntime.java
        self.abi2isa = {'x86': 'x86', 'x86_64': 'x86_64',
                        'armeabi': 'arm', 'armeabi-v7a': 'arm', 'arm64-v8a': 'arm64'}
        self.abis = ['x86', 'x86_64', 'armeabi',
                     'armeabi-v7a', 'arm64-v8a']  # the order matters

    def ensure_dirs(self):
        for d in (self.root_dir, self.repo_dir, self.install_dir):
            if not os.path.exists(d):
                os.makedirs(d)

    def ensure_apk_info(self):
        if not self.apk_info:
            if not self.load_apk_info():
                self.update()

    def get_downloaded_apk(self, name):
        info = self.get_apk_info(name)
        if not info:
            logger.warning("can't find info for " + name)
            return
        path = info['path']
        dst = os.path.join(self.repo_dir, path)
        if os.path.exists(dst):
            return dst
        else:
            src = self.repo_url + path
            if self.download_apk(src, dst):
                return dst

    def get_installed_apk(self, name):
        return os.path.join(self.install_dir, name, name + '.apk')

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
                try:
                    info, raw_info = {}, json.load(f)
                    for item in raw_info:
                        name = '-'.join([item['package'], item['version']])
                        info[name] = item
                    self.apk_info = info
                    logger.debug(json.dumps(
                        info, sort_keys=True, ensure_ascii=False))
                    return True
                except json.JSONDecodeError as e:
                    logger.warning("invalid json file: " + e)
        else:
            logger.warning("no manifest found")

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
            result = [(n, info[n]) for n in sorted(info.keys())
                      if os.path.exists(self.get_installed_apk(n))]
            return result

    def install(self, name):
        self.ensure_dirs()
        path = self.get_downloaded_apk(name)
        logger.debug("install " + name + " from " + str(path))
        if path:
            to = self.get_installed_apk(name)
            try:
                d = os.path.dirname(to)
                if not os.path.exists(d):
                    os.makedirs(d)
                os.link(path, to)
                return self.deploy_apk_lib(to, d)
            except OSError as e:
                logger.warning(str(e))
        else:
            logger.warning("can't find: " + name)

    def uninstall(self, name):
        self.ensure_dirs()
        path = self.get_installed_apk(name)
        logger.debug("uninstall " + name + " at " + path)
        if os.path.exists(path):
            try:
                d = os.path.dirname(path)
                shutil.rmtree(d)
                return True
            except OSError as e:
                logger.warning(str(e))
        else:
            logger.warning("can't find: " + path)


def main():
    args = parser.parse_args()
    repo = Repo(url=args.url, root=args.root)
    if args.cmd == 'update':
        if repo.update():
            print("update success")
        else:
            print("update fail")
            sys.exit(1)
    elif args.cmd == 'search':
        result = repo.search(args.pattern)
        for name, info in result:
            print("{name:50s} {title:28s}".format(
                name=name, title=info['title']))
    elif args.cmd == 'list':
        result = repo.installed()
        for name, info in result:
            print("{name:50s} {title:28s}".format(
                name=name, title=info['title']))
    elif args.cmd == 'install':
        for name in args.name:
            if repo.install(name):
                print("install " + name + " success")
            else:
                print("install " + name + " fail")
                sys.exit(1)
    elif args.cmd == 'uninstall':
        for name in args.name:
            if repo.uninstall(name):
                print("uninstall " + name + " success")
            else:
                print("uninstall " + name + " fail")
                sys.exit(1)
    else:
        print('no such command')
        sys.exit(1)


if __name__ == '__main__':
    main()
