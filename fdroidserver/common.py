#!/usr/bin/env python3
#
# common.py - part of the FDroid server tools
# Copyright (C) 2010-13, Ciaran Gultnieks, ciaran@ciarang.com
# Copyright (C) 2013-2014 Daniel Martí <mvdan@mvdan.cc>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

# common.py is imported by all modules, so do not import third-party
# libraries here as they will become a requirement for all commands.

import git
import io
import os
import sys
import re
import ast
import gzip
import shutil
import glob
import stat
import subprocess
import time
import operator
import logging
import hashlib
import socket
import base64
import urllib.parse
import urllib.request
import zipfile
import tempfile
import json

# TODO change to only import defusedxml once its installed everywhere
try:
    import defusedxml.ElementTree as XMLElementTree
except ImportError:
    import xml.etree.ElementTree as XMLElementTree  # nosec this is a fallback only

from binascii import hexlify
from datetime import datetime, timedelta, timezone
from distutils.version import LooseVersion
from queue import Queue
from zipfile import ZipFile

from pyasn1.codec.der import decoder, encoder
from pyasn1_modules import rfc2315
from pyasn1.error import PyAsn1Error

import fdroidserver.metadata
import fdroidserver.lint
from fdroidserver import _
from fdroidserver.exception import FDroidException, VCSException, NoSubmodulesException,\
    BuildException, VerificationException, MetaDataException
from .asynchronousfilereader import AsynchronousFileReader

# The path to this fdroidserver distribution
FDROID_PATH = os.path.realpath(os.path.join(os.path.dirname(__file__), '..'))

# this is the build-tools version, aapt has a separate version that
# has to be manually set in test_aapt_version()
MINIMUM_AAPT_VERSION = '26.0.0'

VERCODE_OPERATION_RE = re.compile(r'^([ 0-9/*+-]|%c)+$')

# A signature block file with a .DSA, .RSA, or .EC extension
SIGNATURE_BLOCK_FILE_REGEX = re.compile(r'^META-INF/.*\.(DSA|EC|RSA)$')
APK_NAME_REGEX = re.compile(r'^([a-zA-Z][\w.]*)_(-?[0-9]+)_?([0-9a-f]{7})?\.apk')
APK_ID_TRIPLET_REGEX = re.compile(r"^package: name='(\w[^']*)' versionCode='([^']+)' versionName='([^']*)'")
STANDARD_FILE_NAME_REGEX = re.compile(r'^(\w[\w.]*)_(-?[0-9]+)\.\w+')
FDROID_PACKAGE_NAME_REGEX = re.compile(r'''^[a-f0-9]+$''', re.IGNORECASE)
STRICT_APPLICATION_ID_REGEX = re.compile(r'''(?:^[a-zA-Z]+(?:\d*[a-zA-Z_]*)*)(?:\.[a-zA-Z]+(?:\d*[a-zA-Z_]*)*)+$''')
VALID_APPLICATION_ID_REGEX = re.compile(r'''(?:^[a-z_]+(?:\d*[a-zA-Z_]*)*)(?:\.[a-z_]+(?:\d*[a-zA-Z_]*)*)*$''',
                                        re.IGNORECASE)
ANDROID_PLUGIN_REGEX = re.compile(r'''\s*(:?apply plugin:|id)\(?\s*['"](android|com\.android\.application)['"]\s*\)?''')

SETTINGS_GRADLE_REGEX = re.compile(r'settings\.gradle(?:\.kts)?')
GRADLE_SUBPROJECT_REGEX = re.compile(r'''['"]:([^'"]+)['"]''')

MAX_VERSION_CODE = 0x7fffffff  # Java's Integer.MAX_VALUE (2147483647)

XMLNS_ANDROID = '{http://schemas.android.com/apk/res/android}'

config = None
options = None
env = None
orig_path = None


default_config = {
    'sdk_path': "$ANDROID_HOME",
    'ndk_paths': {
        'r10e': None,
        'r11c': None,
        'r12b': "$ANDROID_NDK",
        'r13b': None,
        'r14b': None,
        'r15c': None,
        'r16b': None,
    },
    'cachedir': os.path.join(os.getenv('HOME'), '.cache', 'fdroidserver'),
    'build_tools': MINIMUM_AAPT_VERSION,
    'force_build_tools': False,
    'java_paths': None,
    'scan_binary': False,
    'ant': "ant",
    'mvn3': "mvn",
    'gradle': os.path.join(FDROID_PATH, 'gradlew-fdroid'),
    'gradle_version_dir': os.path.join(os.path.join(os.getenv('HOME'), '.cache', 'fdroidserver'), 'gradle'),
    'accepted_formats': ['txt', 'yml'],
    'sync_from_local_copy_dir': False,
    'allow_disabled_algorithms': False,
    'per_app_repos': False,
    'make_current_version_link': True,
    'current_version_name_source': 'Name',
    'deploy_process_logs': False,
    'update_stats': False,
    'stats_ignore': [],
    'stats_server': None,
    'stats_user': None,
    'stats_to_carbon': False,
    'repo_maxage': 0,
    'build_server_always': False,
    'keystore': 'keystore.jks',
    'smartcardoptions': [],
    'char_limits': {
        'author': 256,
        'name': 50,
        'summary': 80,
        'description': 4000,
        'video': 256,
        'whatsNew': 500,
    },
    'keyaliases': {},
    'repo_url': "https://MyFirstFDroidRepo.org/fdroid/repo",
    'repo_name': "My First FDroid Repo Demo",
    'repo_icon': "fdroid-icon.png",
    'repo_description': _('''
        This is a repository of apps to be used with FDroid. Applications in this
        repository are either official binaries built by the original application
        developers, or are binaries built from source by f-droid.org using the
        tools on https://gitlab.com/fdroid.
        '''),
    'archive_description': _('These are the apps that have been archived from the main repo.'),
    'archive_older': 0,
    'lint_licenses': fdroidserver.lint.APPROVED_LICENSES,
    'git_mirror_size_limit': 10000000000,
}


def setup_global_opts(parser):
    try:  # the buildserver VM might not have PIL installed
        from PIL import PngImagePlugin
        logger = logging.getLogger(PngImagePlugin.__name__)
        logger.setLevel(logging.INFO)  # tame the "STREAM" debug messages
    except ImportError:
        pass

    parser.add_argument("-v", "--verbose", action="store_true", default=False,
                        help=_("Spew out even more information than normal"))
    parser.add_argument("-q", "--quiet", action="store_true", default=False,
                        help=_("Restrict output to warnings and errors"))


def _add_java_paths_to_config(pathlist, thisconfig):
    def path_version_key(s):
        versionlist = []
        for u in re.split('[^0-9]+', s):
            try:
                versionlist.append(int(u))
            except ValueError:
                pass
        return versionlist

    for d in sorted(pathlist, key=path_version_key):
        if os.path.islink(d):
            continue
        j = os.path.basename(d)
        # the last one found will be the canonical one, so order appropriately
        for regex in [
                r'^1\.([16-9][0-9]?)\.0\.jdk$',  # OSX
                r'^jdk1\.([16-9][0-9]?)\.0_[0-9]+.jdk$',  # OSX and Oracle tarball
                r'^jdk1\.([16-9][0-9]?)\.0_[0-9]+$',  # Oracle Windows
                r'^jdk([16-9][0-9]?)-openjdk$',  # Arch
                r'^java-([16-9][0-9]?)-openjdk$',  # Arch
                r'^java-([16-9][0-9]?)-jdk$',  # Arch (oracle)
                r'^java-1\.([16-9][0-9]?)\.0-.*$',  # RedHat
                r'^java-([16-9][0-9]?)-oracle$',  # Debian WebUpd8
                r'^jdk-([16-9][0-9]?)-oracle-.*$',  # Debian make-jpkg
                r'^java-([16-9][0-9]?)-openjdk-[^c][^o][^m].*$',  # Debian
                r'^oracle-jdk-bin-1\.([17-9][0-9]?).*$',  # Gentoo (oracle)
                r'^icedtea-bin-([17-9][0-9]?).*$',  # Gentoo (openjdk)
                ]:
            m = re.match(regex, j)
            if not m:
                continue
            for p in [d, os.path.join(d, 'Contents', 'Home')]:
                if os.path.exists(os.path.join(p, 'bin', 'javac')):
                    thisconfig['java_paths'][m.group(1)] = p


def fill_config_defaults(thisconfig):
    for k, v in default_config.items():
        if k not in thisconfig:
            thisconfig[k] = v

    # Expand paths (~users and $vars)
    def expand_path(path):
        if path is None:
            return None
        orig = path
        path = os.path.expanduser(path)
        path = os.path.expandvars(path)
        if orig == path:
            return None
        return path

    for k in ['sdk_path', 'ant', 'mvn3', 'gradle', 'keystore', 'repo_icon']:
        v = thisconfig[k]
        exp = expand_path(v)
        if exp is not None:
            thisconfig[k] = exp
            thisconfig[k + '_orig'] = v

    # find all installed JDKs for keytool, jarsigner, and JAVA[6-9]_HOME env vars
    if thisconfig['java_paths'] is None:
        thisconfig['java_paths'] = dict()
        pathlist = []
        pathlist += glob.glob('/usr/lib/jvm/j*[16-9]*')
        pathlist += glob.glob('/usr/java/jdk1.[16-9]*')
        pathlist += glob.glob('/System/Library/Java/JavaVirtualMachines/1.[16-9][0-9]?.0.jdk')
        pathlist += glob.glob('/Library/Java/JavaVirtualMachines/*jdk*[0-9]*')
        pathlist += glob.glob('/opt/oracle-jdk-*1.[0-9]*')
        pathlist += glob.glob('/opt/icedtea-*[0-9]*')
        if os.getenv('JAVA_HOME') is not None:
            pathlist.append(os.getenv('JAVA_HOME'))
        if os.getenv('PROGRAMFILES') is not None:
            pathlist += glob.glob(os.path.join(os.getenv('PROGRAMFILES'), 'Java', 'jdk1.[16-9][0-9]?.*'))
        _add_java_paths_to_config(pathlist, thisconfig)

    for java_version in ('14', '13', '12', '11', '10', '9', '8', '7'):
        if java_version not in thisconfig['java_paths']:
            continue
        java_home = thisconfig['java_paths'][java_version]
        jarsigner = os.path.join(java_home, 'bin', 'jarsigner')
        if os.path.exists(jarsigner):
            thisconfig['jarsigner'] = jarsigner
            thisconfig['keytool'] = os.path.join(java_home, 'bin', 'keytool')
            break

    if 'jarsigner' not in thisconfig and shutil.which('jarsigner'):
        thisconfig['jarsigner'] = shutil.which('jarsigner')
    if 'keytool' not in thisconfig and shutil.which('keytool'):
        thisconfig['keytool'] = shutil.which('keytool')

    for k in ['ndk_paths', 'java_paths']:
        d = thisconfig[k]
        for k2 in d.copy():
            v = d[k2]
            exp = expand_path(v)
            if exp is not None:
                thisconfig[k][k2] = exp
                thisconfig[k][k2 + '_orig'] = v


def regsub_file(pattern, repl, path):
    with open(path, 'rb') as f:
        text = f.read()
    text = re.sub(bytes(pattern, 'utf8'), bytes(repl, 'utf8'), text)
    with open(path, 'wb') as f:
        f.write(text)


def read_config(opts, config_file='config.py'):
    """Read the repository config

    The config is read from config_file, which is in the current
    directory when any of the repo management commands are used. If
    there is a local metadata file in the git repo, then config.py is
    not required, just use defaults.

    """
    global config, options

    if config is not None:
        return config

    options = opts

    config = {}

    if os.path.isfile(config_file):
        logging.debug(_("Reading '{config_file}'").format(config_file=config_file))
        with io.open(config_file, "rb") as f:
            code = compile(f.read(), config_file, 'exec')
            exec(code, None, config)  # nosec TODO switch to YAML file
    else:
        logging.warning(_("No 'config.py' found, using defaults."))

    for k in ('mirrors', 'install_list', 'uninstall_list', 'serverwebroot', 'servergitroot'):
        if k in config:
            if not type(config[k]) in (str, list, tuple):
                logging.warning(
                    _("'{field}' will be in random order! Use () or [] brackets if order is important!")
                    .format(field=k))

    # smartcardoptions must be a list since its command line args for Popen
    if 'smartcardoptions' in config:
        config['smartcardoptions'] = config['smartcardoptions'].split(' ')
    elif 'keystore' in config and config['keystore'] == 'NONE':
        # keystore='NONE' means use smartcard, these are required defaults
        config['smartcardoptions'] = ['-storetype', 'PKCS11', '-providerName',
                                      'SunPKCS11-OpenSC', '-providerClass',
                                      'sun.security.pkcs11.SunPKCS11',
                                      '-providerArg', 'opensc-fdroid.cfg']

    if any(k in config for k in ["keystore", "keystorepass", "keypass"]):
        st = os.stat(config_file)
        if st.st_mode & stat.S_IRWXG or st.st_mode & stat.S_IRWXO:
            logging.warning(_("unsafe permissions on '{config_file}' (should be 0600)!")
                            .format(config_file=config_file))

    fill_config_defaults(config)

    for k in ["repo_description", "archive_description"]:
        if k in config:
            config[k] = clean_description(config[k])

    if 'serverwebroot' in config:
        if isinstance(config['serverwebroot'], str):
            roots = [config['serverwebroot']]
        elif all(isinstance(item, str) for item in config['serverwebroot']):
            roots = config['serverwebroot']
        else:
            raise TypeError(_('only accepts strings, lists, and tuples'))
        rootlist = []
        for rootstr in roots:
            # since this is used with rsync, where trailing slashes have
            # meaning, ensure there is always a trailing slash
            if rootstr[-1] != '/':
                rootstr += '/'
            rootlist.append(rootstr.replace('//', '/'))
        config['serverwebroot'] = rootlist

    if 'servergitmirrors' in config:
        if isinstance(config['servergitmirrors'], str):
            roots = [config['servergitmirrors']]
        elif all(isinstance(item, str) for item in config['servergitmirrors']):
            roots = config['servergitmirrors']
        else:
            raise TypeError(_('only accepts strings, lists, and tuples'))
        config['servergitmirrors'] = roots

        limit = config['git_mirror_size_limit']
        config['git_mirror_size_limit'] = parse_human_readable_size(limit)

    return config


def parse_human_readable_size(size):
    units = {
        'b': 1,
        'kb': 1000, 'mb': 1000**2, 'gb': 1000**3, 'tb': 1000**4,
        'kib': 1024, 'mib': 1024**2, 'gib': 1024**3, 'tib': 1024**4,
    }
    try:
        return int(float(size))
    except (ValueError, TypeError):
        if type(size) != str:
            raise ValueError(_('Could not parse size "{size}", wrong type "{type}"')
                             .format(size=size, type=type(size)))
        s = size.lower().replace(' ', '')
        m = re.match(r'^(?P<value>[0-9][0-9.]+) *(?P<unit>' + r'|'.join(units.keys()) + r')$', s)
        if not m:
            raise ValueError(_('Not a valid size definition: "{}"').format(size))
        return int(float(m.group("value")) * units[m.group("unit")])


def assert_config_keystore(config):
    """Check weather keystore is configured correctly and raise exception if not."""

    nosigningkey = False
    if 'repo_keyalias' not in config:
        nosigningkey = True
        logging.critical(_("'repo_keyalias' not found in config.py!"))
    if 'keystore' not in config:
        nosigningkey = True
        logging.critical(_("'keystore' not found in config.py!"))
    elif not os.path.exists(config['keystore']):
        nosigningkey = True
        logging.critical("'" + config['keystore'] + "' does not exist!")
    if 'keystorepass' not in config:
        nosigningkey = True
        logging.critical(_("'keystorepass' not found in config.py!"))
    if 'keypass' not in config:
        nosigningkey = True
        logging.critical(_("'keypass' not found in config.py!"))
    if nosigningkey:
        raise FDroidException("This command requires a signing key, "
                              + "you can create one using: fdroid update --create-key")


def find_sdk_tools_cmd(cmd):
    '''find a working path to a tool from the Android SDK'''

    tooldirs = []
    if config is not None and 'sdk_path' in config and os.path.exists(config['sdk_path']):
        # try to find a working path to this command, in all the recent possible paths
        if 'build_tools' in config:
            build_tools = os.path.join(config['sdk_path'], 'build-tools')
            # if 'build_tools' was manually set and exists, check only that one
            configed_build_tools = os.path.join(build_tools, config['build_tools'])
            if os.path.exists(configed_build_tools):
                tooldirs.append(configed_build_tools)
            else:
                # no configed version, so hunt known paths for it
                for f in sorted(os.listdir(build_tools), reverse=True):
                    if os.path.isdir(os.path.join(build_tools, f)):
                        tooldirs.append(os.path.join(build_tools, f))
                tooldirs.append(build_tools)
        sdk_tools = os.path.join(config['sdk_path'], 'tools')
        if os.path.exists(sdk_tools):
            tooldirs.append(sdk_tools)
            tooldirs.append(os.path.join(sdk_tools, 'bin'))
        sdk_platform_tools = os.path.join(config['sdk_path'], 'platform-tools')
        if os.path.exists(sdk_platform_tools):
            tooldirs.append(sdk_platform_tools)
    tooldirs.append('/usr/bin')
    for d in tooldirs:
        path = os.path.join(d, cmd)
        if os.path.isfile(path):
            if cmd == 'aapt':
                test_aapt_version(path)
            return path
    # did not find the command, exit with error message
    ensure_build_tools_exists(config)


def test_aapt_version(aapt):
    '''Check whether the version of aapt is new enough'''
    output = subprocess.check_output([aapt, 'version'], universal_newlines=True)
    if output is None or output == '':
        logging.error(_("'{path}' failed to execute!").format(path=aapt))
    else:
        m = re.match(r'.*v([0-9]+)\.([0-9]+)[.-]?([0-9.-]*)', output)
        if m:
            major = m.group(1)
            minor = m.group(2)
            bugfix = m.group(3)
            # the Debian package has the version string like "v0.2-23.0.2"
            too_old = False
            if '.' in bugfix:
                if LooseVersion(bugfix) < LooseVersion(MINIMUM_AAPT_VERSION):
                    too_old = True
            elif LooseVersion('.'.join((major, minor, bugfix))) < LooseVersion('0.2.4062713'):
                too_old = True
            if too_old:
                logging.warning(_("'{aapt}' is too old, fdroid requires build-tools-{version} or newer!")
                                .format(aapt=aapt, version=MINIMUM_AAPT_VERSION))
        else:
            logging.warning(_('Unknown version of aapt, might cause problems: ') + output)


def test_sdk_exists(thisconfig):
    if 'sdk_path' not in thisconfig:
        if 'aapt' in thisconfig and os.path.isfile(thisconfig['aapt']):
            test_aapt_version(thisconfig['aapt'])
            return True
        else:
            logging.error(_("'sdk_path' not set in 'config.py'!"))
            return False
    if thisconfig['sdk_path'] == default_config['sdk_path']:
        logging.error(_('No Android SDK found!'))
        logging.error(_('You can use ANDROID_HOME to set the path to your SDK, i.e.:'))
        logging.error('\texport ANDROID_HOME=/opt/android-sdk')
        return False
    if not os.path.exists(thisconfig['sdk_path']):
        logging.critical(_("Android SDK path '{path}' does not exist!")
                         .format(path=thisconfig['sdk_path']))
        return False
    if not os.path.isdir(thisconfig['sdk_path']):
        logging.critical(_("Android SDK path '{path}' is not a directory!")
                         .format(path=thisconfig['sdk_path']))
        return False
    return True


def ensure_build_tools_exists(thisconfig):
    if not test_sdk_exists(thisconfig):
        raise FDroidException(_("Android SDK not found!"))
    build_tools = os.path.join(thisconfig['sdk_path'], 'build-tools')
    versioned_build_tools = os.path.join(build_tools, thisconfig['build_tools'])
    if not os.path.isdir(versioned_build_tools):
        raise FDroidException(
            _("Android build-tools path '{path}' does not exist!")
            .format(path=versioned_build_tools))


def get_local_metadata_files():
    '''get any metadata files local to an app's source repo

    This tries to ignore anything that does not count as app metdata,
    including emacs cruft ending in ~ and the .fdroid.key*pass.txt files.

    '''
    return glob.glob('.fdroid.[a-jl-z]*[a-rt-z]')


def read_pkg_args(appid_versionCode_pairs, allow_vercodes=False):
    """
    :param appids: arguments in the form of multiple appid:[vc] strings
    :returns: a dictionary with the set of vercodes specified for each package
    """
    vercodes = {}
    if not appid_versionCode_pairs:
        return vercodes

    for p in appid_versionCode_pairs:
        if allow_vercodes and ':' in p:
            package, vercode = p.split(':')
            try:
                i_vercode = int(vercode, 0)
            except ValueError:
                i_vercode = int(vercode)
            vercode = str(i_vercode)
        else:
            package, vercode = p, None
        if package not in vercodes:
            vercodes[package] = [vercode] if vercode else []
            continue
        elif vercode and vercode not in vercodes[package]:
            vercodes[package] += [vercode] if vercode else []

    return vercodes


def read_app_args(appid_versionCode_pairs, allapps, allow_vercodes=False):
    """Build a list of App instances for processing

    On top of what read_pkg_args does, this returns the whole app
    metadata, but limiting the builds list to the builds matching the
    appid_versionCode_pairs and vercodes specified.  If no appid_versionCode_pairs are specified, then
    all App and Build instances are returned.

    """

    vercodes = read_pkg_args(appid_versionCode_pairs, allow_vercodes)

    if not vercodes:
        return allapps

    apps = {}
    for appid, app in allapps.items():
        if appid in vercodes:
            apps[appid] = app

    if len(apps) != len(vercodes):
        for p in vercodes:
            if p not in allapps:
                logging.critical(_("No such package: %s") % p)
        raise FDroidException(_("Found invalid appids in arguments"))
    if not apps:
        raise FDroidException(_("No packages specified"))

    error = False
    for appid, app in apps.items():
        vc = vercodes[appid]
        if not vc:
            continue
        app.builds = [b for b in app.builds if b.versionCode in vc]
        if len(app.builds) != len(vercodes[appid]):
            error = True
            allvcs = [b.versionCode for b in app.builds]
            for v in vercodes[appid]:
                if v not in allvcs:
                    logging.critical(_("No such versionCode {versionCode} for app {appid}")
                                     .format(versionCode=v, appid=appid))

    if error:
        raise FDroidException(_("Found invalid versionCodes for some apps"))

    return apps


def get_extension(filename):
    """get name and extension of filename, with extension always lower case"""
    base, ext = os.path.splitext(filename)
    if not ext:
        return base, ''
    return base, ext.lower()[1:]


publish_name_regex = re.compile(r"^(.+)_([0-9]+)\.(apk|zip)$")


def clean_description(description):
    'Remove unneeded newlines and spaces from a block of description text'
    returnstring = ''
    # this is split up by paragraph to make removing the newlines easier
    for paragraph in re.split(r'\n\n', description):
        paragraph = re.sub('\r', '', paragraph)
        paragraph = re.sub('\n', ' ', paragraph)
        paragraph = re.sub(' {2,}', ' ', paragraph)
        paragraph = re.sub(r'^\s*(\w)', r'\1', paragraph)
        returnstring += paragraph + '\n\n'
    return returnstring.rstrip('\n')


def publishednameinfo(filename):
    filename = os.path.basename(filename)
    m = publish_name_regex.match(filename)
    try:
        result = (m.group(1), m.group(2))
    except AttributeError:
        raise FDroidException(_("Invalid name for published file: %s") % filename)
    return result


apk_release_filename = re.compile(r'(?P<appid>[a-zA-Z0-9_\.]+)_(?P<vercode>[0-9]+)\.apk')
apk_release_filename_with_sigfp = re.compile(r'(?P<appid>[a-zA-Z0-9_\.]+)_(?P<vercode>[0-9]+)_(?P<sigfp>[0-9a-f]{7})\.apk')


def apk_parse_release_filename(apkname):
    """Parses the name of an APK file according the F-Droids APK naming
    scheme and returns the tokens.

    WARNING: Returned values don't necessarily represent the APKs actual
    properties, the are just paresed from the file name.

    :returns: A triplet containing (appid, versionCode, signer), where appid
        should be the package name, versionCode should be the integer
        represion of the APKs version and signer should be the first 7 hex
        digists of the sha256 signing key fingerprint which was used to sign
        this APK.
    """
    m = apk_release_filename_with_sigfp.match(apkname)
    if m:
        return m.group('appid'), m.group('vercode'), m.group('sigfp')
    m = apk_release_filename.match(apkname)
    if m:
        return m.group('appid'), m.group('vercode'), None
    return None, None, None


def get_release_filename(app, build):
    if build.output:
        return "%s_%s.%s" % (app.id, build.versionCode, get_file_extension(build.output))
    else:
        return "%s_%s.apk" % (app.id, build.versionCode)


def get_toolsversion_logname(app, build):
    return "%s_%s_toolsversion.log" % (app.id, build.versionCode)


def getsrcname(app, build):
    return "%s_%s_src.tar.gz" % (app.id, build.versionCode)


def get_build_dir(app):
    '''get the dir that this app will be built in'''

    if app.RepoType == 'srclib':
        return os.path.join('build', 'srclib', app.Repo)

    return os.path.join('build', app.id)


class Encoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, set):
            return sorted(obj)
        return super().default(obj)


def setup_status_output(start_timestamp):
    """Create the common output dictionary for public status updates"""
    output = {
        'commandLine': sys.argv,
        'startTimestamp': int(time.mktime(start_timestamp) * 1000),
        'subcommand': sys.argv[0].split()[1],
    }
    if os.path.isdir('.git'):
        git_repo = git.repo.Repo(os.getcwd())
        output['fdroiddata'] = {
            'commitId': get_head_commit_id(git_repo),
            'isDirty': git_repo.is_dirty(),
        }
    fdroidserver_dir = os.path.dirname(sys.argv[0])
    if os.path.isdir(os.path.join(fdroidserver_dir, '.git')):
        git_repo = git.repo.Repo(fdroidserver_dir)
        output['fdroidserver'] = {
            'commitId': get_head_commit_id(git_repo),
            'isDirty': git_repo.is_dirty(),
        }
    write_running_status_json(output)
    return output


def write_running_status_json(output):
    write_status_json(output, pretty=True, name='running')


def write_status_json(output, pretty=False, name=None):
    """Write status out as JSON, and rsync it to the repo server"""
    status_dir = os.path.join('repo', 'status')
    if not os.path.exists(status_dir):
        os.makedirs(status_dir)
    if not name:
        output['endTimestamp'] = int(datetime.now(timezone.utc).timestamp() * 1000)
        name = sys.argv[0].split()[1]  # fdroid subcommand
    path = os.path.join(status_dir, name + '.json')
    with open(path, 'w') as fp:
        if pretty:
            json.dump(output, fp, sort_keys=True, cls=Encoder, indent=2)
        else:
            json.dump(output, fp, sort_keys=True, cls=Encoder, separators=(',', ':'))
    rsync_status_file_to_repo(path, repo_subdir='status')


def get_head_commit_id(git_repo):
    """Get git commit ID for HEAD as a str

    repo.head.commit.binsha is a bytearray stored in a str
    """
    return hexlify(bytearray(git_repo.head.commit.binsha)).decode()


def setup_vcs(app):
    '''checkout code from VCS and return instance of vcs and the build dir'''
    build_dir = get_build_dir(app)

    # Set up vcs interface and make sure we have the latest code...
    logging.debug("Getting {0} vcs interface for {1}"
                  .format(app.RepoType, app.Repo))
    if app.RepoType == 'git' and os.path.exists('.fdroid.yml'):
        remote = os.getcwd()
    else:
        remote = app.Repo
    vcs = getvcs(app.RepoType, remote, build_dir)

    return vcs, build_dir


def getvcs(vcstype, remote, local):
    if vcstype == 'git':
        return vcs_git(remote, local)
    if vcstype == 'git-svn':
        return vcs_gitsvn(remote, local)
    if vcstype == 'hg':
        return vcs_hg(remote, local)
    if vcstype == 'bzr':
        return vcs_bzr(remote, local)
    if vcstype == 'srclib':
        if local != os.path.join('build', 'srclib', remote):
            raise VCSException("Error: srclib paths are hard-coded!")
        return getsrclib(remote, os.path.join('build', 'srclib'), raw=True)
    if vcstype == 'svn':
        raise VCSException("Deprecated vcs type 'svn' - please use 'git-svn' instead")
    raise VCSException("Invalid vcs type " + vcstype)


def getsrclibvcs(name):
    if name not in fdroidserver.metadata.srclibs:
        raise VCSException("Missing srclib " + name)
    return fdroidserver.metadata.srclibs[name]['RepoType']


class vcs:

    def __init__(self, remote, local):

        # svn, git-svn and bzr may require auth
        self.username = None
        if self.repotype() in ('git-svn', 'bzr'):
            if '@' in remote:
                if self.repotype == 'git-svn':
                    raise VCSException("Authentication is not supported for git-svn")
                self.username, remote = remote.split('@')
                if ':' not in self.username:
                    raise VCSException(_("Password required with username"))
                self.username, self.password = self.username.split(':')

        self.remote = remote
        self.local = local
        self.clone_failed = False
        self.refreshed = False
        self.srclib = None

    def repotype(self):
        return None

    def clientversion(self):
        versionstr = FDroidPopen(self.clientversioncmd()).output
        return versionstr[0:versionstr.find('\n')]

    def clientversioncmd(self):
        return None

    def gotorevision(self, rev, refresh=True):
        """Take the local repository to a clean version of the given
        revision, which is specificed in the VCS's native
        format. Beforehand, the repository can be dirty, or even
        non-existent. If the repository does already exist locally, it
        will be updated from the origin, but only once in the lifetime
        of the vcs object.  None is acceptable for 'rev' if you know
        you are cloning a clean copy of the repo - otherwise it must
        specify a valid revision.
        """

        if self.clone_failed:
            raise VCSException(_("Downloading the repository already failed once, not trying again."))

        # The .fdroidvcs-id file for a repo tells us what VCS type
        # and remote that directory was created from, allowing us to drop it
        # automatically if either of those things changes.
        fdpath = os.path.join(self.local, '..',
                              '.fdroidvcs-' + os.path.basename(self.local))
        fdpath = os.path.normpath(fdpath)
        cdata = self.repotype() + ' ' + self.remote
        writeback = True
        deleterepo = False
        if os.path.exists(self.local):
            if os.path.exists(fdpath):
                with open(fdpath, 'r') as f:
                    fsdata = f.read().strip()
                if fsdata == cdata:
                    writeback = False
                else:
                    deleterepo = True
                    logging.info("Repository details for %s changed - deleting" % (
                        self.local))
            else:
                deleterepo = True
                logging.info("Repository details for %s missing - deleting" % (
                    self.local))
        if deleterepo:
            shutil.rmtree(self.local)

        exc = None
        if not refresh:
            self.refreshed = True

        try:
            self.gotorevisionx(rev)
        except FDroidException as e:
            exc = e

        # If necessary, write the .fdroidvcs file.
        if writeback and not self.clone_failed:
            os.makedirs(os.path.dirname(fdpath), exist_ok=True)
            with open(fdpath, 'w+') as f:
                f.write(cdata)

        if exc is not None:
            raise exc

    def gotorevisionx(self, rev):  # pylint: disable=unused-argument
        """Derived classes need to implement this.

        It's called once basic checking has been performed.
        """
        raise VCSException("This VCS type doesn't define gotorevisionx")

    # Initialise and update submodules
    def initsubmodules(self):
        raise VCSException('Submodules not supported for this vcs type')

    # Get a list of all known tags
    def gettags(self):
        if not self._gettags:
            raise VCSException('gettags not supported for this vcs type')
        rtags = []
        for tag in self._gettags():
            if re.match('[-A-Za-z0-9_. /]+$', tag):
                rtags.append(tag)
        return rtags

    def latesttags(self):
        """Get a list of all the known tags, sorted from newest to oldest"""
        raise VCSException('latesttags not supported for this vcs type')

    def getref(self):
        """Get current commit reference (hash, revision, etc)"""
        raise VCSException('getref not supported for this vcs type')

    def getsrclib(self):
        """Returns the srclib (name, path) used in setting up the current revision, or None."""
        return self.srclib


class vcs_git(vcs):

    def repotype(self):
        return 'git'

    def clientversioncmd(self):
        return ['git', '--version']

    def git(self, args, envs=dict(), cwd=None, output=True):
        '''Prevent git fetch/clone/submodule from hanging at the username/password prompt

        While fetch/pull/clone respect the command line option flags,
        it seems that submodule commands do not.  They do seem to
        follow whatever is in env vars, if the version of git is new
        enough.  So we just throw the kitchen sink at it to see what
        sticks.

        Also, because of CVE-2017-1000117, block all SSH URLs.
        '''
        #
        # supported in git >= 2.3
        git_config = [
            '-c', 'core.askpass=/bin/true',
            '-c', 'core.sshCommand=/bin/false',
            '-c', 'url.https://.insteadOf=ssh://',
        ]
        for domain in ('bitbucket.org', 'github.com', 'gitlab.com'):
            git_config.append('-c')
            git_config.append('url.https://u:p@' + domain + '/.insteadOf=git@' + domain + ':')
            git_config.append('-c')
            git_config.append('url.https://u:p@' + domain + '.insteadOf=git://' + domain)
            git_config.append('-c')
            git_config.append('url.https://u:p@' + domain + '.insteadOf=https://' + domain)
        envs.update({
            'GIT_TERMINAL_PROMPT': '0',
            'GIT_ASKPASS': '/bin/true',
            'SSH_ASKPASS': '/bin/true',
            'GIT_SSH': '/bin/false',  # for git < 2.3
        })
        return FDroidPopen(['git', ] + git_config + args,
                           envs=envs, cwd=cwd, output=output)

    def checkrepo(self):
        """If the local directory exists, but is somehow not a git repository,
        git will traverse up the directory tree until it finds one
        that is (i.e.  fdroidserver) and then we'll proceed to destroy
        it!  This is called as a safety check.

        """

        p = FDroidPopen(['git', 'rev-parse', '--show-toplevel'], cwd=self.local, output=False)
        result = p.output.rstrip()
        if not result.endswith(self.local):
            raise VCSException('Repository mismatch')

    def gotorevisionx(self, rev):
        if not os.path.exists(self.local):
            # Brand new checkout
            p = self.git(['clone', '--', self.remote, self.local])
            if p.returncode != 0:
                self.clone_failed = True
                raise VCSException("Git clone failed", p.output)
            self.checkrepo()
        else:
            self.checkrepo()
            # Discard any working tree changes
            p = FDroidPopen(['git', 'submodule', 'foreach', '--recursive',
                             'git', 'reset', '--hard'], cwd=self.local, output=False)
            if p.returncode != 0:
                raise VCSException(_("Git reset failed"), p.output)
            # Remove untracked files now, in case they're tracked in the target
            # revision (it happens!)
            p = FDroidPopen(['git', 'submodule', 'foreach', '--recursive',
                             'git', 'clean', '-dffx'], cwd=self.local, output=False)
            if p.returncode != 0:
                raise VCSException(_("Git clean failed"), p.output)
            if not self.refreshed:
                # Get latest commits and tags from remote
                p = self.git(['fetch', 'origin'], cwd=self.local)
                if p.returncode != 0:
                    raise VCSException(_("Git fetch failed"), p.output)
                p = self.git(['fetch', '--prune', '--tags', '--force', 'origin'], output=False, cwd=self.local)
                if p.returncode != 0:
                    raise VCSException(_("Git fetch failed"), p.output)
                # Recreate origin/HEAD as git clone would do it, in case it disappeared
                p = FDroidPopen(['git', 'remote', 'set-head', 'origin', '--auto'], cwd=self.local, output=False)
                if p.returncode != 0:
                    lines = p.output.splitlines()
                    if 'Multiple remote HEAD branches' not in lines[0]:
                        raise VCSException(_("Git remote set-head failed"), p.output)
                    branch = lines[1].split(' ')[-1]
                    p2 = FDroidPopen(['git', 'remote', 'set-head', 'origin', '--', branch],
                                     cwd=self.local, output=False)
                    if p2.returncode != 0:
                        raise VCSException(_("Git remote set-head failed"), p.output + '\n' + p2.output)
                self.refreshed = True
        # origin/HEAD is the HEAD of the remote, e.g. the "default branch" on
        # a github repo. Most of the time this is the same as origin/master.
        rev = rev or 'origin/HEAD'
        p = FDroidPopen(['git', 'checkout', '-f', rev], cwd=self.local, output=False)
        if p.returncode != 0:
            raise VCSException(_("Git checkout of '%s' failed") % rev, p.output)
        # Get rid of any uncontrolled files left behind
        p = FDroidPopen(['git', 'clean', '-dffx'], cwd=self.local, output=False)
        if p.returncode != 0:
            raise VCSException(_("Git clean failed"), p.output)

    def initsubmodules(self):
        self.checkrepo()
        submfile = os.path.join(self.local, '.gitmodules')
        if not os.path.isfile(submfile):
            raise NoSubmodulesException(_("No git submodules available"))

        # fix submodules not accessible without an account and public key auth
        with open(submfile, 'r') as f:
            lines = f.readlines()
        with open(submfile, 'w') as f:
            for line in lines:
                for domain in ('bitbucket.org', 'github.com', 'gitlab.com'):
                    line = re.sub('git@' + domain + ':', 'https://u:p@' + domain + '/', line)
                f.write(line)

        p = FDroidPopen(['git', 'submodule', 'sync'], cwd=self.local, output=False)
        if p.returncode != 0:
            raise VCSException(_("Git submodule sync failed"), p.output)
        p = self.git(['submodule', 'update', '--init', '--force', '--recursive'], cwd=self.local)
        if p.returncode != 0:
            raise VCSException(_("Git submodule update failed"), p.output)

    def _gettags(self):
        self.checkrepo()
        p = FDroidPopen(['git', 'tag'], cwd=self.local, output=False)
        return p.output.splitlines()

    tag_format = re.compile(r'tag: ([^),]*)')

    def latesttags(self):
        self.checkrepo()
        p = FDroidPopen(['git', 'log', '--tags',
                         '--simplify-by-decoration', '--pretty=format:%d'],
                        cwd=self.local, output=False)
        tags = []
        for line in p.output.splitlines():
            for tag in self.tag_format.findall(line):
                tags.append(tag)
        return tags


class vcs_gitsvn(vcs):

    def repotype(self):
        return 'git-svn'

    def clientversioncmd(self):
        return ['git', 'svn', '--version']

    def checkrepo(self):
        """If the local directory exists, but is somehow not a git repository,
        git will traverse up the directory tree until it finds one that
        is (i.e.  fdroidserver) and then we'll proceed to destory it!
        This is called as a safety check.

        """
        p = FDroidPopen(['git', 'rev-parse', '--show-toplevel'], cwd=self.local, output=False)
        result = p.output.rstrip()
        if not result.endswith(self.local):
            raise VCSException('Repository mismatch')

    def git(self, args, envs=dict(), cwd=None, output=True):
        '''Prevent git fetch/clone/submodule from hanging at the username/password prompt

        AskPass is set to /bin/true to let the process try to connect
        without a username/password.

        The SSH command is set to /bin/false to block all SSH URLs
        (supported in git >= 2.3).  This protects against
        CVE-2017-1000117.

        '''
        git_config = [
            '-c', 'core.askpass=/bin/true',
            '-c', 'core.sshCommand=/bin/false',
        ]
        envs.update({
            'GIT_TERMINAL_PROMPT': '0',
            'GIT_ASKPASS': '/bin/true',
            'SSH_ASKPASS': '/bin/true',
            'GIT_SSH': '/bin/false',  # for git < 2.3
            'SVN_SSH': '/bin/false',
        })
        return FDroidPopen(['git', ] + git_config + args,
                           envs=envs, cwd=cwd, output=output)

    def gotorevisionx(self, rev):
        if not os.path.exists(self.local):
            # Brand new checkout
            gitsvn_args = ['svn', 'clone']
            remote = None
            if ';' in self.remote:
                remote_split = self.remote.split(';')
                for i in remote_split[1:]:
                    if i.startswith('trunk='):
                        gitsvn_args.extend(['-T', i[6:]])
                    elif i.startswith('tags='):
                        gitsvn_args.extend(['-t', i[5:]])
                    elif i.startswith('branches='):
                        gitsvn_args.extend(['-b', i[9:]])
                remote = remote_split[0]
            else:
                remote = self.remote

            if not remote.startswith('https://'):
                raise VCSException(_('HTTPS must be used with Subversion URLs!'))

            # git-svn sucks at certificate validation, this throws useful errors:
            try:
                import requests
                r = requests.head(remote)
                r.raise_for_status()
            except Exception as e:
                raise VCSException('SVN certificate pre-validation failed: ' + str(e))
            location = r.headers.get('location')
            if location and not location.startswith('https://'):
                raise VCSException(_('Invalid redirect to non-HTTPS: {before} -> {after} ')
                                   .format(before=remote, after=location))

            gitsvn_args.extend(['--', remote, self.local])
            p = self.git(gitsvn_args)
            if p.returncode != 0:
                self.clone_failed = True
                raise VCSException(_('git svn clone failed'), p.output)
            self.checkrepo()
        else:
            self.checkrepo()
            # Discard any working tree changes
            p = self.git(['reset', '--hard'], cwd=self.local, output=False)
            if p.returncode != 0:
                raise VCSException("Git reset failed", p.output)
            # Remove untracked files now, in case they're tracked in the target
            # revision (it happens!)
            p = self.git(['clean', '-dffx'], cwd=self.local, output=False)
            if p.returncode != 0:
                raise VCSException("Git clean failed", p.output)
            if not self.refreshed:
                # Get new commits, branches and tags from repo
                p = self.git(['svn', 'fetch'], cwd=self.local, output=False)
                if p.returncode != 0:
                    raise VCSException("Git svn fetch failed")
                p = self.git(['svn', 'rebase'], cwd=self.local, output=False)
                if p.returncode != 0:
                    raise VCSException("Git svn rebase failed", p.output)
                self.refreshed = True

        rev = rev or 'master'
        if rev:
            nospaces_rev = rev.replace(' ', '%20')
            # Try finding a svn tag
            for treeish in ['origin/', '']:
                p = self.git(['checkout', treeish + 'tags/' + nospaces_rev], cwd=self.local, output=False)
                if p.returncode == 0:
                    break
            if p.returncode != 0:
                # No tag found, normal svn rev translation
                # Translate svn rev into git format
                rev_split = rev.split('/')

                p = None
                for treeish in ['origin/', '']:
                    if len(rev_split) > 1:
                        treeish += rev_split[0]
                        svn_rev = rev_split[1]

                    else:
                        # if no branch is specified, then assume trunk (i.e. 'master' branch):
                        treeish += 'master'
                        svn_rev = rev

                    svn_rev = svn_rev if svn_rev[0] == 'r' else 'r' + svn_rev

                    p = self.git(['svn', 'find-rev', '--before', svn_rev, treeish], cwd=self.local, output=False)
                    git_rev = p.output.rstrip()

                    if p.returncode == 0 and git_rev:
                        break

                if p.returncode != 0 or not git_rev:
                    # Try a plain git checkout as a last resort
                    p = self.git(['checkout', rev], cwd=self.local, output=False)
                    if p.returncode != 0:
                        raise VCSException("No git treeish found and direct git checkout of '%s' failed" % rev, p.output)
                else:
                    # Check out the git rev equivalent to the svn rev
                    p = self.git(['checkout', git_rev], cwd=self.local, output=False)
                    if p.returncode != 0:
                        raise VCSException(_("Git checkout of '%s' failed") % rev, p.output)

        # Get rid of any uncontrolled files left behind
        p = self.git(['clean', '-dffx'], cwd=self.local, output=False)
        if p.returncode != 0:
            raise VCSException(_("Git clean failed"), p.output)

    def _gettags(self):
        self.checkrepo()
        for treeish in ['origin/', '']:
            d = os.path.join(self.local, '.git', 'svn', 'refs', 'remotes', treeish, 'tags')
            if os.path.isdir(d):
                return os.listdir(d)

    def getref(self):
        self.checkrepo()
        p = FDroidPopen(['git', 'svn', 'find-rev', 'HEAD'], cwd=self.local, output=False)
        if p.returncode != 0:
            return None
        return p.output.strip()


class vcs_hg(vcs):

    def repotype(self):
        return 'hg'

    def clientversioncmd(self):
        return ['hg', '--version']

    def gotorevisionx(self, rev):
        if not os.path.exists(self.local):
            p = FDroidPopen(['hg', 'clone', '--ssh', '/bin/false', '--', self.remote, self.local],
                            output=False)
            if p.returncode != 0:
                self.clone_failed = True
                raise VCSException("Hg clone failed", p.output)
        else:
            p = FDroidPopen(['hg', 'status', '-uS'], cwd=self.local, output=False)
            if p.returncode != 0:
                raise VCSException("Hg status failed", p.output)
            for line in p.output.splitlines():
                if not line.startswith('? '):
                    raise VCSException("Unexpected output from hg status -uS: " + line)
                FDroidPopen(['rm', '-rf', '--', line[2:]], cwd=self.local, output=False)
            if not self.refreshed:
                p = FDroidPopen(['hg', 'pull', '--ssh', '/bin/false'], cwd=self.local, output=False)
                if p.returncode != 0:
                    raise VCSException("Hg pull failed", p.output)
                self.refreshed = True

        rev = rev or 'default'
        if not rev:
            return
        p = FDroidPopen(['hg', 'update', '-C', '--', rev], cwd=self.local, output=False)
        if p.returncode != 0:
            raise VCSException("Hg checkout of '%s' failed" % rev, p.output)
        p = FDroidPopen(['hg', 'purge', '--all'], cwd=self.local, output=False)
        # Also delete untracked files, we have to enable purge extension for that:
        if "'purge' is provided by the following extension" in p.output:
            with open(os.path.join(self.local, '.hg', 'hgrc'), "a") as myfile:
                myfile.write("\n[extensions]\nhgext.purge=\n")
            p = FDroidPopen(['hg', 'purge', '--all'], cwd=self.local, output=False)
            if p.returncode != 0:
                raise VCSException("HG purge failed", p.output)
        elif p.returncode != 0:
            raise VCSException("HG purge failed", p.output)

    def _gettags(self):
        p = FDroidPopen(['hg', 'tags', '-q'], cwd=self.local, output=False)
        return p.output.splitlines()[1:]


class vcs_bzr(vcs):

    def repotype(self):
        return 'bzr'

    def clientversioncmd(self):
        return ['bzr', '--version']

    def bzr(self, args, envs=dict(), cwd=None, output=True):
        '''Prevent bzr from ever using SSH to avoid security vulns'''
        envs.update({
            'BZR_SSH': 'false',
        })
        return FDroidPopen(['bzr', ] + args, envs=envs, cwd=cwd, output=output)

    def gotorevisionx(self, rev):
        if not os.path.exists(self.local):
            p = self.bzr(['branch', self.remote, self.local], output=False)
            if p.returncode != 0:
                self.clone_failed = True
                raise VCSException("Bzr branch failed", p.output)
        else:
            p = self.bzr(['clean-tree', '--force', '--unknown', '--ignored'], cwd=self.local, output=False)
            if p.returncode != 0:
                raise VCSException("Bzr revert failed", p.output)
            if not self.refreshed:
                p = self.bzr(['pull'], cwd=self.local, output=False)
                if p.returncode != 0:
                    raise VCSException("Bzr update failed", p.output)
                self.refreshed = True

        revargs = list(['-r', rev] if rev else [])
        p = self.bzr(['revert'] + revargs, cwd=self.local, output=False)
        if p.returncode != 0:
            raise VCSException("Bzr revert of '%s' failed" % rev, p.output)

    def _gettags(self):
        p = self.bzr(['tags'], cwd=self.local, output=False)
        return [tag.split('   ')[0].strip() for tag in
                p.output.splitlines()]


def unescape_string(string):
    if len(string) < 2:
        return string
    if string[0] == '"' and string[-1] == '"':
        return string[1:-1]

    return string.replace("\\'", "'")


def retrieve_string(app_dir, string, xmlfiles=None):

    if not string.startswith('@string/'):
        return unescape_string(string)

    if xmlfiles is None:
        xmlfiles = []
        for res_dir in [
            os.path.join(app_dir, 'res'),
            os.path.join(app_dir, 'src', 'main', 'res'),
        ]:
            for root, dirs, files in os.walk(res_dir):
                if os.path.basename(root) == 'values':
                    xmlfiles += [os.path.join(root, x) for x in files if x.endswith('.xml')]

    name = string[len('@string/'):]

    def element_content(element):
        if element.text is None:
            return ""
        s = XMLElementTree.tostring(element, encoding='utf-8', method='text')
        return s.decode('utf-8').strip()

    for path in xmlfiles:
        if not os.path.isfile(path):
            continue
        xml = parse_xml(path)
        element = xml.find('string[@name="' + name + '"]')
        if element is not None:
            content = element_content(element)
            return retrieve_string(app_dir, content, xmlfiles)

    return ''


def retrieve_string_singleline(app_dir, string, xmlfiles=None):
    return retrieve_string(app_dir, string, xmlfiles).replace('\n', ' ').strip()


def manifest_paths(app_dir, flavours):
    '''Return list of existing files that will be used to find the highest vercode'''

    possible_manifests = \
        [os.path.join(app_dir, 'AndroidManifest.xml'),
         os.path.join(app_dir, 'src', 'main', 'AndroidManifest.xml'),
         os.path.join(app_dir, 'src', 'AndroidManifest.xml'),
         os.path.join(app_dir, 'build.gradle'),
         os.path.join(app_dir, 'build-extras.gradle'),
         os.path.join(app_dir, 'build.gradle.kts')]

    for flavour in flavours:
        if flavour == 'yes':
            continue
        possible_manifests.append(
            os.path.join(app_dir, 'src', flavour, 'AndroidManifest.xml'))

    return [path for path in possible_manifests if os.path.isfile(path)]


def fetch_real_name(app_dir, flavours):
    '''Retrieve the package name. Returns the name, or None if not found.'''
    for path in manifest_paths(app_dir, flavours):
        if not path.endswith('.xml') or not os.path.isfile(path):
            continue
        logging.debug("fetch_real_name: Checking manifest at " + path)
        xml = parse_xml(path)
        app = xml.find('application')
        if app is None:
            continue
        if XMLNS_ANDROID + "label" not in app.attrib:
            continue
        label = app.attrib[XMLNS_ANDROID + "label"]
        result = retrieve_string_singleline(app_dir, label)
        if result:
            result = result.strip()
        return result
    return None


def get_library_references(root_dir):
    libraries = []
    proppath = os.path.join(root_dir, 'project.properties')
    if not os.path.isfile(proppath):
        return libraries
    with open(proppath, 'r', encoding='iso-8859-1') as f:
        for line in f:
            if not line.startswith('android.library.reference.'):
                continue
            path = line.split('=')[1].strip()
            relpath = os.path.join(root_dir, path)
            if not os.path.isdir(relpath):
                continue
            logging.debug("Found subproject at %s" % path)
            libraries.append(path)
    return libraries


def ant_subprojects(root_dir):
    subprojects = get_library_references(root_dir)
    for subpath in subprojects:
        subrelpath = os.path.join(root_dir, subpath)
        for p in get_library_references(subrelpath):
            relp = os.path.normpath(os.path.join(subpath, p))
            if relp not in subprojects:
                subprojects.insert(0, relp)
    return subprojects


def remove_debuggable_flags(root_dir):
    # Remove forced debuggable flags
    logging.debug("Removing debuggable flags from %s" % root_dir)
    for root, dirs, files in os.walk(root_dir):
        if 'AndroidManifest.xml' in files and os.path.isfile(os.path.join(root, 'AndroidManifest.xml')):
            regsub_file(r'android:debuggable="[^"]*"',
                        '',
                        os.path.join(root, 'AndroidManifest.xml'))


vcsearch_g = re.compile(r'''\b[Vv]ersionCode\s*=?\s*["']*([0-9]+)["']*''').search
vnsearch_g = re.compile(r'''\b[Vv]ersionName\s*=?\s*(["'])((?:(?=(\\?))\3.)*?)\1''').search
vnssearch_g = re.compile(r'''\b[Vv]ersionNameSuffix\s*=?\s*(["'])((?:(?=(\\?))\3.)*?)\1''').search
psearch_g = re.compile(r'''\b(packageName|applicationId)\s*=*\s*["']([^"']+)["']''').search
fsearch_g = re.compile(r'''\b(applicationIdSuffix)\s*=*\s*["']([^"']+)["']''').search


def app_matches_packagename(app, package):
    if not package:
        return False
    appid = app.UpdateCheckName or app.id
    if appid is None or appid == "Ignore":
        return True
    return appid == package


def parse_androidmanifests(paths, app):
    """
    Extract some information from the AndroidManifest.xml at the given path.
    Returns (version, vercode, package), any or all of which might be None.
    All values returned are strings.
    """

    ignoreversions = app.UpdateCheckIgnore
    ignoresearch = re.compile(ignoreversions).search if ignoreversions else None

    if not paths:
        return (None, None, None)

    max_version = None
    max_vercode = None
    max_package = None

    for path in paths:

        if not os.path.isfile(path):
            continue

        logging.debug(_("Parsing manifest at '{path}'").format(path=path))
        version = None
        vercode = None
        package = None

        flavour = None
        temp_app_id = None
        temp_version_name = None
        if app.builds and 'gradle' in app.builds[-1] and app.builds[-1].gradle:
            flavour = app.builds[-1].gradle[-1]

        if path.endswith('.gradle') or path.endswith('.gradle.kts'):
            with open(path, 'r') as f:
                android_plugin_file = False
                inside_flavour_group = 0
                inside_required_flavour = 0
                for line in f:
                    if gradle_comment.match(line):
                        continue

                    if "applicationId" in line and not temp_app_id:
                        matches = psearch_g(line)
                        if matches:
                            temp_app_id = matches.group(2)

                    if "versionName" in line and not temp_version_name:
                        matches = vnsearch_g(line)
                        if matches:
                            temp_version_name = matches.group(2)

                    if inside_flavour_group > 0:
                        if inside_required_flavour > 0:
                            matches = psearch_g(line)
                            if matches:
                                s = matches.group(2)
                                if app_matches_packagename(app, s):
                                    package = s
                            else:
                                # If build.gradle contains applicationIdSuffix add it to the end of package name
                                matches = fsearch_g(line)
                                if matches and temp_app_id:
                                    suffix = matches.group(2)
                                    temp_app_id = temp_app_id + suffix
                                    if app_matches_packagename(app, temp_app_id):
                                        package = temp_app_id

                            matches = vnsearch_g(line)
                            if matches:
                                version = matches.group(2)
                            else:
                                # If build.gradle contains applicationNameSuffix add it to the end of version name
                                matches = vnssearch_g(line)
                                if matches and temp_version_name:
                                    name_suffix = matches.group(2)
                                    version = temp_version_name + name_suffix

                            matches = vcsearch_g(line)
                            if matches:
                                vercode = matches.group(1)

                            if '{' in line:
                                inside_required_flavour += 1
                            if '}' in line:
                                inside_required_flavour -= 1
                        else:
                            if flavour and (flavour in line):
                                inside_required_flavour = 1

                        if '{' in line:
                            inside_flavour_group += 1
                        if '}' in line:
                            inside_flavour_group -= 1
                    else:
                        if "productFlavors" in line:
                            inside_flavour_group = 1
                        if not package:
                            matches = psearch_g(line)
                            if matches:
                                s = matches.group(2)
                                if app_matches_packagename(app, s):
                                    package = s
                        if not version:
                            matches = vnsearch_g(line)
                            if matches:
                                version = matches.group(2)
                        if not vercode:
                            matches = vcsearch_g(line)
                            if matches:
                                vercode = matches.group(1)
                    if not android_plugin_file and ANDROID_PLUGIN_REGEX.match(line):
                        android_plugin_file = True
            if android_plugin_file:
                if package:
                    max_package = package
                if version:
                    max_version = version
                if vercode:
                    max_vercode = vercode
                if max_package and max_version and max_vercode:
                    break
        else:
            try:
                xml = parse_xml(path)
                if "package" in xml.attrib:
                    s = xml.attrib["package"]
                    if app_matches_packagename(app, s):
                        package = s
                if XMLNS_ANDROID + "versionName" in xml.attrib:
                    version = xml.attrib[XMLNS_ANDROID + "versionName"]
                    base_dir = os.path.dirname(path)
                    version = retrieve_string_singleline(base_dir, version)
                if XMLNS_ANDROID + "versionCode" in xml.attrib:
                    a = xml.attrib[XMLNS_ANDROID + "versionCode"]
                    if string_is_integer(a):
                        vercode = a
            except Exception:
                logging.warning(_("Problem with xml at '{path}'").format(path=path))

        # Remember package name, may be defined separately from version+vercode
        if package is None:
            package = max_package

        logging.debug("..got package={0}, version={1}, vercode={2}"
                      .format(package, version, vercode))

        # Always grab the package name and version name in case they are not
        # together with the highest version code
        if max_package is None and package is not None:
            max_package = package
        if max_version is None and version is not None:
            max_version = version

        if vercode is not None \
           and (max_vercode is None or vercode > max_vercode):
            if version and (not ignoresearch or not ignoresearch(version)):
                if version is not None:
                    max_version = version
                if vercode is not None:
                    max_vercode = vercode
                if package is not None:
                    max_package = package
            else:
                max_version = "Ignore"

    if max_version is None:
        max_version = "Unknown"

    if max_package:
        msg = _("Invalid package name {0}").format(max_package)
        if not is_valid_package_name(max_package):
            raise FDroidException(msg)
        elif not is_strict_application_id(max_package):
            logging.warning(msg)

    return (max_version, max_vercode, max_package)


def is_valid_package_name(name):
    """Check whether name is a valid fdroid package name

    APKs and manually defined package names must use a valid Java
    Package Name.  Automatically generated package names for non-APK
    files use the SHA-256 sum.

    """
    return VALID_APPLICATION_ID_REGEX.match(name) is not None \
        or FDROID_PACKAGE_NAME_REGEX.match(name) is not None


def is_strict_application_id(name):
    """Check whether name is a valid Android Application ID

    The Android ApplicationID is basically a Java Package Name, but
    with more restrictive naming rules:

    * It must have at least two segments (one or more dots).
    * Each segment must start with a letter.
    * All characters must be alphanumeric or an underscore [a-zA-Z0-9_].

    https://developer.android.com/studio/build/application-id

    """
    return STRICT_APPLICATION_ID_REGEX.match(name) is not None \
        and '.' in name


def get_all_gradle_and_manifests(build_dir):
    paths = []
    for root, dirs, files in os.walk(build_dir):
        for f in sorted(files):
            if f == 'AndroidManifest.xml' \
               or f.endswith('.gradle') or f.endswith('.gradle.kts'):
                full = os.path.join(root, f)
                paths.append(full)
    return paths


def get_gradle_subdir(build_dir, paths):
    """get the subdir where the gradle build is based"""
    first_gradle_dir = None
    for path in paths:
        if not first_gradle_dir:
            first_gradle_dir = os.path.relpath(os.path.dirname(path), build_dir)
        if os.path.exists(path) and SETTINGS_GRADLE_REGEX.match(os.path.basename(path)):
            with open(path) as fp:
                for m in GRADLE_SUBPROJECT_REGEX.finditer(fp.read()):
                    for f in glob.glob(os.path.join(os.path.dirname(path), m.group(1), 'build.gradle*')):
                        with open(f) as fp:
                            while True:
                                line = fp.readline()
                                if not line:
                                    break
                                if ANDROID_PLUGIN_REGEX.match(line):
                                    return os.path.relpath(os.path.dirname(f), build_dir)
    if first_gradle_dir and first_gradle_dir != '.':
        return first_gradle_dir

    return ''


def getrepofrompage(url):
    """Get the repo type and address from the given web page.

    The page is scanned in a rather naive manner for 'git clone xxxx',
    'hg clone xxxx', etc, and when one of these is found it's assumed
    that's the information we want.  Returns repotype, address, or
    None, reason

    """
    if not url.startswith('http'):
        return (None, _('{url} does not start with "http"!'.format(url=url)))
    req = urllib.request.urlopen(url)  # nosec B310 non-http URLs are filtered out
    if req.getcode() != 200:
        return (None, 'Unable to get ' + url + ' - return code ' + str(req.getcode()))
    page = req.read().decode(req.headers.get_content_charset())

    # Works for BitBucket
    m = re.search('data-fetch-url="(.*)"', page)
    if m is not None:
        repo = m.group(1)

        if repo.endswith('.git'):
            return ('git', repo)

        return ('hg', repo)

    # Works for BitBucket (obsolete)
    index = page.find('hg clone')
    if index != -1:
        repotype = 'hg'
        repo = page[index + 9:]
        index = repo.find('<')
        if index == -1:
            return (None, _("Error while getting repo address"))
        repo = repo[:index]
        repo = repo.split('"')[0]
        return (repotype, repo)

    # Works for BitBucket (obsolete)
    index = page.find('git clone')
    if index != -1:
        repotype = 'git'
        repo = page[index + 10:]
        index = repo.find('<')
        if index == -1:
            return (None, _("Error while getting repo address"))
        repo = repo[:index]
        repo = repo.split('"')[0]
        return (repotype, repo)

    return (None, _("No information found.") + page)


def get_app_from_url(url):
    """Guess basic app metadata from the URL.

    The URL must include a network hostname, unless it is an lp:,
    file:, or git/ssh URL.  This throws ValueError on bad URLs to
    match urlparse().

    """

    parsed = urllib.parse.urlparse(url)
    invalid_url = False
    if not parsed.scheme or not parsed.path:
        invalid_url = True

    app = fdroidserver.metadata.App()
    app.Repo = url
    if url.startswith('git://') or url.startswith('git@'):
        app.RepoType = 'git'
    elif parsed.netloc == 'github.com':
        app.RepoType = 'git'
        app.SourceCode = url
        app.IssueTracker = url + '/issues'
    elif parsed.netloc == 'gitlab.com':
        # git can be fussy with gitlab URLs unless they end in .git
        if url.endswith('.git'):
            url = url[:-4]
        app.Repo = url + '.git'
        app.RepoType = 'git'
        app.SourceCode = url
        app.IssueTracker = url + '/issues'
    elif parsed.netloc == 'notabug.org':
        if url.endswith('.git'):
            url = url[:-4]
        app.Repo = url + '.git'
        app.RepoType = 'git'
        app.SourceCode = url
        app.IssueTracker = url + '/issues'
    elif parsed.netloc == 'bitbucket.org':
        if url.endswith('/'):
            url = url[:-1]
        app.SourceCode = url + '/src'
        app.IssueTracker = url + '/issues'
        # Figure out the repo type and adddress...
        app.RepoType, app.Repo = getrepofrompage(url)
    elif parsed.netloc == 'codeberg.org':
        app.RepoType = 'git'
        app.SourceCode = url
        app.IssueTracker = url + '/issues'
    elif url.startswith('https://') and url.endswith('.git'):
        app.RepoType = 'git'

    if not parsed.netloc and parsed.scheme in ('git', 'http', 'https', 'ssh'):
        invalid_url = True

    if invalid_url:
        raise ValueError(_('"{url}" is not a valid URL!'.format(url=url)))

    if not app.RepoType:
        raise FDroidException("Unable to determine vcs type. " + app.Repo)

    return app


def parse_srclib_spec(spec):

    if type(spec) != str:
        raise MetaDataException(_("can not parse scrlib spec "
                                  "(not a string): '{}'")
                                .format(spec))

    tokens = spec.split('@')
    if len(tokens) > 2:
        raise MetaDataException(_("could not parse srclib spec "
                                  "(too many '@' signs): '{}'")
                                .format(spec))
    elif len(tokens) < 2:
        raise MetaDataException(_("could not parse srclib spec "
                                  "(no ref specified): '{}'")
                                .format(spec))

    name = tokens[0]
    ref = tokens[1]
    number = None
    subdir = None

    if ':' in name:
        number, name = name.split(':', 1)
    if '/' in name:
        name, subdir = name.split('/', 1)

    return (name, ref, number, subdir)


def getsrclib(spec, srclib_dir, subdir=None, basepath=False,
              raw=False, prepare=True, preponly=False, refresh=True,
              build=None):
    """Get the specified source library.

    Returns the path to it. Normally this is the path to be used when
    referencing it, which may be a subdirectory of the actual project. If
    you want the base directory of the project, pass 'basepath=True'.

    """
    number = None
    subdir = None
    if raw:
        name = spec
        ref = None
    else:
        name, ref = spec.split('@')
        if ':' in name:
            number, name = name.split(':', 1)
        if '/' in name:
            name, subdir = name.split('/', 1)

    if name not in fdroidserver.metadata.srclibs:
        raise VCSException('srclib ' + name + ' not found.')

    srclib = fdroidserver.metadata.srclibs[name]

    sdir = os.path.join(srclib_dir, name)

    if not preponly:
        vcs = getvcs(srclib["RepoType"], srclib["Repo"], sdir)
        vcs.srclib = (name, number, sdir)
        if ref:
            vcs.gotorevision(ref, refresh)

        if raw:
            return vcs

    libdir = None
    if subdir:
        libdir = os.path.join(sdir, subdir)
    elif srclib["Subdir"]:
        for subdir in srclib["Subdir"]:
            libdir_candidate = os.path.join(sdir, subdir)
            if os.path.exists(libdir_candidate):
                libdir = libdir_candidate
                break

    if libdir is None:
        libdir = sdir

    remove_signing_keys(sdir)
    remove_debuggable_flags(sdir)

    if prepare:

        if srclib["Prepare"]:
            cmd = replace_config_vars(srclib["Prepare"], build)

            p = FDroidPopen(['bash', '-x', '-c', '--', cmd], cwd=libdir)
            if p.returncode != 0:
                raise BuildException("Error running prepare command for srclib %s"
                                     % name, p.output)

    if basepath:
        libdir = sdir

    return (name, number, libdir)


gradle_version_regex = re.compile(r"[^/]*'com\.android\.tools\.build:gradle:([^\.]+\.[^\.]+).*'.*")


def prepare_source(vcs, app, build, build_dir, srclib_dir, extlib_dir, onserver=False, refresh=True):
    """ Prepare the source code for a particular build

    :param vcs: the appropriate vcs object for the application
    :param app: the application details from the metadata
    :param build: the build details from the metadata
    :param build_dir: the path to the build directory, usually 'build/app.id'
    :param srclib_dir: the path to the source libraries directory, usually 'build/srclib'
    :param extlib_dir: the path to the external libraries directory, usually 'build/extlib'

    Returns the (root, srclibpaths) where:
    :param root: is the root directory, which may be the same as 'build_dir' or may
                 be a subdirectory of it.
    :param srclibpaths: is information on the srclibs being used
    """

    # Optionally, the actual app source can be in a subdirectory
    if build.subdir:
        root_dir = os.path.join(build_dir, build.subdir)
    else:
        root_dir = build_dir

    # Get a working copy of the right revision
    logging.info("Getting source for revision " + build.commit)
    vcs.gotorevision(build.commit, refresh)

    # Initialise submodules if required
    if build.submodules:
        logging.info(_("Initialising submodules"))
        vcs.initsubmodules()

    # Check that a subdir (if we're using one) exists. This has to happen
    # after the checkout, since it might not exist elsewhere
    if not os.path.exists(root_dir):
        raise BuildException('Missing subdir ' + root_dir)

    # Run an init command if one is required
    if build.init:
        cmd = replace_config_vars(build.init, build)
        logging.info("Running 'init' commands in %s" % root_dir)

        p = FDroidPopen(['bash', '-x', '-c', '--', cmd], cwd=root_dir)
        if p.returncode != 0:
            raise BuildException("Error running init command for %s:%s" %
                                 (app.id, build.versionName), p.output)

    # Apply patches if any
    if build.patch:
        logging.info("Applying patches")
        for patch in build.patch:
            patch = patch.strip()
            logging.info("Applying " + patch)
            patch_path = os.path.join('metadata', app.id, patch)
            p = FDroidPopen(['patch', '-p1', '-i', os.path.abspath(patch_path)], cwd=build_dir)
            if p.returncode != 0:
                raise BuildException("Failed to apply patch %s" % patch_path)

    # Get required source libraries
    srclibpaths = []
    if build.srclibs:
        logging.info("Collecting source libraries")
        for lib in build.srclibs:
            srclibpaths.append(getsrclib(lib, srclib_dir, build, preponly=onserver,
                                         refresh=refresh, build=build))

    for name, number, libpath in srclibpaths:
        place_srclib(root_dir, int(number) if number else None, libpath)

    basesrclib = vcs.getsrclib()
    # If one was used for the main source, add that too.
    if basesrclib:
        srclibpaths.append(basesrclib)

    # Update the local.properties file
    localprops = [os.path.join(build_dir, 'local.properties')]
    if build.subdir:
        parts = build.subdir.split(os.sep)
        cur = build_dir
        for d in parts:
            cur = os.path.join(cur, d)
            localprops += [os.path.join(cur, 'local.properties')]
    for path in localprops:
        props = ""
        if os.path.isfile(path):
            logging.info("Updating local.properties file at %s" % path)
            with open(path, 'r', encoding='iso-8859-1') as f:
                props += f.read()
            props += '\n'
        else:
            logging.info("Creating local.properties file at %s" % path)
        # Fix old-fashioned 'sdk-location' by copying
        # from sdk.dir, if necessary
        if build.oldsdkloc:
            sdkloc = re.match(r".*^sdk.dir=(\S+)$.*", props,
                              re.S | re.M).group(1)
            props += "sdk-location=%s\n" % sdkloc
        else:
            props += "sdk.dir=%s\n" % config['sdk_path']
            props += "sdk-location=%s\n" % config['sdk_path']
        ndk_path = build.ndk_path()
        # if for any reason the path isn't valid or the directory
        # doesn't exist, some versions of Gradle will error with a
        # cryptic message (even if the NDK is not even necessary).
        # https://gitlab.com/fdroid/fdroidserver/issues/171
        if ndk_path and os.path.exists(ndk_path):
            # Add ndk location
            props += "ndk.dir=%s\n" % ndk_path
            props += "ndk-location=%s\n" % ndk_path
        # Add java.encoding if necessary
        if build.encoding:
            props += "java.encoding=%s\n" % build.encoding
        with open(path, 'w', encoding='iso-8859-1') as f:
            f.write(props)

    flavours = []
    if build.build_method() == 'gradle':
        flavours = build.gradle

        if build.target:
            n = build.target.split('-')[1]
            build_gradle = os.path.join(root_dir, "build.gradle")
            build_gradle_kts = build_gradle + ".kts"
            if os.path.exists(build_gradle):
                gradlefile = build_gradle
            elif os.path.exist(build_gradle_kts):
                gradlefile = build_gradle_kts
            regsub_file(r'compileSdkVersion[ =]+[0-9]+',
                        r'compileSdkVersion %s' % n,
                        gradlefile)

    # Remove forced debuggable flags
    remove_debuggable_flags(root_dir)

    # Insert version code and number into the manifest if necessary
    if build.forceversion:
        logging.info("Changing the version name")
        for path in manifest_paths(root_dir, flavours):
            if not os.path.isfile(path):
                continue
            if path.endswith('.xml'):
                regsub_file(r'android:versionName="[^"]*"',
                            r'android:versionName="%s"' % build.versionName,
                            path)
            elif path.endswith('.gradle'):
                regsub_file(r"""(\s*)versionName[\s'"=]+.*""",
                            r"""\1versionName '%s'""" % build.versionName,
                            path)

    if build.forcevercode:
        logging.info("Changing the version code")
        for path in manifest_paths(root_dir, flavours):
            if not os.path.isfile(path):
                continue
            if path.endswith('.xml'):
                regsub_file(r'android:versionCode="[^"]*"',
                            r'android:versionCode="%s"' % build.versionCode,
                            path)
            elif path.endswith('.gradle'):
                regsub_file(r'versionCode[ =]+[0-9]+',
                            r'versionCode %s' % build.versionCode,
                            path)

    # Delete unwanted files
    if build.rm:
        logging.info(_("Removing specified files"))
        for part in getpaths(build_dir, build.rm):
            dest = os.path.join(build_dir, part)
            logging.info("Removing {0}".format(part))
            if os.path.lexists(dest):
                # rmtree can only handle directories that are not symlinks, so catch anything else
                if not os.path.isdir(dest) or os.path.islink(dest):
                    os.remove(dest)
                else:
                    shutil.rmtree(dest)
            else:
                logging.info("...but it didn't exist")

    remove_signing_keys(build_dir)

    # Add required external libraries
    if build.extlibs:
        logging.info("Collecting prebuilt libraries")
        libsdir = os.path.join(root_dir, 'libs')
        if not os.path.exists(libsdir):
            os.mkdir(libsdir)
        for lib in build.extlibs:
            lib = lib.strip()
            logging.info("...installing extlib {0}".format(lib))
            libf = os.path.basename(lib)
            libsrc = os.path.join(extlib_dir, lib)
            if not os.path.exists(libsrc):
                raise BuildException("Missing extlib file {0}".format(libsrc))
            shutil.copyfile(libsrc, os.path.join(libsdir, libf))

    # Run a pre-build command if one is required
    if build.prebuild:
        logging.info("Running 'prebuild' commands in %s" % root_dir)

        cmd = replace_config_vars(build.prebuild, build)

        # Substitute source library paths into prebuild commands
        for name, number, libpath in srclibpaths:
            libpath = os.path.relpath(libpath, root_dir)
            cmd = cmd.replace('$$' + name + '$$', libpath)

        p = FDroidPopen(['bash', '-x', '-c', '--', cmd], cwd=root_dir)
        if p.returncode != 0:
            raise BuildException("Error running prebuild command for %s:%s" %
                                 (app.id, build.versionName), p.output)

    # Generate (or update) the ant build file, build.xml...
    if build.build_method() == 'ant' and build.androidupdate != ['no']:
        parms = ['android', 'update', 'lib-project']
        lparms = ['android', 'update', 'project']

        if build.target:
            parms += ['-t', build.target]
            lparms += ['-t', build.target]
        if build.androidupdate:
            update_dirs = build.androidupdate
        else:
            update_dirs = ant_subprojects(root_dir) + ['.']

        for d in update_dirs:
            subdir = os.path.join(root_dir, d)
            if d == '.':
                logging.debug("Updating main project")
                cmd = parms + ['-p', d]
            else:
                logging.debug("Updating subproject %s" % d)
                cmd = lparms + ['-p', d]
            p = SdkToolsPopen(cmd, cwd=root_dir)
            # Check to see whether an error was returned without a proper exit
            # code (this is the case for the 'no target set or target invalid'
            # error)
            if p.returncode != 0 or p.output.startswith("Error: "):
                raise BuildException("Failed to update project at %s" % d, p.output)
            # Clean update dirs via ant
            if d != '.':
                logging.info("Cleaning subproject %s" % d)
                p = FDroidPopen(['ant', 'clean'], cwd=subdir)

    return (root_dir, srclibpaths)


def getpaths_map(build_dir, globpaths):
    """Extend via globbing the paths from a field and return them as a map from original path to resulting paths"""
    paths = dict()
    for p in globpaths:
        p = p.strip()
        full_path = os.path.join(build_dir, p)
        full_path = os.path.normpath(full_path)
        paths[p] = [r[len(build_dir) + 1:] for r in glob.glob(full_path)]
        if not paths[p]:
            raise FDroidException("glob path '%s' did not match any files/dirs" % p)
    return paths


def getpaths(build_dir, globpaths):
    """Extend via globbing the paths from a field and return them as a set"""
    paths_map = getpaths_map(build_dir, globpaths)
    paths = set()
    for k, v in paths_map.items():
        for p in v:
            paths.add(p)
    return paths


def natural_key(s):
    return [int(sp) if sp.isdigit() else sp for sp in re.split(r'(\d+)', s)]


def check_system_clock(dt_obj, path):
    """Check if system clock is updated based on provided date

    If an APK has files newer than the system time, suggest updating
    the system clock.  This is useful for offline systems, used for
    signing, which do not have another source of clock sync info. It
    has to be more than 24 hours newer because ZIP/APK files do not
    store timezone info

    """
    checkdt = dt_obj - timedelta(1)
    if datetime.today() < checkdt:
        logging.warning(_('System clock is older than date in {path}!').format(path=path)
                        + '\n' + _('Set clock to that time using:') + '\n'
                        + 'sudo date -s "' + str(dt_obj) + '"')


class KnownApks:
    """permanent store of existing APKs with the date they were added

    This is currently the only way to permanently store the "updated"
    date of APKs.
    """

    def __init__(self):
        '''Load filename/date info about previously seen APKs

        Since the appid and date strings both will never have spaces,
        this is parsed as a list from the end to allow the filename to
        have any combo of spaces.
        '''

        self.path = os.path.join('stats', 'known_apks.txt')
        self.apks = {}
        if os.path.isfile(self.path):
            with open(self.path, 'r') as f:
                for line in f:
                    t = line.rstrip().split(' ')
                    if len(t) == 2:
                        self.apks[t[0]] = (t[1], None)
                    else:
                        appid = t[-2]
                        date = datetime.strptime(t[-1], '%Y-%m-%d')
                        filename = line[0:line.rfind(appid) - 1]
                        self.apks[filename] = (appid, date)
                        check_system_clock(date, self.path)
        self.changed = False

    def writeifchanged(self):
        if not self.changed:
            return

        if not os.path.exists('stats'):
            os.mkdir('stats')

        lst = []
        for apk, app in self.apks.items():
            appid, added = app
            line = apk + ' ' + appid
            if added:
                line += ' ' + added.strftime('%Y-%m-%d')
            lst.append(line)

        with open(self.path, 'w') as f:
            for line in sorted(lst, key=natural_key):
                f.write(line + '\n')

    def recordapk(self, apkName, app, default_date=None):
        '''
        Record an apk (if it's new, otherwise does nothing)
        Returns the date it was added as a datetime instance
        '''
        if apkName not in self.apks:
            if default_date is None:
                default_date = datetime.utcnow()
            self.apks[apkName] = (app, default_date)
            self.changed = True
        _ignored, added = self.apks[apkName]
        return added

    def getapp(self, apkname):
        """Look up information - given the 'apkname', returns (app id, date added/None).

        Or returns None for an unknown apk.
        """
        if apkname in self.apks:
            return self.apks[apkname]
        return None

    def getlatest(self, num):
        """Get the most recent 'num' apps added to the repo, as a list of package ids with the most recent first"""
        apps = {}
        for apk, app in self.apks.items():
            appid, added = app
            if added:
                if appid in apps:
                    if apps[appid] > added:
                        apps[appid] = added
                else:
                    apps[appid] = added
        sortedapps = sorted(apps.items(), key=operator.itemgetter(1))[-num:]
        lst = [app for app, _ignored in sortedapps]
        lst.reverse()
        return lst


def get_file_extension(filename):
    """get the normalized file extension, can be blank string but never None"""
    if isinstance(filename, bytes):
        filename = filename.decode('utf-8')
    return os.path.splitext(filename)[1].lower()[1:]


def use_androguard():
    """Report if androguard is available, and config its debug logging"""

    try:
        import androguard
        if use_androguard.show_path:
            logging.debug(_('Using androguard from "{path}"').format(path=androguard.__file__))
            use_androguard.show_path = False
        if options and options.verbose:
            logging.getLogger("androguard.axml").setLevel(logging.INFO)
        return True
    except ImportError:
        return False


use_androguard.show_path = True


def _get_androguard_APK(apkfile):
    try:
        from androguard.core.bytecodes.apk import APK
    except ImportError:
        raise FDroidException("androguard library is not installed and aapt not present")

    return APK(apkfile)


def ensure_final_value(packageName, arsc, value):
    """Ensure incoming value is always the value, not the resid

    androguard will sometimes return the Android "resId" aka
    Resource ID instead of the actual value.  This checks whether
    the value is actually a resId, then performs the Android
    Resource lookup as needed.

    """
    if value:
        returnValue = value
        if value[0] == '@':
            try:  # can be a literal value or a resId
                res_id = int('0x' + value[1:], 16)
                res_id = arsc.get_id(packageName, res_id)[1]
                returnValue = arsc.get_string(packageName, res_id)[1]
            except (ValueError, TypeError):
                pass
        return returnValue
    return ''


def is_apk_and_debuggable_aapt(apkfile):
    p = SdkToolsPopen(['aapt', 'dump', 'xmltree', apkfile, 'AndroidManifest.xml'],
                      output=False)
    if p.returncode != 0:
        raise FDroidException(_("Failed to get APK manifest information"))
    for line in p.output.splitlines():
        if 'android:debuggable' in line and not line.endswith('0x0'):
            return True
    return False


def is_apk_and_debuggable_androguard(apkfile):
    """Parse only <application android:debuggable=""> from the APK"""
    from androguard.core.bytecodes.axml import AXMLParser, format_value, START_TAG
    with ZipFile(apkfile) as apk:
        with apk.open('AndroidManifest.xml') as manifest:
            axml = AXMLParser(manifest.read())
            while axml.is_valid():
                _type = next(axml)
                if _type == START_TAG and axml.getName() == 'application':
                    for i in range(0, axml.getAttributeCount()):
                        name = axml.getAttributeName(i)
                        if name == 'debuggable':
                            _type = axml.getAttributeValueType(i)
                            _data = axml.getAttributeValueData(i)
                            value = format_value(_type, _data, lambda _: axml.getAttributeValue(i))
                            if value == 'true':
                                return True
                            else:
                                return False
                    break
    return False


def is_apk_and_debuggable(apkfile):
    """Returns True if the given file is an APK and is debuggable

    :param apkfile: full path to the apk to check"""

    if get_file_extension(apkfile) != 'apk':
        return False

    if use_androguard():
        return is_apk_and_debuggable_androguard(apkfile)
    else:
        return is_apk_and_debuggable_aapt(apkfile)


def get_apk_id(apkfile):
    """Extract identification information from APK.

    Androguard is preferred since it is more reliable and a lot
    faster.  Occasionally, when androguard can't get the info from the
    APK, aapt still can.  So aapt is also used as the final fallback
    method.

    :param apkfile: path to an APK file.
    :returns: triplet (appid, version code, version name)

    """
    if use_androguard():
        try:
            return get_apk_id_androguard(apkfile)
        except zipfile.BadZipFile as e:
            logging.error(apkfile + ': ' + str(e))
            if 'aapt' in config:
                return get_apk_id_aapt(apkfile)
    else:
        return get_apk_id_aapt(apkfile)


def get_apk_id_androguard(apkfile):
    """Read (appid, versionCode, versionName) from an APK

    This first tries to do quick binary XML parsing to just get the
    values that are needed.  It will fallback to full androguard
    parsing, which is slow, if it can't find the versionName value or
    versionName is set to a Android String Resource (e.g. an integer
    hex value that starts with @).

    """
    if not os.path.exists(apkfile):
        raise FDroidException(_("Reading packageName/versionCode/versionName failed, APK invalid: '{apkfilename}'")
                              .format(apkfilename=apkfile))

    from androguard.core.bytecodes.axml import AXMLParser, format_value, START_TAG, END_TAG, TEXT, END_DOCUMENT

    appid = None
    versionCode = None
    versionName = None
    with zipfile.ZipFile(apkfile) as apk:
        with apk.open('AndroidManifest.xml') as manifest:
            axml = AXMLParser(manifest.read())
            count = 0
            while axml.is_valid():
                _type = next(axml)
                count += 1
                if _type == START_TAG:
                    for i in range(0, axml.getAttributeCount()):
                        name = axml.getAttributeName(i)
                        _type = axml.getAttributeValueType(i)
                        _data = axml.getAttributeValueData(i)
                        value = format_value(_type, _data, lambda _: axml.getAttributeValue(i))
                        if appid is None and name == 'package':
                            appid = value
                        elif versionCode is None and name == 'versionCode':
                            if value.startswith('0x'):
                                versionCode = str(int(value, 16))
                            else:
                                versionCode = value
                        elif versionName is None and name == 'versionName':
                            versionName = value

                    if axml.getName() == 'manifest':
                        break
                elif _type == END_TAG or _type == TEXT or _type == END_DOCUMENT:
                    raise RuntimeError('{path}: <manifest> must be the first element in AndroidManifest.xml'
                                       .format(path=apkfile))

    if not versionName or versionName[0] == '@':
        a = _get_androguard_APK(apkfile)
        versionName = ensure_final_value(a.package, a.get_android_resources(), a.get_androidversion_name())
    if not versionName:
        versionName = ''  # versionName is expected to always be a str

    return appid, versionCode, versionName.strip('\0')


def get_apk_id_aapt(apkfile):
    p = SdkToolsPopen(['aapt', 'dump', 'badging', apkfile], output=False)
    m = APK_ID_TRIPLET_REGEX.match(p.output[0:p.output.index('\n')])
    if m:
        return m.group(1), m.group(2), m.group(3)
    raise FDroidException(_("Reading packageName/versionCode/versionName failed, APK invalid: '{apkfilename}'")
                          .format(apkfilename=apkfile))


def get_native_code(apkfile):
    """aapt checks if there are architecture folders under the lib/ folder
    so we are simulating the same behaviour"""
    arch_re = re.compile("^lib/(.*)/.*$")
    archset = set()
    with ZipFile(apkfile) as apk:
        for filename in apk.namelist():
            m = arch_re.match(filename)
            if m:
                archset.add(m.group(1))
    return sorted(list(archset))


def get_minSdkVersion_aapt(apkfile):
    """Extract the minimum supported Android SDK from an APK using aapt

    :param apkfile: path to an APK file.
    :returns: the integer representing the SDK version
    """
    r = re.compile(r"^sdkVersion:'([0-9]+)'")
    p = SdkToolsPopen(['aapt', 'dump', 'badging', apkfile], output=False)
    for line in p.output.splitlines():
        m = r.match(line)
        if m:
            return int(m.group(1))
    raise FDroidException(_('Reading minSdkVersion failed: "{apkfilename}"')
                          .format(apkfilename=apkfile))


class PopenResult:
    def __init__(self):
        self.returncode = None
        self.output = None


def SdkToolsPopen(commands, cwd=None, output=True):
    cmd = commands[0]
    if cmd not in config:
        config[cmd] = find_sdk_tools_cmd(commands[0])
    abscmd = config[cmd]
    if abscmd is None:
        raise FDroidException(_("Could not find '{command}' on your system").format(command=cmd))
    if cmd == 'aapt':
        test_aapt_version(config['aapt'])
    return FDroidPopen([abscmd] + commands[1:],
                       cwd=cwd, output=output)


def FDroidPopenBytes(commands, cwd=None, envs=None, output=True, stderr_to_stdout=True):
    """
    Run a command and capture the possibly huge output as bytes.

    :param commands: command and argument list like in subprocess.Popen
    :param cwd: optionally specifies a working directory
    :param envs: a optional dictionary of environment variables and their values
    :returns: A PopenResult.
    """

    global env
    if env is None:
        set_FDroidPopen_env()

    process_env = env.copy()
    if envs is not None and len(envs) > 0:
        process_env.update(envs)

    if cwd:
        cwd = os.path.normpath(cwd)
        logging.debug("Directory: %s" % cwd)
    logging.debug("> %s" % ' '.join(commands))

    stderr_param = subprocess.STDOUT if stderr_to_stdout else subprocess.PIPE
    result = PopenResult()
    p = None
    try:
        p = subprocess.Popen(commands, cwd=cwd, shell=False, env=process_env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=stderr_param)
    except OSError as e:
        raise BuildException("OSError while trying to execute "
                             + ' '.join(commands) + ': ' + str(e))

    # TODO are these AsynchronousFileReader threads always exiting?
    if not stderr_to_stdout and options.verbose:
        stderr_queue = Queue()
        stderr_reader = AsynchronousFileReader(p.stderr, stderr_queue)

        while not stderr_reader.eof():
            while not stderr_queue.empty():
                line = stderr_queue.get()
                sys.stderr.buffer.write(line)
                sys.stderr.flush()

            time.sleep(0.1)

    stdout_queue = Queue()
    stdout_reader = AsynchronousFileReader(p.stdout, stdout_queue)
    buf = io.BytesIO()

    # Check the queue for output (until there is no more to get)
    while not stdout_reader.eof():
        while not stdout_queue.empty():
            line = stdout_queue.get()
            if output and options.verbose:
                # Output directly to console
                sys.stderr.buffer.write(line)
                sys.stderr.flush()
            buf.write(line)

        time.sleep(0.1)

    result.returncode = p.wait()
    result.output = buf.getvalue()
    buf.close()
    # make sure all filestreams of the subprocess are closed
    for streamvar in ['stdin', 'stdout', 'stderr']:
        if hasattr(p, streamvar):
            stream = getattr(p, streamvar)
            if stream:
                stream.close()
    return result


def FDroidPopen(commands, cwd=None, envs=None, output=True, stderr_to_stdout=True):
    """
    Run a command and capture the possibly huge output as a str.

    :param commands: command and argument list like in subprocess.Popen
    :param cwd: optionally specifies a working directory
    :param envs: a optional dictionary of environment variables and their values
    :returns: A PopenResult.
    """
    result = FDroidPopenBytes(commands, cwd, envs, output, stderr_to_stdout)
    result.output = result.output.decode('utf-8', 'ignore')
    return result


gradle_comment = re.compile(r'[ ]*//')
gradle_signing_configs = re.compile(r'^[\t ]*signingConfigs[ \t]*{[ \t]*$')
gradle_line_matches = [
    re.compile(r'^[\t ]*signingConfig\s*[= ]\s*[^ ]*$'),
    re.compile(r'.*android\.signingConfigs\.[^{]*$'),
    re.compile(r'.*release\.signingConfig *= *'),
]


def remove_signing_keys(build_dir):
    for root, dirs, files in os.walk(build_dir):
        gradlefile = None
        if 'build.gradle' in files:
            gradlefile = "build.gradle"
        elif 'build.gradle.kts' in files:
            gradlefile = "build.gradle.kts"
        if gradlefile:
            path = os.path.join(root, gradlefile)
            with open(path, "r") as o:
                lines = o.readlines()

            changed = False

            opened = 0
            i = 0
            with open(path, "w") as o:
                while i < len(lines):
                    line = lines[i]
                    i += 1
                    while line.endswith('\\\n'):
                        line = line.rstrip('\\\n') + lines[i]
                        i += 1

                    if gradle_comment.match(line):
                        o.write(line)
                        continue

                    if opened > 0:
                        opened += line.count('{')
                        opened -= line.count('}')
                        continue

                    if gradle_signing_configs.match(line):
                        changed = True
                        opened += 1
                        continue

                    if any(s.match(line) for s in gradle_line_matches):
                        changed = True
                        continue

                    if opened == 0:
                        o.write(line)

            if changed:
                logging.info("Cleaned %s of keysigning configs at %s" % (gradlefile, path))

        for propfile in [
                'project.properties',
                'build.properties',
                'default.properties',
                'ant.properties', ]:
            if propfile in files:
                path = os.path.join(root, propfile)

                with open(path, "r", encoding='iso-8859-1') as o:
                    lines = o.readlines()

                changed = False

                with open(path, "w", encoding='iso-8859-1') as o:
                    for line in lines:
                        if any(line.startswith(s) for s in ('key.store', 'key.alias')):
                            changed = True
                            continue

                        o.write(line)

                if changed:
                    logging.info("Cleaned %s of keysigning configs at %s" % (propfile, path))


def set_FDroidPopen_env(build=None):
    '''
    set up the environment variables for the build environment

    There is only a weak standard, the variables used by gradle, so also set
    up the most commonly used environment variables for SDK and NDK.  Also, if
    there is no locale set, this will set the locale (e.g. LANG) to en_US.UTF-8.
    '''
    global env, orig_path

    if env is None:
        env = os.environ
        orig_path = env['PATH']
        for n in ['ANDROID_HOME', 'ANDROID_SDK']:
            env[n] = config['sdk_path']
        for k, v in config['java_paths'].items():
            env['JAVA%s_HOME' % k] = v

    missinglocale = True
    for k, v in env.items():
        if k == 'LANG' and v != 'C':
            missinglocale = False
        elif k == 'LC_ALL':
            missinglocale = False
    if missinglocale:
        env['LANG'] = 'en_US.UTF-8'

    if build is not None:
        path = build.ndk_path()
        paths = orig_path.split(os.pathsep)
        if path not in paths:
            paths = [path] + paths
            env['PATH'] = os.pathsep.join(paths)
        for n in ['ANDROID_NDK', 'NDK', 'ANDROID_NDK_HOME']:
            env[n] = build.ndk_path()


def replace_build_vars(cmd, build):
    cmd = cmd.replace('$$COMMIT$$', build.commit)
    cmd = cmd.replace('$$VERSION$$', build.versionName)
    cmd = cmd.replace('$$VERCODE$$', build.versionCode)
    return cmd


def replace_config_vars(cmd, build):
    cmd = cmd.replace('$$SDK$$', config['sdk_path'])
    cmd = cmd.replace('$$NDK$$', build.ndk_path())
    cmd = cmd.replace('$$MVN3$$', config['mvn3'])
    if build is not None:
        cmd = replace_build_vars(cmd, build)
    return cmd


def place_srclib(root_dir, number, libpath):
    if not number:
        return
    relpath = os.path.relpath(libpath, root_dir)
    proppath = os.path.join(root_dir, 'project.properties')

    lines = []
    if os.path.isfile(proppath):
        with open(proppath, "r", encoding='iso-8859-1') as o:
            lines = o.readlines()

    with open(proppath, "w", encoding='iso-8859-1') as o:
        placed = False
        for line in lines:
            if line.startswith('android.library.reference.%d=' % number):
                o.write('android.library.reference.%d=%s\n' % (number, relpath))
                placed = True
            else:
                o.write(line)
        if not placed:
            o.write('android.library.reference.%d=%s\n' % (number, relpath))


APK_SIGNATURE_FILES = re.compile(r'META-INF/[0-9A-Za-z_\-]+\.(SF|RSA|DSA|EC)')


def signer_fingerprint_short(cert_encoded):
    """Obtain shortened sha256 signing-key fingerprint for pkcs7 DER certficate.

    Extracts the first 7 hexadecimal digits of sha256 signing-key fingerprint
    for a given pkcs7 signature.

    :param cert_encoded: Contents of an APK signing certificate.
    :returns: shortened signing-key fingerprint.
    """
    return signer_fingerprint(cert_encoded)[:7]


def signer_fingerprint(cert_encoded):
    """Obtain sha256 signing-key fingerprint for pkcs7 DER certificate.

    Extracts hexadecimal sha256 signing-key fingerprint string
    for a given pkcs7 signature.

    :param: Contents of an APK signature.
    :returns: shortened signature fingerprint.
    """
    return hashlib.sha256(cert_encoded).hexdigest()


def get_first_signer_certificate(apkpath):
    """Get the first signing certificate from the APK,  DER-encoded"""
    certs = None
    cert_encoded = None
    with zipfile.ZipFile(apkpath, 'r') as apk:
        cert_files = [n for n in apk.namelist() if SIGNATURE_BLOCK_FILE_REGEX.match(n)]
        if len(cert_files) > 1:
            logging.error(_("Found multiple JAR Signature Block Files in {path}").format(path=apkpath))
            return None
        elif len(cert_files) == 1:
            cert_encoded = get_certificate(apk.read(cert_files[0]))

    if not cert_encoded and use_androguard():
        apkobject = _get_androguard_APK(apkpath)
        certs = apkobject.get_certificates_der_v2()
        if len(certs) > 0:
            logging.debug(_('Using APK Signature v2'))
            cert_encoded = certs[0]
        if not cert_encoded:
            certs = apkobject.get_certificates_der_v3()
            if len(certs) > 0:
                logging.debug(_('Using APK Signature v3'))
                cert_encoded = certs[0]

    if not cert_encoded:
        logging.error(_("No signing certificates found in {path}").format(path=apkpath))
        return None
    return cert_encoded


def apk_signer_fingerprint(apk_path):
    """Obtain sha256 signing-key fingerprint for APK.

    Extracts hexadecimal sha256 signing-key fingerprint string
    for a given APK.

    :param apk_path: path to APK
    :returns: signature fingerprint
    """

    cert_encoded = get_first_signer_certificate(apk_path)
    if not cert_encoded:
        return None
    return signer_fingerprint(cert_encoded)


def apk_signer_fingerprint_short(apk_path):
    """Obtain shortened sha256 signing-key fingerprint for APK.

    Extracts the first 7 hexadecimal digits of sha256 signing-key fingerprint
    for a given pkcs7 APK.

    :param apk_path: path to APK
    :returns: shortened signing-key fingerprint
    """
    return apk_signer_fingerprint(apk_path)[:7]


def metadata_get_sigdir(appid, vercode=None):
    """Get signature directory for app"""
    if vercode:
        return os.path.join('metadata', appid, 'signatures', vercode)
    else:
        return os.path.join('metadata', appid, 'signatures')


def metadata_find_developer_signature(appid, vercode=None):
    """Tires to find the developer signature for given appid.

    This picks the first signature file found in metadata an returns its
    signature.

    :returns: sha256 signing key fingerprint of the developer signing key.
        None in case no signature can not be found."""

    # fetch list of dirs for all versions of signatures
    appversigdirs = []
    if vercode:
        appversigdirs.append(metadata_get_sigdir(appid, vercode))
    else:
        appsigdir = metadata_get_sigdir(appid)
        if os.path.isdir(appsigdir):
            numre = re.compile('[0-9]+')
            for ver in os.listdir(appsigdir):
                if numre.match(ver):
                    appversigdir = os.path.join(appsigdir, ver)
                    appversigdirs.append(appversigdir)

    for sigdir in appversigdirs:
        sigs = glob.glob(os.path.join(sigdir, '*.DSA')) + \
            glob.glob(os.path.join(sigdir, '*.EC')) + \
            glob.glob(os.path.join(sigdir, '*.RSA'))
        if len(sigs) > 1:
            raise FDroidException('ambiguous signatures, please make sure there is only one signature in \'{}\'. (The signature has to be the App maintainers signature for version of the APK.)'.format(sigdir))
        for sig in sigs:
            with open(sig, 'rb') as f:
                return signer_fingerprint(get_certificate(f.read()))
    return None


def metadata_find_signing_files(appid, vercode):
    """Gets a list of singed manifests and signatures.

    :param appid: app id string
    :param vercode: app version code
    :returns: a list of triplets for each signing key with following paths:
        (signature_file, singed_file, manifest_file)
    """
    ret = []
    sigdir = metadata_get_sigdir(appid, vercode)
    sigs = glob.glob(os.path.join(sigdir, '*.DSA')) + \
        glob.glob(os.path.join(sigdir, '*.EC')) + \
        glob.glob(os.path.join(sigdir, '*.RSA'))
    extre = re.compile(r'(\.DSA|\.EC|\.RSA)$')
    for sig in sigs:
        sf = extre.sub('.SF', sig)
        if os.path.isfile(sf):
            mf = os.path.join(sigdir, 'MANIFEST.MF')
            if os.path.isfile(mf):
                ret.append((sig, sf, mf))
    return ret


def metadata_find_developer_signing_files(appid, vercode):
    """Get developer signature files for specified app from metadata.

    :returns: A triplet of paths for signing files from metadata:
        (signature_file, singed_file, manifest_file)
    """
    allsigningfiles = metadata_find_signing_files(appid, vercode)
    if allsigningfiles and len(allsigningfiles) == 1:
        return allsigningfiles[0]
    else:
        return None


def apk_strip_signatures(signed_apk, strip_manifest=False):
    """Removes signatures from APK.

    :param signed_apk: path to apk file.
    :param strip_manifest: when set to True also the manifest file will
        be removed from the APK.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_apk = os.path.join(tmpdir, 'tmp.apk')
        shutil.move(signed_apk, tmp_apk)
        with ZipFile(tmp_apk, 'r') as in_apk:
            with ZipFile(signed_apk, 'w') as out_apk:
                for info in in_apk.infolist():
                    if not APK_SIGNATURE_FILES.match(info.filename):
                        if strip_manifest:
                            if info.filename != 'META-INF/MANIFEST.MF':
                                buf = in_apk.read(info.filename)
                                out_apk.writestr(info, buf)
                        else:
                            buf = in_apk.read(info.filename)
                            out_apk.writestr(info, buf)


def _zipalign(unsigned_apk, aligned_apk):
    """run 'zipalign' using standard flags used by Gradle Android Plugin

    -p was added in build-tools-23.0.0

    https://developer.android.com/studio/publish/app-signing#sign-manually
    """
    p = SdkToolsPopen(['zipalign', '-v', '-p', '4', unsigned_apk, aligned_apk])
    if p.returncode != 0:
        raise BuildException("Failed to align application")


def apk_implant_signatures(apkpath, signaturefile, signedfile, manifest):
    """Implats a signature from metadata into an APK.

    Note: this changes there supplied APK in place. So copy it if you
    need the original to be preserved.

    :param apkpath: location of the apk
    """
    # get list of available signature files in metadata
    with tempfile.TemporaryDirectory() as tmpdir:
        apkwithnewsig = os.path.join(tmpdir, 'newsig.apk')
        with ZipFile(apkpath, 'r') as in_apk:
            with ZipFile(apkwithnewsig, 'w') as out_apk:
                for sig_file in [signaturefile, signedfile, manifest]:
                    with open(sig_file, 'rb') as fp:
                        buf = fp.read()
                    info = zipfile.ZipInfo('META-INF/' + os.path.basename(sig_file))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.create_system = 0  # "Windows" aka "FAT", what Android SDK uses
                    out_apk.writestr(info, buf)
                for info in in_apk.infolist():
                    if not APK_SIGNATURE_FILES.match(info.filename):
                        if info.filename != 'META-INF/MANIFEST.MF':
                            buf = in_apk.read(info.filename)
                            out_apk.writestr(info, buf)
        os.remove(apkpath)
        _zipalign(apkwithnewsig, apkpath)


def apk_extract_signatures(apkpath, outdir, manifest=True):
    """Extracts a signature files from APK and puts them into target directory.

    :param apkpath: location of the apk
    :param outdir: folder where the extracted signature files will be stored
    :param manifest: (optionally) disable extracting manifest file
    """
    with ZipFile(apkpath, 'r') as in_apk:
        for f in in_apk.infolist():
            if APK_SIGNATURE_FILES.match(f.filename) or \
                    (manifest and f.filename == 'META-INF/MANIFEST.MF'):
                newpath = os.path.join(outdir, os.path.basename(f.filename))
                with open(newpath, 'wb') as out_file:
                    out_file.write(in_apk.read(f.filename))


def sign_apk(unsigned_path, signed_path, keyalias):
    """Sign and zipalign an unsigned APK, then save to a new file, deleting the unsigned

    android-18 (4.3) finally added support for reasonable hash
    algorithms, like SHA-256, before then, the only options were MD5
    and SHA1 :-/ This aims to use SHA-256 when the APK does not target
    older Android versions, and is therefore safe to do so.

    https://issuetracker.google.com/issues/36956587
    https://android-review.googlesource.com/c/platform/libcore/+/44491

    """

    if get_minSdkVersion_aapt(unsigned_path) < 18:
        signature_algorithm = ['-sigalg', 'SHA1withRSA', '-digestalg', 'SHA1']
    else:
        signature_algorithm = ['-sigalg', 'SHA256withRSA', '-digestalg', 'SHA-256']

    p = FDroidPopen([config['jarsigner'], '-keystore', config['keystore'],
                     '-storepass:env', 'FDROID_KEY_STORE_PASS',
                     '-keypass:env', 'FDROID_KEY_PASS']
                    + signature_algorithm + [unsigned_path, keyalias],
                    envs={
                        'FDROID_KEY_STORE_PASS': config['keystorepass'],
                        'FDROID_KEY_PASS': config['keypass'], })
    if p.returncode != 0:
        raise BuildException(_("Failed to sign application"), p.output)

    _zipalign(unsigned_path, signed_path)
    os.remove(unsigned_path)


def verify_apks(signed_apk, unsigned_apk, tmp_dir):
    """Verify that two apks are the same

    One of the inputs is signed, the other is unsigned. The signature metadata
    is transferred from the signed to the unsigned apk, and then jarsigner is
    used to verify that the signature from the signed apk is also varlid for
    the unsigned one.  If the APK given as unsigned actually does have a
    signature, it will be stripped out and ignored.

    :param signed_apk: Path to a signed apk file
    :param unsigned_apk: Path to an unsigned apk file expected to match it
    :param tmp_dir: Path to directory for temporary files
    :returns: None if the verification is successful, otherwise a string
              describing what went wrong.
    """

    if not os.path.isfile(signed_apk):
        return 'can not verify: file does not exists: {}'.format(signed_apk)

    if not os.path.isfile(unsigned_apk):
        return 'can not verify: file does not exists: {}'.format(unsigned_apk)

    with ZipFile(signed_apk, 'r') as signed:
        meta_inf_files = ['META-INF/MANIFEST.MF']
        for f in signed.namelist():
            if APK_SIGNATURE_FILES.match(f):
                meta_inf_files.append(f)
        if len(meta_inf_files) < 3:
            return "Signature files missing from {0}".format(signed_apk)

        tmp_apk = os.path.join(tmp_dir, 'sigcp_' + os.path.basename(unsigned_apk))
        with ZipFile(unsigned_apk, 'r') as unsigned:
            # only read the signature from the signed APK, everything else from unsigned
            with ZipFile(tmp_apk, 'w') as tmp:
                for filename in meta_inf_files:
                    tmp.writestr(signed.getinfo(filename), signed.read(filename))
                for info in unsigned.infolist():
                    if info.filename in meta_inf_files:
                        logging.warning('Ignoring %s from %s', info.filename, unsigned_apk)
                        continue
                    if info.filename in tmp.namelist():
                        return "duplicate filename found: " + info.filename
                    tmp.writestr(info, unsigned.read(info.filename))

    verified = verify_apk_signature(tmp_apk)

    if not verified:
        logging.info("...NOT verified - {0}".format(tmp_apk))
        return compare_apks(signed_apk, tmp_apk, tmp_dir,
                            os.path.dirname(unsigned_apk))

    logging.info("...successfully verified")
    return None


def verify_jar_signature(jar):
    """Verifies the signature of a given JAR file.

    jarsigner is very shitty: unsigned JARs pass as "verified"! So
    this has to turn on -strict then check for result 4, since this
    does not expect the signature to be from a CA-signed certificate.

    :raises: VerificationException() if the JAR's signature could not be verified

    """

    error = _('JAR signature failed to verify: {path}').format(path=jar)
    try:
        output = subprocess.check_output([config['jarsigner'], '-strict', '-verify', jar],
                                         stderr=subprocess.STDOUT)
        raise VerificationException(error + '\n' + output.decode('utf-8'))
    except subprocess.CalledProcessError as e:
        if e.returncode == 4:
            logging.debug(_('JAR signature verified: {path}').format(path=jar))
        else:
            raise VerificationException(error + '\n' + e.output.decode('utf-8'))


def verify_apk_signature(apk, min_sdk_version=None):
    """verify the signature on an APK

    Try to use apksigner whenever possible since jarsigner is very
    shitty: unsigned APKs pass as "verified"!  Warning, this does
    not work on JARs with apksigner >= 0.7 (build-tools 26.0.1)

    :returns: boolean whether the APK was verified
    """
    if set_command_in_config('apksigner'):
        args = [config['apksigner'], 'verify']
        if min_sdk_version:
            args += ['--min-sdk-version=' + min_sdk_version]
        if options.verbose:
            args += ['--verbose']
        try:
            output = subprocess.check_output(args + [apk])
            if options.verbose:
                logging.debug(apk + ': ' + output.decode('utf-8'))
            return True
        except subprocess.CalledProcessError as e:
            logging.error('\n' + apk + ': ' + e.output.decode('utf-8'))
    else:
        if not config.get('jarsigner_warning_displayed'):
            config['jarsigner_warning_displayed'] = True
            logging.warning(_("Using Java's jarsigner, not recommended for verifying APKs! Use apksigner"))
        try:
            verify_jar_signature(apk)
            return True
        except Exception as e:
            logging.error(e)
    return False


def verify_old_apk_signature(apk):
    """verify the signature on an archived APK, supporting deprecated algorithms

    F-Droid aims to keep every single binary that it ever published.  Therefore,
    it needs to be able to verify APK signatures that include deprecated/removed
    algorithms.  For example, jarsigner treats an MD5 signature as unsigned.

    jarsigner passes unsigned APKs as "verified"! So this has to turn
    on -strict then check for result 4.

    Just to be safe, this never reuses the file, and locks down the
    file permissions while in use.  That should prevent a bad actor
    from changing the settings during operation.

    :returns: boolean whether the APK was verified

    """

    _java_security = os.path.join(os.getcwd(), '.java.security')
    if os.path.exists(_java_security):
        os.remove(_java_security)
    with open(_java_security, 'w') as fp:
        fp.write('jdk.jar.disabledAlgorithms=MD2, RSA keySize < 1024')
    os.chmod(_java_security, 0o400)

    try:
        cmd = [
            config['jarsigner'],
            '-J-Djava.security.properties=' + _java_security,
            '-strict', '-verify', apk
        ]
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        if e.returncode != 4:
            output = e.output
        else:
            logging.debug(_('JAR signature verified: {path}').format(path=apk))
            return True
    finally:
        if os.path.exists(_java_security):
            os.chmod(_java_security, 0o600)
            os.remove(_java_security)

    logging.error(_('Old APK signature failed to verify: {path}').format(path=apk)
                  + '\n' + output.decode('utf-8'))
    return False


apk_badchars = re.compile('''[/ :;'"]''')


def compare_apks(apk1, apk2, tmp_dir, log_dir=None):
    """Compare two apks

    Returns None if the apk content is the same (apart from the signing key),
    otherwise a string describing what's different, or what went wrong when
    trying to do the comparison.
    """

    if not log_dir:
        log_dir = tmp_dir

    absapk1 = os.path.abspath(apk1)
    absapk2 = os.path.abspath(apk2)

    if set_command_in_config('diffoscope'):
        logfilename = os.path.join(log_dir, os.path.basename(absapk1))
        htmlfile = logfilename + '.diffoscope.html'
        textfile = logfilename + '.diffoscope.txt'
        if subprocess.call([config['diffoscope'],
                            '--max-report-size', '12345678', '--max-diff-block-lines', '128',
                            '--html', htmlfile, '--text', textfile,
                            absapk1, absapk2]) != 0:
            return("Failed to run diffoscope " + apk1)

    apk1dir = os.path.join(tmp_dir, apk_badchars.sub('_', apk1[0:-4]))  # trim .apk
    apk2dir = os.path.join(tmp_dir, apk_badchars.sub('_', apk2[0:-4]))  # trim .apk
    for d in [apk1dir, apk2dir]:
        if os.path.exists(d):
            shutil.rmtree(d)
        os.mkdir(d)
        os.mkdir(os.path.join(d, 'content'))

    # extract APK contents for comparision
    with ZipFile(absapk1, 'r') as f:
        f.extractall(path=os.path.join(apk1dir, 'content'))
    with ZipFile(absapk2, 'r') as f:
        f.extractall(path=os.path.join(apk2dir, 'content'))

    if set_command_in_config('apktool'):
        if subprocess.call([config['apktool'], 'd', absapk1, '--output', 'apktool'],
                           cwd=apk1dir) != 0:
            return("Failed to run apktool " + apk1)
        if subprocess.call([config['apktool'], 'd', absapk2, '--output', 'apktool'],
                           cwd=apk2dir) != 0:
            return("Failed to run apktool " + apk2)

    p = FDroidPopen(['diff', '-r', apk1dir, apk2dir], output=False)
    lines = p.output.splitlines()
    if len(lines) != 1 or 'META-INF' not in lines[0]:
        if set_command_in_config('meld'):
            p = FDroidPopen([config['meld'], apk1dir, apk2dir], output=False)
        return("Unexpected diff output:\n" + p.output)

    # since everything verifies, delete the comparison to keep cruft down
    shutil.rmtree(apk1dir)
    shutil.rmtree(apk2dir)

    # If we get here, it seems like they're the same!
    return None


def set_command_in_config(command):
    '''Try to find specified command in the path, if it hasn't been
    manually set in config.py.  If found, it is added to the config
    dict.  The return value says whether the command is available.

    '''
    if command in config:
        return True
    else:
        tmp = find_command(command)
        if tmp is not None:
            config[command] = tmp
            return True
    return False


def find_command(command):
    '''find the full path of a command, or None if it can't be found in the PATH'''

    def is_exe(fpath):
        return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

    fpath, fname = os.path.split(command)
    if fpath:
        if is_exe(command):
            return command
    else:
        for path in os.environ["PATH"].split(os.pathsep):
            path = path.strip('"')
            exe_file = os.path.join(path, command)
            if is_exe(exe_file):
                return exe_file

    return None


def genpassword():
    '''generate a random password for when generating keys'''
    h = hashlib.sha256()
    h.update(os.urandom(16))  # salt
    h.update(socket.getfqdn().encode('utf-8'))
    passwd = base64.b64encode(h.digest()).strip()
    return passwd.decode('utf-8')


def genkeystore(localconfig):
    """
    Generate a new key with password provided in :param localconfig and add it to new keystore
    :return: hexed public key, public key fingerprint
    """
    logging.info('Generating a new key in "' + localconfig['keystore'] + '"...')
    keystoredir = os.path.dirname(localconfig['keystore'])
    if keystoredir is None or keystoredir == '':
        keystoredir = os.path.join(os.getcwd(), keystoredir)
    if not os.path.exists(keystoredir):
        os.makedirs(keystoredir, mode=0o700)

    env_vars = {'LC_ALL': 'C.UTF-8',
                'FDROID_KEY_STORE_PASS': localconfig['keystorepass'],
                'FDROID_KEY_PASS': localconfig['keypass']}
    p = FDroidPopen([config['keytool'], '-genkey',
                     '-keystore', localconfig['keystore'],
                     '-alias', localconfig['repo_keyalias'],
                     '-keyalg', 'RSA', '-keysize', '4096',
                     '-sigalg', 'SHA256withRSA',
                     '-validity', '10000',
                     '-storepass:env', 'FDROID_KEY_STORE_PASS',
                     '-keypass:env', 'FDROID_KEY_PASS',
                     '-dname', localconfig['keydname'],
                     '-J-Duser.language=en'], envs=env_vars)
    if p.returncode != 0:
        raise BuildException("Failed to generate key", p.output)
    os.chmod(localconfig['keystore'], 0o0600)
    if not options.quiet:
        # now show the lovely key that was just generated
        p = FDroidPopen([config['keytool'], '-list', '-v',
                         '-keystore', localconfig['keystore'],
                         '-alias', localconfig['repo_keyalias'],
                         '-storepass:env', 'FDROID_KEY_STORE_PASS', '-J-Duser.language=en'], envs=env_vars)
        logging.info(p.output.strip() + '\n\n')
    # get the public key
    p = FDroidPopenBytes([config['keytool'], '-exportcert',
                          '-keystore', localconfig['keystore'],
                          '-alias', localconfig['repo_keyalias'],
                          '-storepass:env', 'FDROID_KEY_STORE_PASS']
                         + config['smartcardoptions'],
                         envs=env_vars, output=False, stderr_to_stdout=False)
    if p.returncode != 0 or len(p.output) < 20:
        raise BuildException("Failed to get public key", p.output)
    pubkey = p.output
    fingerprint = get_cert_fingerprint(pubkey)
    return hexlify(pubkey), fingerprint


def get_cert_fingerprint(pubkey):
    """
    Generate a certificate fingerprint the same way keytool does it
    (but with slightly different formatting)
    """
    digest = hashlib.sha256(pubkey).digest()
    ret = [' '.join("%02X" % b for b in bytearray(digest))]
    return " ".join(ret)


def get_certificate(signature_block_file):
    """Extracts a DER certificate from JAR Signature's "Signature Block File".

    :param signature_block_file: file bytes (as string) representing the
    certificate, as read directly out of the APK/ZIP

    :return: A binary representation of the certificate's public key,
    or None in case of error

    """
    content = decoder.decode(signature_block_file, asn1Spec=rfc2315.ContentInfo())[0]
    if content.getComponentByName('contentType') != rfc2315.signedData:
        return None
    content = decoder.decode(content.getComponentByName('content'),
                             asn1Spec=rfc2315.SignedData())[0]
    try:
        certificates = content.getComponentByName('certificates')
        cert = certificates[0].getComponentByName('certificate')
    except PyAsn1Error:
        logging.error("Certificates not found.")
        return None
    return encoder.encode(cert)


def load_stats_fdroid_signing_key_fingerprints():
    """Load list of signing-key fingerprints stored by fdroid publish from file.

    :returns: list of dictionanryies containing the singing-key fingerprints.
    """
    jar_file = os.path.join('stats', 'publishsigkeys.jar')
    if not os.path.isfile(jar_file):
        return {}
    cmd = [config['jarsigner'], '-strict', '-verify', jar_file]
    p = FDroidPopen(cmd, output=False)
    if p.returncode != 4:
        raise FDroidException("Signature validation of '{}' failed! "
                              "Please run publish again to rebuild this file.".format(jar_file))

    jar_sigkey = apk_signer_fingerprint(jar_file)
    repo_key_sig = config.get('repo_key_sha256')
    if repo_key_sig:
        if jar_sigkey != repo_key_sig:
            raise FDroidException("Signature key fingerprint of file '{}' does not match repo_key_sha256 in config.py (found fingerprint: '{}')".format(jar_file, jar_sigkey))
    else:
        logging.warning("repo_key_sha256 not in config.py, setting it to the signature key fingerprint of '{}'".format(jar_file))
        config['repo_key_sha256'] = jar_sigkey
        write_to_config(config, 'repo_key_sha256')

    with zipfile.ZipFile(jar_file, 'r') as f:
        return json.loads(str(f.read('publishsigkeys.json'), 'utf-8'))


def write_to_config(thisconfig, key, value=None, config_file=None):
    '''write a key/value to the local config.py

    NOTE: only supports writing string variables.

    :param thisconfig: config dictionary
    :param key: variable name in config.py to be overwritten/added
    :param value: optional value to be written, instead of fetched
        from 'thisconfig' dictionary.
    '''
    if value is None:
        origkey = key + '_orig'
        value = thisconfig[origkey] if origkey in thisconfig else thisconfig[key]
    cfg = config_file if config_file else 'config.py'

    # load config file, create one if it doesn't exist
    if not os.path.exists(cfg):
        open(cfg, 'a').close()
        logging.info("Creating empty " + cfg)
    with open(cfg, 'r') as f:
        lines = f.readlines()

    # make sure the file ends with a carraige return
    if len(lines) > 0:
        if not lines[-1].endswith('\n'):
            lines[-1] += '\n'

    # regex for finding and replacing python string variable
    # definitions/initializations
    pattern = re.compile(r'^[\s#]*' + key + r'\s*=\s*"[^"]*"')
    repl = key + ' = "' + value + '"'
    pattern2 = re.compile(r'^[\s#]*' + key + r"\s*=\s*'[^']*'")
    repl2 = key + " = '" + value + "'"

    # If we replaced this line once, we make sure won't be a
    # second instance of this line for this key in the document.
    didRepl = False
    # edit config file
    with open(cfg, 'w') as f:
        for line in lines:
            if pattern.match(line) or pattern2.match(line):
                if not didRepl:
                    line = pattern.sub(repl, line)
                    line = pattern2.sub(repl2, line)
                    f.write(line)
                    didRepl = True
            else:
                f.write(line)
        if not didRepl:
            f.write('\n')
            f.write(repl)
            f.write('\n')


def parse_xml(path):
    return XMLElementTree.parse(path).getroot()


def string_is_integer(string):
    try:
        int(string, 0)
        return True
    except ValueError:
        try:
            int(string)
            return True
        except ValueError:
            return False


def version_code_string_to_int(vercode):
    """Convert an version code string of any base into an int"""
    try:
        return int(vercode, 0)
    except ValueError:
        return int(vercode)


def local_rsync(options, fromdir, todir):
    '''Rsync method for local to local copying of things

    This is an rsync wrapper with all the settings for safe use within
    the various fdroidserver use cases. This uses stricter rsync
    checking on all files since people using offline mode are already
    prioritizing security above ease and speed.

    '''
    rsyncargs = ['rsync', '--recursive', '--safe-links', '--times', '--perms',
                 '--one-file-system', '--delete', '--chmod=Da+rx,Fa-x,a+r,u+w']
    if not options.no_checksum:
        rsyncargs.append('--checksum')
    if options.verbose:
        rsyncargs += ['--verbose']
    if options.quiet:
        rsyncargs += ['--quiet']
    logging.debug(' '.join(rsyncargs + [fromdir, todir]))
    if subprocess.call(rsyncargs + [fromdir, todir]) != 0:
        raise FDroidException()


def deploy_build_log_with_rsync(appid, vercode, log_content):
    """Upload build log of one individual app build to an fdroid repository.

    :param appid: package name for dientifying to which app this log belongs.
    :param vercode: version of the app to which this build belongs.
    :param log_content: Content of the log which is about to be posted.
                        Should be either a string or bytes. (bytes will
                        be decoded as 'utf-8')
    """

    if not log_content:
        logging.warning(_('skip deploying full build logs: log content is empty'))
        return

    if not os.path.exists('repo'):
        os.mkdir('repo')

    # gzip compress log file
    log_gz_path = os.path.join('repo',
                               '{appid}_{versionCode}.log.gz'.format(appid=appid,
                                                                     versionCode=vercode))

    with gzip.open(log_gz_path, 'wb') as f:
        if isinstance(log_content, str):
            f.write(bytes(log_content, 'utf-8'))
        else:
            f.write(log_content)
    rsync_status_file_to_repo(log_gz_path)


def rsync_status_file_to_repo(path, repo_subdir=None):
    """Copy a build log or status JSON to the repo using rsync"""

    if not config.get('deploy_process_logs', False):
        logging.debug(_('skip deploying full build logs: not enabled in config'))
        return

    for webroot in config.get('serverwebroot', []):
        cmd = ['rsync',
               '--archive',
               '--delete-after',
               '--safe-links']
        if options.verbose:
            cmd += ['--verbose']
        if options.quiet:
            cmd += ['--quiet']
        if 'identity_file' in config:
            cmd += ['-e', 'ssh -oBatchMode=yes -oIdentitiesOnly=yes -i ' + config['identity_file']]

        dest_path = os.path.join(webroot, "repo")
        if repo_subdir is not None:
            dest_path = os.path.join(dest_path, repo_subdir)
        if not dest_path.endswith('/'):
            dest_path += '/'  # make sure rsync knows this is a directory
        cmd += [path, dest_path]

        retcode = subprocess.call(cmd)
        if retcode:
            logging.error(_('process log deploy {path} to {dest} failed!')
                          .format(path=path, dest=webroot))
        else:
            logging.debug(_('deployed process log {path} to {dest}')
                          .format(path=path, dest=webroot))


def get_per_app_repos():
    '''per-app repos are dirs named with the packageName of a single app'''

    # Android packageNames are Java packages, they may contain uppercase or
    # lowercase letters ('A' through 'Z'), numbers, and underscores
    # ('_'). However, individual package name parts may only start with
    # letters. https://developer.android.com/guide/topics/manifest/manifest-element.html#package
    p = re.compile('^([a-zA-Z][a-zA-Z0-9_]*(\\.[a-zA-Z][a-zA-Z0-9_]*)*)?$')

    repos = []
    for root, dirs, files in os.walk(os.getcwd()):
        for d in dirs:
            print('checking', root, 'for', d)
            if d in ('archive', 'metadata', 'repo', 'srclibs', 'tmp'):
                # standard parts of an fdroid repo, so never packageNames
                continue
            elif p.match(d) \
                    and os.path.exists(os.path.join(d, 'fdroid', 'repo', 'index.jar')):
                repos.append(d)
        break
    return repos


def is_repo_file(filename):
    '''Whether the file in a repo is a build product to be delivered to users'''
    if isinstance(filename, str):
        filename = filename.encode('utf-8', errors="surrogateescape")
    return os.path.isfile(filename) \
        and not filename.endswith(b'.asc') \
        and not filename.endswith(b'.sig') \
        and not filename.endswith(b'.log.gz') \
        and os.path.basename(filename) not in [
            b'index.jar',
            b'index_unsigned.jar',
            b'index.xml',
            b'index.html',
            b'index-v1.jar',
            b'index-v1.json',
            b'categories.txt',
        ]


def get_examples_dir():
    '''Return the dir where the fdroidserver example files are available'''
    examplesdir = None
    tmp = os.path.dirname(sys.argv[0])
    if os.path.basename(tmp) == 'bin':
        egg_links = glob.glob(os.path.join(tmp, '..',
                                           'local/lib/python3.*/site-packages/fdroidserver.egg-link'))
        if egg_links:
            # installed from local git repo
            examplesdir = os.path.join(open(egg_links[0]).readline().rstrip(), 'examples')
        else:
            # try .egg layout
            examplesdir = os.path.dirname(os.path.dirname(__file__)) + '/share/doc/fdroidserver/examples'
            if not os.path.exists(examplesdir):  # use UNIX layout
                examplesdir = os.path.dirname(tmp) + '/share/doc/fdroidserver/examples'
    else:
        # we're running straight out of the git repo
        prefix = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
        examplesdir = prefix + '/examples'

    return examplesdir


def get_wiki_timestamp(timestamp=None):
    """Return current time in the standard format for posting to the wiki"""

    if timestamp is None:
        timestamp = time.gmtime()
    return time.strftime("%Y-%m-%d %H:%M:%SZ", timestamp)


def get_android_tools_versions(ndk_path=None):
    '''get a list of the versions of all installed Android SDK/NDK components'''

    global config
    sdk_path = config['sdk_path']
    if sdk_path[-1] != '/':
        sdk_path += '/'
    components = []
    if ndk_path:
        ndk_release_txt = os.path.join(ndk_path, 'RELEASE.TXT')
        if os.path.isfile(ndk_release_txt):
            with open(ndk_release_txt, 'r') as fp:
                components.append((os.path.basename(ndk_path), fp.read()[:-1]))

    pattern = re.compile('^Pkg.Revision=(.+)', re.MULTILINE)
    for root, dirs, files in os.walk(sdk_path):
        if 'source.properties' in files:
            source_properties = os.path.join(root, 'source.properties')
            with open(source_properties, 'r') as fp:
                m = pattern.search(fp.read())
                if m:
                    components.append((root[len(sdk_path):], m.group(1)))

    return components


def get_android_tools_version_log(ndk_path=None):
    '''get a list of the versions of all installed Android SDK/NDK components'''
    log = '== Installed Android Tools ==\n\n'
    components = get_android_tools_versions(ndk_path)
    for name, version in sorted(components):
        log += '* ' + name + ' (' + version + ')\n'

    return log


def get_git_describe_link():
    """Get a link to the current fdroiddata commit, to post to the wiki

    """
    try:
        output = subprocess.check_output(['git', 'describe', '--always', '--dirty', '--abbrev=0'],
                                         universal_newlines=True).strip()
    except subprocess.CalledProcessError:
        pass
    if output:
        commit = output.replace('-dirty', '')
        return ('* fdroiddata: [https://gitlab.com/fdroid/fdroiddata/commit/{commit} {id}]\n'
                .format(commit=commit, id=output))
    else:
        logging.error(_("'{path}' failed to execute!").format(path='git describe'))
        return ''


def calculate_math_string(expr):
    ops = {
        ast.Add: operator.add,
        ast.Mult: operator.mul,
        ast.Sub: operator.sub,
        ast.USub: operator.neg,
    }

    def execute_ast(node):
        if isinstance(node, ast.Num):  # <number>
            return node.n
        elif isinstance(node, ast.BinOp):  # <left> <operator> <right>
            return ops[type(node.op)](execute_ast(node.left),
                                      execute_ast(node.right))
        elif isinstance(node, ast.UnaryOp):  # <operator> <operand> e.g., -1
            return ops[type(node.op)](ast.literal_eval(node.operand))
        else:
            raise SyntaxError(node)

    try:
        if '#' in expr:
            raise SyntaxError('no comments allowed')
        return execute_ast(ast.parse(expr, mode='eval').body)
    except SyntaxError:
        raise SyntaxError("could not parse expression '{expr}', "
                          "only basic math operations are allowed (+, -, *)"
                          .format(expr=expr))


def force_exit(exitvalue=0):
    """force exit when thread operations could block the exit

    The build command has to use some threading stuff to handle the
    timeout and locks.  This seems to prevent the command from
    exiting, unless this hack is used.

    """
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exitvalue)


YAML_LINT_CONFIG = {'extends': 'default',
                    'rules': {'document-start': 'disable',
                              'line-length': 'disable',
                              'truthy': 'disable'}}


def run_yamllint(path, indent=0):

    try:
        import yamllint.config
        import yamllint.linter
    except ImportError:
        return ''

    result = []
    with open(path, 'r', encoding='utf-8') as f:
        problems = yamllint.linter.run(f, yamllint.config.YamlLintConfig(json.dumps(YAML_LINT_CONFIG)))
    for problem in problems:
        result.append(' ' * indent + path + ':' + str(problem.line) + ': ' + problem.message)
    return '\n'.join(result)
