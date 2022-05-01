"""
WIP
"""

# from __future__ import absolute_import, division, print_function
# __metaclass__ = type

import hashlib
import platform
# from distutils.spawn import find_executable
from pathlib import Path
from shlex import shlex
from tarfile import TarFile
from tempfile import TemporaryDirectory
from typing import Optional, Union

from ansible.module_utils.basic import AnsibleModule, missing_required_lib

REQUESTS_IMP_ERR = None
try:
    import requests
except ImportError:
    import traceback
    REQUESTS_IMP_ERR = traceback.format_exc()

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
    plex_verify_checksum:
        description:
            - Whether the downloaded file's checksum should be verified
        type: bool
        default: True
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
            'plex_verify_checksum': {'default': True, 'type': 'bool'},
        },
        supports_check_mode=True,
    )
    args = module.params
    install_dir = Path(args['plex_install_path'])

    """
    TODO:
        - Implement check mode / dry run
        - Store file indicating version currently installed (or find how to detect in installed files)
        - Save previous versions, support version override
        - Create symlink {install_dir}/Plex_Media_Server -> "{install_dir}/Plex Media Server"
        - Follow ansible module interface for returning information
    """

    release_info = get_plex_release_info(args['x_plex_token'])
    with TemporaryDirectory() as tmp_dir:
        tmp_dir = Path(tmp_dir)
        path = download_plex_release(release_info, tmp_dir, args['plex_verify_checksum'])

        tar_file = TarFile.open(path)
        tar_file.extractall(tmp_dir)

    # module.fail_json(msg="To use option 'rootdir' pkg version must be 1.5 or greater")
    # return module.run_command(cmd + list(args), environ_update=pkgng_env, **kwargs)

    # if pkgs == ['*'] and p["state"] == 'latest':
    #     # Operate on all installed packages. Only state: latest makes sense here.
    #     _changed, _msg, _stdout, _stderr = upgrade_packages(module, run_pkgng)
    #     changed = changed or _changed
    #     stdout += _stdout
    #     stderr += _stderr
    #     msgs.append(_msg)

    # module.exit_json(changed=changed, msg=", ".join(msgs), stdout=stdout, stderr=stderr)


def test_dependencies(module):
    if REQUESTS_IMP_ERR is not None:
        module.fail_json(
            msg=missing_required_lib('requests', url='https://pypi.org/project/requests/'),
            exception=REQUESTS_IMP_ERR,
        )


class OsRelease:
    """
    Documentation: https://www.freedesktop.org/software/systemd/man/os-release.html

    In Python 3.10+: ``import platform; platform.freedesktop_os_release()``
    """
    __slots__ = ('source', '_data')
    _defaults = {'NAME': 'Linux', 'ID': 'linux', 'PRETTY_NAME': 'Linux', 'SYSEXT_SCOPE': 'system portable'}

    def __init__(self, path: Union[str, Path] = '/etc/os-release'):
        self.source = path = Path(path)
        self._data = self._parse(path.read_text('utf-8'))

    @classmethod
    def _parse(cls, raw_data: str) -> dict[str, str]:
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

    @property
    def id_like(self) -> frozenset[str]:
        try:
            return frozenset(self['ID_LIKE'].split())
        except KeyError:
            return frozenset()

    @property
    def all_ids(self) -> frozenset[str]:
        return frozenset((self['ID'], *self.id_like))


def get_plex_release_info(x_plex_token: str):
    params = {'channel': 'plexpass', 'X-Plex-Token': x_plex_token}
    resp = requests.get('https://plex.tv/api/downloads/5.json', params=params)
    resp.raise_for_status()

    system_name_map = {'windows': 'Windows', 'freebsd': 'FreeBSD', 'linux': 'Linux', 'macos': 'MacOS'}

    uname = platform.uname()
    system = uname.system.lower()
    build = f'{system}-{uname.machine.lower()}'
    os_release_info = resp.json()['computer'][system_name_map[system]]
    releases = [rel for rel in os_release_info['releases'] if rel['build'] == build]
    if system in ('linux', 'freebsd'):
        os_rel = OsRelease()
        os_id = os_rel['ID']
        filtered = [rel for rel in releases if rel['distro'] == os_id]
        if not filtered and (os_id_alts := os_rel.id_like):
            filtered = [rel for rel in releases if rel['distro'] in os_id_alts]
    else:
        filtered = releases

    if len(filtered) == 1:
        return filtered[0]
    raise RuntimeError(f'Unable to pick release from {filtered=}')


def download_plex_release(release_info: dict[str, str], dir_path: Path, verify: bool = True) -> Path:
    path = dir_path.joinpath(release_info['url'].rsplit('/', 1)[-1])

    resp = requests.get(release_info['url'])
    resp.raise_for_status()

    with path.open('wb') as f:
        f.write(resp.content)

    if verify:
        checksum = hashlib.md5()
        with path.open('rb') as f:
            checksum.update(f.read())

        if not release_info['checksum'] == checksum.hexdigest():
            raise RuntimeError(f'checksum mismatch - expected={release_info["checksum"]} found={checksum.hexdigest()}')

    return path


# if __name__ == '__main__':
#     main()
