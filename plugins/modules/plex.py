"""
Ansible module for installing / upgrading Plex Media Server with PlexPass.

Only FreeBSD is technically supported right now, but it has support for downloading on other OSes.  The install method
would need to change for different OSes, and the default install path would likely be different as well.

:author: Doug Skrypa
"""

import hashlib
import json
import platform
import shutil
import time
from functools import cached_property
from pathlib import Path
from shlex import shlex
from subprocess import check_call, check_output, SubprocessError
from tarfile import TarFile
from tempfile import TemporaryDirectory
from typing import Optional, Union, Any
from urllib.parse import urlencode

from ansible.module_utils.basic import AnsibleModule, missing_required_lib

DEFAULT_CACHE_DIR = '/var/tmp/plex/ansible_cache/'
# TODO: Different default path based on OS?
INSTALL_PATH_DEFAULT = '/usr/local/share/plex_media_server'     # immutable binaries; owner of contents: root
# PLEX_HOME_DEFAULT = '/usr/local/plex_media_server'              # mutable; owner of contents: plex
# RC_SERVICE_PATH = '/usr/local/etc/rc.d/plex_media_server'       # owner: root

DOCUMENTATION = f"""
---
module: plex
short_description: Plex binary downloader
description:
    - Manage Plex installation
options:
    x_plex_token:
        description:
            - The X-Plex-Token value to use for PlexPass authentication
        required: true
        type: str
    plex_install_path:
        description:
            - The directory in which Plex should be installed
        default: {INSTALL_PATH_DEFAULT}
        type: str
    verify_download_checksum:
        description:
            - Whether the downloaded file's checksum should be verified
        type: bool
        default: True
    distro:
        description:
            - The target distro for the Plex release to download (default: automatically detected)
        type: str
        default: None
    version:
        description:
            - The specific version to install
        type: str
        default: latest
author: dskrypa
notes:
  - WIP
"""

EXAMPLES = """
- name: Install Plex
  plex:
    x_plex_token: "{{ x_plex_token }}"
    plex_install_path: /usr/local/plex/
"""


def main():
    module = AnsibleModule(
        argument_spec={
            'x_plex_token': {'required': True, 'type': 'str'},
            'plex_install_path': {'default': INSTALL_PATH_DEFAULT, 'type': 'str'},
            'verify_download_checksum': {'default': True, 'type': 'bool'},
            'distro': {'type': 'str', 'default': None},
            'version': {'type': 'str', 'default': 'latest'},
        },
        supports_check_mode=True,
    )

    # TODO: Catch error for missing requests/curl and return something similar to below
    # if REQUESTS_IMP_ERR is not None:
    #     module.fail_json(
    #         msg=missing_required_lib('requests', url='https://pypi.org/project/requests/'),
    #         exception=REQUESTS_IMP_ERR,
    #     )
    #     return  # Not reachable, but makes PyCharm happy

    args = module.params
    installer = PlexInstaller(
        x_plex_token=args['x_plex_token'],
        install_dir=Path(args['plex_install_path']),
        verify_checksum=args['verify_download_checksum'],
        distro=args['distro'],
        version=args['version'],
    )
    meta = {'action': 'install' if installer.installed_version is None else 'update'}

    try:
        needs_install, reason = installer.needs_install()
        meta['versions'] = installer.versions()
    except PlexInstallError as e:
        module.fail_json(msg=str(e), **meta)
        return  # Not reachable, but makes PyCharm happy

    if module.check_mode or not needs_install:
        module.exit_json(changed=needs_install, msg=reason, **meta)
        return  # Not reachable, but makes PyCharm happy

    try:
        installer.install()
    except PlexInstallError as e:
        module.fail_json(msg=str(e), **meta)
    else:
        module.exit_json(changed=True, msg=f'Installed Plex Media Server version={installer.latest_version}', **meta)


class OsRelease:
    """
    Documentation: https://www.freedesktop.org/software/systemd/man/os-release.html

    In Python 3.10+: ``import platform; platform.freedesktop_os_release()``
    """
    _defaults = {'NAME': 'Linux', 'ID': 'linux', 'PRETTY_NAME': 'Linux', 'SYSEXT_SCOPE': 'system portable'}

    def __init__(self, path: Union[str, Path] = '/etc/os-release'):
        self.source = path = Path(path)
        self._data = self._parse(path.read_text('utf-8'))

    @classmethod
    # def _parse(cls, raw_data: str) -> dict[str, str]:
    def _parse(cls, raw_data: str):
        data = {}
        for line in filter(None, map(str.strip, raw_data.splitlines())):
            if line.startswith('#'):
                continue

            key, val = map(str.strip, line.split('=', 1))
            if val and (val[0] in '"\'' or '\\' in val):
                val = ''.join(shlex(val, posix=True))  # Some lines don't follow the spec exactly
            data[key] = val

            # The following is closer to the py impl in docs, but fails on \ escapes that are not valid in python
            # key, val = map(str.strip, line.split('=', 1))
            # if val and (val[0] in '"\'' or '\\' in val):
            #     val = ast.literal_eval(val)
            # data[key] = val

        return data

    def __getitem__(self, key: str) -> str:
        try:
            return self._data[key]
        except KeyError:
            pass
        return self._defaults[key]

    def get(self, key: str, default: str = None) -> Optional[str]:
        try:
            return self[key]
        except KeyError:
            return default

    @cached_property
    # def id_like(self) -> frozenset[str]:
    def id_like(self):
        try:
            alt_ids = set(map(str.lower, self['ID_LIKE'].split()))
        except KeyError:
            alt_ids = set()
        if self['ID'].lower() == 'ubuntu' or 'ubuntu' in alt_ids:
            alt_ids.add('debian')  # Linux Mint at least does not include debian in this value
        return frozenset(alt_ids)

    @cached_property
    # def all_ids(self) -> frozenset[str]:
    def all_ids(self):
        return frozenset((self['ID'], *self.id_like))


class PlexInstallError(Exception):
    pass


class PlexInstaller:
    def __init__(
        self,
        x_plex_token: str,
        install_dir: Union[str, Path] = None,
        verify_checksum: bool = True,
        system: str = None,
        build: str = None,
        distro: str = None,
        cache_dir: Union[str, Path] = None,
        version: str = 'latest',
    ):
        self.x_plex_token = x_plex_token
        self.verify_checksum = verify_checksum
        self.version = version or 'latest'

        self.install_dir = Path(install_dir or INSTALL_PATH_DEFAULT)
        self.version_path = self.install_dir.joinpath('__version__.txt')
        self.cache_dir = Path(cache_dir or DEFAULT_CACHE_DIR)
        self.release_cache_dir = self.cache_dir.joinpath('releases')
        if not self.release_cache_dir.exists():
            self.release_cache_dir.mkdir(parents=True)

        uname = platform.uname()
        self.system = system or uname.system.lower()
        machine = uname.machine.lower()
        if machine == 'amd64':
            machine = 'x86_64'
        self.build = build or f'{self.system}-{machine}'
        self.distro = distro

    # def needs_install(self) -> tuple[bool, str]:
    def needs_install(self):
        if self.installed_version is None:
            return True, f'Unable to find {self.version_path.as_posix()}'
        elif self.installed_version == self.target_version:
            return False, f'Version {self.installed_version} is already up to date'
        else:
            return True, f'Found existing={self.installed_version} installed, but the target={self.target_version}'

    def install(self):
        path = self.get_release()
        with TemporaryDirectory() as tmp_dir:
            tmp_dir = Path(tmp_dir)
            tar_file = TarFile.open(path)
            tar_file.extractall(tmp_dir)

            extracted_dir = next((p for p in tmp_dir.iterdir() if p.is_dir()))
            version_path = extracted_dir.joinpath('__version__.txt')
            version_path.write_text(f'{self.target_version}\n', encoding='utf-8')

            self._replace_install_dir(extracted_dir, self.install_dir)

        self.install_dir.joinpath('Plex_Media_Server').symlink_to(self.install_dir.joinpath('Plex Media Server'))
        self.__dict__['installed_version'] = self.target_version

    def _replace_install_dir(self, extracted_dir: Path, install_dir: Path):
        bkp_dir = None
        if install_dir.exists():
            bkp_dir = _unique_path(install_dir.parent, f'{install_dir.name}_bkp')
            install_dir.rename(bkp_dir)

        try:
            extracted_dir.replace(install_dir)
        except Exception:
            if bkp_dir is not None:
                bkp_dir.rename(install_dir)
            raise

        if bkp_dir is not None:
            shutil.rmtree(bkp_dir)

    # region Version Info

    @cached_property
    def installed_version(self) -> Optional[str]:
        if not self.version_path.exists():
            return None
        return self.version_path.read_text('utf-8').strip()

    @cached_property
    def target_version(self) -> str:
        return self.latest_version if self.version == 'latest' else self.version

    @cached_property
    def latest_version(self) -> str:
        return self.system_release_info['version']

    # def versions(self) -> dict[str, Optional[str]]:
    def versions(self):
        return {'installed': self.installed_version, 'target': self.target_version, 'latest': self.latest_version}

    # endregion

    @cached_property
    # def full_downloads_info(self) -> dict[str, Any]:
    def full_downloads_info(self):
        cache_path = self.cache_dir.joinpath('downloads_info.json')
        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < 600:  # 10 minutes
                with cache_path.open('r', encoding='utf-8') as f:
                    return json.load(f)

        params = {'channel': 'plexpass', 'X-Plex-Token': self.x_plex_token}
        data = get_json(f'https://plex.tv/api/downloads/5.json?{urlencode(params)}')

        with cache_path.open('w', encoding='utf-8') as f:
            json.dump(data, f)

        return data

    @cached_property
    # def system_release_info(self) -> dict[str, Any]:
    def system_release_info(self):
        try:
            return next((v for k, v in self.full_downloads_info['computer'].items() if k.lower() == self.system))
        except StopIteration:
            raise PlexInstallError(f'No Plex releases found for system={self.system}')

    @cached_property
    # def release_info(self) -> dict[str, Any]:
    def release_info(self):
        releases = [rel for rel in self.system_release_info['releases'] if rel['build'] == self.build]
        if self.distro:
            filtered = [rel for rel in releases if rel['distro'] == self.distro]
        elif self.system in ('linux', 'freebsd'):
            os_rel = OsRelease()
            os_id = os_rel['ID']
            filtered = [rel for rel in releases if rel['distro'] == os_id]
            if not filtered and (os_id_alts := os_rel.id_like):
                filtered = [rel for rel in releases if rel['distro'] in os_id_alts]
        else:
            filtered = releases

        if len(filtered) == 1:
            return filtered[0]

        spec = f'system={self.system} with build={self.build}'
        if self.distro:
            spec += f' and distro={self.distro}'
        raise PlexInstallError(f'Unable to pick Plex release for {spec} from {filtered=}')

    @cached_property
    def release_path(self) -> Path:
        if self.version == 'latest':
            return self.release_cache_dir.joinpath(self.release_info['url'].rsplit('/', 1)[-1])

        for path in self.release_cache_dir.iterdir():
            if self.version in path.name:
                return path

        raise PlexInstallError(f'Unable to find cached version={self.version} in {self.release_cache_dir.as_posix()}')

    def get_release(self) -> Path:
        if self.release_path.exists():
            return self.release_path
        return self.download_release(self.release_cache_dir)

    def download_release(self, dir_path: Path = None) -> Path:
        release_info = self.release_info
        dir_path = dir_path or self.release_cache_dir
        path = dir_path.joinpath(release_info['url'].rsplit('/', 1)[-1])

        save_file(release_info['url'], save_path=path)

        if self.verify_checksum:
            checksum = hashlib.sha1(path.read_bytes()).hexdigest()
            if not release_info['checksum'] == checksum:
                raise PlexInstallError(f'checksum mismatch - expected={release_info["checksum"]} found={checksum}')
        return path


def _unique_path(parent: Path, name: str) -> Path:
    path = parent.joinpath(name)
    n = 0
    while path.exists():
        path = parent.joinpath(f'{name}_{n}')
        n += 1

    return path


def _download_func(req_func, curl_func):
    def download_func(url: str, *args, curl_args=(), **kwargs):
        try:
            return req_func(url, *args, **kwargs)
        except ImportError:
            pass
        try:
            return curl_func(url, *args, args=curl_args, **kwargs)
        except SubprocessError:
            pass
        missing_dependencies(url)

    return download_func


# region Get Json

def _get_json_via_requests(url: str):
    import requests

    with requests.Session() as session:
        resp = session.get(url)
        resp.raise_for_status()
        return resp.json()


def _get_json_via_curl(url: str, args=()):
    stdout = check_output(['curl', url, *args])
    return json.loads(stdout)


get_json = _download_func(_get_json_via_requests, _get_json_via_curl)

# endregion


# region Get File

def _save_file_via_requests(url: str, save_path: Path):
    import requests

    with save_path.open('wb') as f, requests.Session() as session:
        resp = session.get(url)
        resp.raise_for_status()
        f.write(resp.content)


def _save_file_via_curl(url: str, save_path: Path, args=()):
    check_call(['curl', url, '-o', save_path.as_posix(), *args])


save_file = _download_func(_save_file_via_requests, _save_file_via_curl)

# endregion


def missing_dependencies(url: str):
    import platform
    from distutils.spawn import find_executable

    msg_parts = [
        f'Unable to download {url} due to missing install time dependencies.',
        'One of the following is required:',
        '  - `pkg install curl`'
    ]
    if find_executable('pip') is None:
        if platform.uname().system.lower() == 'freebsd':
            py_ver = ''.join(platform.python_version_tuple()[:2])
            cmd = f'pkg install py{py_ver}-pip'
        else:
            cmd = 'apt install python3-pip'
        extra = f' (you may need to run `{cmd}` first)'
    else:
        extra = ''

    msg_parts.append(f'  - `pip install requests`{extra}')
    raise RuntimeError('\n'.join(msg_parts))


if __name__ == '__main__':
    main()
