"""
WIP
"""

# from __future__ import absolute_import, division, print_function
# __metaclass__ = type


DOCUMENTATION = """
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
        required: true
        type: str
author: dskrypa
notes:
  - WIP
"""

EXAMPLES = """
- name: Install package foo
  plex:
    x_plex_token: "{{ x_plex_token }}"
"""

import hashlib
import platform
# from distutils.spawn import find_executable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

from ansible.module_utils.basic import AnsibleModule, missing_required_lib

REQUESTS_IMP_ERR = None
try:
    import requests
except ImportError:
    import traceback
    REQUESTS_IMP_ERR = traceback.format_exc()


def test_dependencies(module):
    if REQUESTS_IMP_ERR is not None:
        module.fail_json(
            msg=missing_required_lib('requests', url='https://pypi.org/project/requests/'),
            exception=REQUESTS_IMP_ERR,
        )


def get_distro() -> Optional[str]:
    try:
        with open('/etc/lsb-release', 'r', encoding='utf-8') as f:
            release_info = f.read()
    except OSError:
        return None
    info = dict(map(str.strip, line.split('=', 1)) for line in release_info.splitlines() if line)  # noqa
    try:
        distro = info['DISTRIB_ID'].lower()
    except KeyError:
        return None
    if distro == 'ubuntu':
        return 'debian'
    return distro


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
    if system == 'linux' and (distro := get_distro()):
        releases = [rel for rel in releases if rel['distro'] == distro]
    if len(releases) == 1:
        return releases[0]
    raise RuntimeError(f'Unable to pick release from {releases=}')


def download_plex_release(release_info: dict[str, str], dir_path: Path) -> Path:
    path = dir_path.joinpath(release_info['url'].rsplit('/', 1)[-1])

    resp = requests.get(release_info['url'])
    resp.raise_for_status()

    with path.open('wb') as f:
        f.write(resp.content)

    checksum = hashlib.md5()
    with path.open('rb') as f:
        checksum.update(f.read())

    if not release_info['checksum'] == checksum.hexdigest():
        raise RuntimeError(f'checksum mismatch - expected={release_info["checksum"]} found={checksum.hexdigest()}')

    return path


def main():
    module = AnsibleModule(
        argument_spec={
            'x_plex_token': {'required': True, 'type': 'str'},
            'plex_install_path': {'required': True, 'type': 'str'},
        },
        supports_check_mode=True,
    )
    args = module.params

    install_dir = Path(args['plex_install_path'])

    release_info = get_plex_release_info(args['x_plex_token'])
    with TemporaryDirectory() as tmp_dir:
        tmp_dir = Path(tmp_dir)
        path = download_plex_release(release_info, tmp_dir)

    """
    $ cat plex_downloads_plexpass_2022-04-28.json | jq -Mr .computer.FreeBSD
    {
      "id": "freebsd",
      "name": "FreeBSD",
      "release_date": 1651087360,
      "version": "1.26.1.5772-872b93b91",
      "requirements": "<a href=\"https://support.plex.tv/hc/en-us/articles/200375666\" target=\"_blank\">FreeBSD 11.2 or newer</a>",
      "extra_info": "",
      "items_added": "",
      "items_fixed": "(DVR) Plex Tuner Service does not return non-unicode encoded channels  (#13374)\n(DVR) Plex Tuner Service only returns a small number of channels (#13261)\n(HttpClient) Plex Media Server could exit unexpectedly after HTTP requests completed with certain timing conditions (#13489)\n(Metadata) Editing the Originally Available date on an item could result in an incorrect year being stored (#13517)\n(Metadata) Manually setting the Originally Available date on an item would result in an incorrect value being stored (#13508)\n(Subtitles) Automatic character set conversion could fail with certain language codes (#13492)\n(TLS) Plex Media Server could exit unexpectedly when loading an incomplete user-provided certificate (#13484)\n(Transcoder) QSV tone-mapping could fail on Linux distributions using newer versions of libstdc++ (#13453)",
      "releases": [
        {
          "label": "Download 64-bit",
          "build": "freebsd-x86_64",
          "distro": "freebsd",
          "url": "https://downloads.plex.tv/plex-media-server-new/1.26.1.5772-872b93b91/freebsd/PlexMediaServer-1.26.1.5772-872b93b91-FreeBSD-amd64.tar.bz2",
          "checksum": "f5830143e0f8c06a65155da04cf7d2272b11a437"
        }
      ]
    }
    """

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


if __name__ == '__main__':
    main()
