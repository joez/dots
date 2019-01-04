#!/usr/bin/env python
# author: joe.zheng

import argparse
import copy
import logging
import subprocess
import re
import sys
import json
import os
from string import Template

# hack here to ensure the current locale supports unicode correctly
import locale
if locale.getpreferredencoding() != 'UTF-8':
    locale.setlocale(locale.LC_CTYPE, 'en_US.UTF-8')

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)

parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter, description="""
check the APK information by 'aapt' and unify the file name with
the format '<package>-<version>.apk', and output a index file to
store the information, such as package name, version, title, etc.""")
parser.add_argument("-v", "--version", action="version",
                    version="%(prog)s 0.1.0")
parser.add_argument("-o", "--output",
                    help="the output folder for the index file (default: .)", default=".")
parser.add_argument("apk", help="APK file or directory", nargs="+")


def sh(cmd):
    logger.debug('sh: ' + ' '.join(cmd))
    return subprocess.check_output(cmd, universal_newlines=True)


def get_aapt_version():
    logger.debug('get_aapt_version')
    cmd = ['aapt', 'v']
    try:
        l = sh(cmd).splitlines()[0]
        m = re.match(r'Android Asset Packaging Tool,\s*(.+)$', l.rstrip())
        if m:
            return m.group(1)
    except Exception as e:
        logger.error(e)


def clean(s):
    return s.strip().strip("'")


def get_apk_info(path):
    logger.debug('get_apk_info: ' + path)

    info = {'launchable': []}
    cmd = ['aapt', 'd', 'badging', path]

    for l in sh(cmd).splitlines():
        if not 'title' in info:
            m = re.match(r"application-label(?:-en)?(?:-US)?:\s*(.+)$", l)
            if m:
                info['title'] = clean(m.group(1))
                continue

        m = re.match(r"application:\s*label='([^']+)'", l)
        if m:
            info['title'] = clean(m.group(1))
            continue

        m = re.match(r"package:\s*(.+)$", l)
        if m:
            for attr in re.split(r'\s+', m.group(1)):
                k, v = attr.split('=', 2)
                if k == 'name':
                    info['package'] = clean(v)
                    continue
                elif k == 'versionName':
                    info['version'] = clean(v)
                    continue
            continue

        # TODO: add "leanback-launchable-activity" to support TV
        m = re.match(r"launchable-activity:\s*(.+)$", l)
        if m:
            for attr in re.split(r'\s+', m.group(1)):
                k, v = attr.split('=', 2)
                if k == 'name':
                    info['launchable'].append(clean(v))
                    # enough for us to get one
                    break
            continue

    logger.debug(json.dumps(info, sort_keys=True, ensure_ascii=False))
    return info


def is_apk(path):
    logger.debug('is_apk: ' + path)
    return True if os.path.splitext(path)[1].lower() == '.apk' else False


def find_apk(paths):
    logger.debug('find_apk: ' + str(paths))
    for p in paths:
        logger.info('processing ' + p)
        if not os.path.exists(p):
            logger.warning('no such path, skip: ' + p)
            continue
        if os.path.isfile(p):
            if is_apk(p):
                logger.debug('found apk: ' + p)
                yield p
        elif os.path.isdir(p):
            for root, _, files in os.walk(p):
                for f in files:
                    if is_apk(f):
                        logger.debug('found apk: ' + f)
                        yield os.path.join(root, f)


def save_index(meta, path):
    logger.debug('save_index: ' + path)

    data_file = 'index.json'
    html_file = 'index.html'

    data = []
    for name in sorted(meta.keys()):
        d = copy.copy(meta[name])
        for k, v in d.items():
            # list is not easy to be saved into csv, so convert it into a string
            if isinstance(v, list):
                d[k] = ', '.join(v)
        data.append(d)

    with open(os.path.join(path, data_file), 'w') as f:
        f.write(json.dumps(data, sort_keys=True, ensure_ascii=False, indent=4))

    tmpl = '''<!doctype html>
<html lang="en">

<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, shrink-to-fit=no">

    <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.2.1/css/bootstrap.min.css" crossorigin="anonymous">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/tabulator/4.1.4/css/bootstrap/tabulator_bootstrap4.min.css">

    <title>APK info</title>
</head>

<body>
    <div class="container-fluid">
        <div class="row btn-group">
            <button type="button" class="btn btn-primary" id="download-csv">Download CSV</button>
            <button type="button" class="btn btn-primary" id="download-json">Download JSON</button>
            <button type="button" class="btn btn-primary" id="download-xlsx">Download XLSX</button>
        </div>
        <div class="row">
            <div id="table-body"></div>
        </div>
    </div>

    <script src="https://stackpath.bootstrapcdn.com/bootstrap/4.2.1/js/bootstrap.min.js" crossorigin="anonymous"></script>
    <script src="https://unpkg.com/tabulator-tables@4.1.4/dist/js/tabulator.min.js"></script>

    <script>
        var table = new Tabulator("#table-body", {
            dataTree: true,
            initialSort: [{ column: "path", dir: "asc" }],
            index: "path",
            ajaxURL:"$data",
            columns: [
                { title: "Title", field: "title", headerFilter: true },
                { title: "Package", field: "package", headerFilter: true },
                { title: "Version", field: "version", headerFilter: true },
                { title: "File", field: "path", formatter: "link", formatterParams:{ urlField: "path" }, headerFilter: true },
                { title: "Launchable", field: "launchable", headerFilter: true },
            ],
        });
        document.getElementById("download-csv").onclick = function () {
            table.download("csv", "apk-info.csv");
        };
        document.getElementById("download-json").onclick = function () {
            table.download("json", "apk-info.json");
        };
        document.getElementById("download-xlsx").onclick = function () {
            table.download("xlsx", "apk-info.xlsx", { sheetName: "data" });
        };
    </script>
    <script src="http://oss.sheetjs.com/js-xlsx/xlsx.full.min.js"></script>

</body>

</html>
    '''

    with open(os.path.join(path, html_file), 'w') as f:
        f.write(Template(tmpl).substitute(data=data_file))


def main():
    args = parser.parse_args()
    aapt = get_aapt_version()
    if not aapt:
        logger.error('no aapt found, stop')
        sys.exit(1)
    else:
        meta = {}
        seen = {}
        out  = args.output
        for src in find_apk(args.apk):
            logger.info('processing apk: ' + src)
            info = get_apk_info(src)
            name = '-'.join([info[k] for k in ['package', 'version']]) + '.apk'
            if name in seen:
                logger.warning(
                    'duplicated {}, already found {}, skip'.format(name, seen[name]))
            else:
                dst = os.path.join(os.path.dirname(src), name)
                if src == dst:
                    logger.info('no need to rename')
                else:
                    try:
                        os.rename(src, dst)
                        logger.info('renamed to ' + dst)
                    except os.error:
                        logger.warning('fail to rename, skip')
                        continue
                meta[name] = info
                seen[name] = dst
                # add path relative to the index file
                info['path'] = os.path.relpath(dst, start=out)

        logger.info('save index to ' + out)
        save_index(meta, out)


if __name__ == '__main__':
    main()
