"""
Source: https://github.com/austinhyde/ansible-sshjail

Original:
Copyright (c) 2015-2018, Austin Hyde (@austinhyde)

# The MIT License (MIT)

Copyright (c) 2015 Austin Hyde

> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in
> all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
> THE SOFTWARE.
"""

import os
import pipes
import shlex
from contextlib import contextmanager

from ansible.errors import AnsibleError
from ansible.plugins.connection.ssh import Connection as SSHConnection, DOCUMENTATION as SSH_DOCUMENTATION
from ansible.module_utils._text import to_text
from ansible.plugins.loader import get_shell_plugin

__metaclass__ = type

DOCUMENTATION = '''
    connection: sshjail
    short_description: connect via ssh client binary to jail
    description:
        - This connection plugin allows ansible to communicate to the target machines via normal ssh command line.
    author: Austin Hyde (@austinhyde)
    version_added: historical
    options:
''' + SSH_DOCUMENTATION.partition('options:\n')[2]

try:
    from __main__ import display
except ImportError:
    from ansible.utils.display import Display
    display = Display()


# HACK: Ansible core does classname-based validation checks, to ensure connection plugins inherit directly from a class
# named "ConnectionBase". This intermediate class works around this limitation.
class ConnectionBase(SSHConnection):
    pass


class Connection(ConnectionBase):
    """ssh based connections"""

    transport = 'sshjail'

    def __init__(self, *args, **kwargs):
        super(Connection, self).__init__(*args, **kwargs)
        self.inventory_hostname = self.host
        self.jailspec, self.host = self.host.split('@', 1)  # jail name @ jail host
        # this way SSHConnection parent class uses the jail host as the SSH remote host

        # jail information loaded on first use by match_jail
        self.jid = None
        self.jname = None
        self.jpath = None
        self.connector = None
        # logging.warning(self._play_context.connection)

    def match_jail(self):
        if self.jid is None:
            code, stdout, stderr = self._jailhost_command("jls -q jid name host.hostname path")
            if code != 0:
                display.vvv("JLS stdout: %s" % stdout)
                raise AnsibleError("jls returned non-zero!")

            lines = stdout.strip().split(b'\n')
            found = False
            for line in lines:
                if line.strip() == '':
                    break

                jid, name, hostname, path = to_text(line).strip().split()
                if name == self.jailspec or hostname == self.jailspec:
                    self.jid = jid
                    self.jname = name
                    self.jpath = path
                    found = True
                    break

            if not found:
                raise AnsibleError("failed to find a jail with name or hostname of '%s'" % self.jailspec)

    def get_jail_path(self):
        self.match_jail()
        return self.jpath

    def get_jail_id(self):
        self.match_jail()
        return self.jid

    def get_jail_connector(self):
        if self.connector is None:
            code, _, _ = self._jailhost_command("which -s jailme")
            if code != 0:
                self.connector = 'jexec'
            else:
                self.connector = 'jailme'
        return self.connector

    def _strip_sudo(self, executable, cmd):
        words = shlex.split(cmd)
        while words[0] == executable or words[0] == 'sudo':
            cmd = words[-1]
            words = shlex.split(cmd)

        cmd = cmd.split(' ; ', 1)[1]

        # # Get the command without sudo
        # sudoless = cmd.rsplit(executable + ' -c ', 1)[1]
        # # Get the quotes
        # quotes = sudoless.partition('echo')[0]
        # # Get the string between the quotes
        # cmd = sudoless[len(quotes):-len(quotes+'?')]
        # # Drop the first command becasue we don't need it
        # cmd = cmd.split('; ', 1)[1]
        return cmd

    def _strip_sleep(self, cmd):
        # Get the command without sleep
        cmd = cmd.split(' && sleep 0', 1)[0]
        # Add back trailing quote
        cmd = '%s%s' % (cmd, "'")
        return cmd

    def _jailhost_command(self, cmd):
        return super(Connection, self).exec_command(cmd, in_data=None, sudoable=True)

    def exec_command(self, cmd, in_data=None, executable='/bin/sh', sudoable=True):
        ''' run a command in the jail '''
        slpcmd = False

        if '&& sleep 0' in cmd:
            slpcmd = True
            cmd = self._strip_sleep(cmd)

        if 'sudo' in cmd:
            cmd = self._strip_sudo(executable, cmd)

        cmd = ' '.join([executable, '-c', pipes.quote(cmd)])
        if slpcmd:
            cmd = '%s %s %s %s' % (self.get_jail_connector(), self.get_jail_id(), cmd, '&& sleep 0')
        else:
            cmd = '%s %s %s' % (self.get_jail_connector(), self.get_jail_id(), cmd)

        if self._play_context.become:
            # display.debug("_low_level_execute_command(): using become for this command")
            plugin = self.become
            shell = get_shell_plugin(executable=executable)
            cmd = plugin.build_become_command(cmd, shell)

        # display.vvv("JAIL (%s) %s" % (local_cmd), host=self.host)
        return super(Connection, self).exec_command(cmd, in_data, True)

    def _normalize_path(self, path, prefix):
        if not path.startswith(os.path.sep):
            path = os.path.join(os.path.sep, path)
        normpath = os.path.normpath(path)
        return os.path.join(prefix, normpath[1:])

    def _copy_file(self, from_file, to_file, executable='/bin/sh'):
        copycmd = ' '.join(('cp', from_file, to_file))
        if self._play_context.become:
            plugin = self.become
            shell = get_shell_plugin(executable=executable)
            copycmd = plugin.build_become_command(copycmd, shell)

        display.vvv(u"REMOTE COPY {0} TO {1}".format(from_file, to_file), host=self.inventory_hostname)
        code, stdout, stderr = self._jailhost_command(copycmd)
        if code != 0:
            raise AnsibleError("failed to copy file from %s to %s:\n%s\n%s" % (from_file, to_file, stdout, stderr))

    @contextmanager
    def tempfile(self):
        code, stdout, stderr = self._jailhost_command('mktemp')
        if code != 0:
            raise AnsibleError("failed to make temp file:\n%s\n%s" % (stdout, stderr))
        tmp = to_text(stdout.strip().split(b'\n')[-1])

        code, stdout, stderr = self._jailhost_command(' '.join(['chmod 0644', tmp]))
        if code != 0:
            raise AnsibleError("failed to make temp file %s world readable:\n%s\n%s" % (tmp, stdout, stderr))

        yield tmp

        code, stdout, stderr = self._jailhost_command(' '.join(['rm', tmp]))
        if code != 0:
            raise AnsibleError("failed to remove temp file %s:\n%s\n%s" % (tmp, stdout, stderr))

    def put_file(self, in_path, out_path):
        ''' transfer a file from local to remote jail '''
        out_path = self._normalize_path(out_path, self.get_jail_path())

        with self.tempfile() as tmp:
            super(Connection, self).put_file(in_path, tmp)
            self._copy_file(tmp, out_path)

    def fetch_file(self, in_path, out_path):
        ''' fetch a file from remote to local '''
        in_path = self._normalize_path(in_path, self.get_jail_path())

        with self.tempfile() as tmp:
            self._copy_file(in_path, tmp)
            super(Connection, self).fetch_file(tmp, out_path)
