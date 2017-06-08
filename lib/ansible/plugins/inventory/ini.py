# Copyright 2015 Abhijit Menon-Sen <ams@2ndQuadrant.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

'''
DOCUMENTATION:
    inventory: ini
    version_added: "2.4"
    short_description: Uses an Ansible INI file as inventory source.
    description:
        - INI file based inventory, sections are groups or group related with special `:modifiers`.
        - Entries in sections C([group_1]) are hosts, members of the group.
        - Hosts can have variables defined inline as key/value pairs separated by C(=).
        - The C(children) modifier indicates that the section contains groups.
        - The C(vars) modifier indicates that the section contains variables assigned to members of the group.
        - Anything found outside a section is considered an 'ungrouped' host.
    notes:
        - It takes the place of the previously hardcoded INI inventory.
        - To function it requires being whitelisted in configuration.

EXAMPLES:
  example1: |
      # example cfg file
      [web]
      host1
      host2 ansible_port=222

      [web:vars]
      http_port=8080 # all members of 'web' will inherit these
      myvar=23

      [web:children] # child groups will automatically add their hosts to partent group
      apache
      nginx

      [apache]
      tomcat1
      tomcat2 myvar=34 # host specific vars override group vars

      [nginx]
      jenkins1

      [nginx:vars]
      has_java = True # vars in child groups override same in parent

      [all:vars]
      has_java = False # 'all' is 'top' parent

  example2: |
      # other example config
      host1 # this is 'ungrouped'

      # both hsots have same IP but diff ports, also 'ungrouped'
      host2 ansible_host=127.0.0.1 ansible_port=44
      host3 ansible_host=127.0.0.1 ansible_port=45

      [g1]
      host4

      [g2]
      host4 # same host as above, but member of 2 groups, will inherit vars from both
            # inventory hostnames are unique
'''
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import ast
import re

from ansible.plugins.inventory import BaseFileInventoryPlugin, detect_range, expand_hostname_range
from ansible.parsing.utils.addresses import parse_address

from ansible.errors import AnsibleError, AnsibleParserError
from ansible.module_utils._text import to_bytes, to_text
from ansible.utils.shlex import shlex_split


class InventoryModule(BaseFileInventoryPlugin):
    """
    Takes an INI-format inventory file and builds a list of groups and subgroups
    with their associated hosts and variable settings.
    """
    NAME = 'ini'
    _COMMENT_MARKERS = frozenset((u';', u'#'))
    b_COMMENT_MARKERS = frozenset((b';', b'#'))

    def __init__(self):

        super(InventoryModule, self).__init__()

        self.patterns = {}
        self._filename = None

    def parse(self, inventory, loader, path, cache=True):

        super(InventoryModule, self).parse(inventory, loader, path)

        self._filename = path

        try:
            # Read in the hosts, groups, and variables defined in the
            # inventory file.
            if self.loader:
                (b_data, private) = self.loader._get_file_contents(path)
            else:
                b_path = to_bytes(path)
                with open(b_path, 'rb') as fh:
                    b_data = fh.read()

            try:
                # Faster to do to_text once on a long string than many
                # times on smaller strings
                data = to_text(b_data, errors='surrogate_or_strict').splitlines()
            except UnicodeError:
                # Handle non-utf8 in comment lines: https://github.com/ansible/ansible/issues/17593
                data = []
                for line in b_data.splitlines():
                    if line and line[0] in self.b_COMMENT_MARKERS:
                        # Replace is okay for comment lines
                        # data.append(to_text(line, errors='surrogate_then_replace'))
                        # Currently we only need these lines for accurate lineno in errors
                        data.append(u'')
                    else:
                        # Non-comment lines still have to be valid uf-8
                        data.append(to_text(line, errors='surrogate_or_strict'))

            self._parse(path, data)
        except Exception as e:
            raise AnsibleParserError(e)

    def _raise_error(self, message):
        raise AnsibleError("%s:%d: " % (self._filename, self.lineno) + message)

    def _parse(self, path, lines):
        '''
        Populates self.groups from the given array of lines. Raises an error on
        any parse failure.
        '''

        self._compile_patterns()

        # We behave as though the first line of the inventory is '[ungrouped]',
        # and begin to look for host definitions. We make a single pass through
        # each line of the inventory, building up self.groups and adding hosts,
        # subgroups, and setting variables as we go.

        pending_declarations = {}
        groupname = 'ungrouped'
        state = 'hosts'

        self.lineno = 0
        for line in lines:
            self.lineno += 1

            line = line.strip()
            # Skip empty lines and comments
            if not line or line[0] in self._COMMENT_MARKERS:
                continue

            # Is this a [section] header? That tells us what group we're parsing
            # definitions for, and what kind of definitions to expect.

            m = self.patterns['section'].match(line)
            if m:
                (groupname, state) = m.groups()

                state = state or 'hosts'
                if state not in ['hosts', 'children', 'vars']:
                    title = ":".join(m.groups())
                    self._raise_error("Section [%s] has unknown type: %s" % (title, state))

                # If we haven't seen this group before, we add a new Group.
                #
                # Either [groupname] or [groupname:children] is sufficient to
                # declare a group, but [groupname:vars] is allowed only if the
                # group is declared elsewhere (not necessarily earlier). We add
                # the group anyway, but make a note in pending_declarations to
                # check at the end.

                self.inventory.add_group(groupname)

                if state == 'vars':
                    pending_declarations[groupname] = dict(line=self.lineno, state=state, name=groupname)

                # When we see a declaration that we've been waiting for, we can
                # delete the note.

                if groupname in pending_declarations and state != 'vars':
                    if pending_declarations[groupname]['state'] == 'children':
                        for parent in pending_declarations[groupname]['parents']:
                            self.inventory.add_child(parent, groupname)
                    del pending_declarations[groupname]

                continue
            elif line.startswith('[') and line.endswith(']'):
                self._raise_error("Invalid section entry: '%s'. Please make sure that there are no spaces" % line +
                                  "in the section entry, and that there are no other invalid characters")

            # It's not a section, so the current state tells us what kind of
            # definition it must be. The individual parsers will raise an
            # error if we feed them something they can't digest.

            # [groupname] contains host definitions that must be added to
            # the current group.
            if state == 'hosts':
                hosts, port, variables = self._parse_host_definition(line)
                self.populate_host_vars(hosts, variables, groupname, port)

            # [groupname:vars] contains variable definitions that must be
            # applied to the current group.
            elif state == 'vars':
                (k, v) = self._parse_variable_definition(line)
                self.inventory.set_variable(groupname, k, v)

            # [groupname:children] contains subgroup names that must be
            # added as children of the current group. The subgroup names
            # must themselves be declared as groups, but as before, they
            # may only be declared later.
            elif state == 'children':
                child = self._parse_group_name(line)
                if child not in self.inventory.groups:
                    if child not in pending_declarations:
                        pending_declarations[child] = dict(line=self.lineno, state=state, name=child, parents=[groupname])
                    else:
                        pending_declarations[child]['parents'].append(groupname)
                else:
                    self.inventory.add_child(groupname, child)

            # This is a fencepost. It can happen only if the state checker
            # accepts a state that isn't handled above.
            else:
                self._raise_error("Entered unhandled state: %s" % (state))

        # Any entries in pending_declarations not removed by a group declaration above mean that there was an unresolved reference.
        # We report only the first such error here.

        for g in pending_declarations:
            if g not in self.inventory.groups:
                decl = pending_declarations[g]
                if decl['state'] == 'vars':
                    raise AnsibleError("%s:%d: Section [%s:vars] not valid for undefined group: %s" % (path, decl['line'], decl['name'], decl['name']))
                elif decl['state'] == 'children':
                    raise AnsibleError("%s:%d: Section [%s:children] includes undefined group: %s" % (path, decl['line'], decl['parents'].pop(), decl['name']))

    def _parse_group_name(self, line):
        '''
        Takes a single line and tries to parse it as a group name. Returns the
        group name if successful, or raises an error.
        '''

        m = self.patterns['groupname'].match(line)
        if m:
            return m.group(1)

        self._raise_error("Expected group name, got: %s" % (line))

    def _parse_variable_definition(self, line):
        '''
        Takes a string and tries to parse it as a variable definition. Returns
        the key and value if successful, or raises an error.
        '''

        # TODO: We parse variable assignments as a key (anything to the left of
        # an '='"), an '=', and a value (anything left) and leave the value to
        # _parse_value to sort out. We should be more systematic here about
        # defining what is acceptable, how quotes work, and so on.

        if '=' in line:
            (k, v) = [e.strip() for e in line.split("=", 1)]
            return (k, self._parse_value(v))

        self._raise_error("Expected key=value, got: %s" % (line))

    def _parse_host_definition(self, line):
        '''
        Takes a single line and tries to parse it as a host definition. Returns
        a list of Hosts if successful, or raises an error.
        '''

        # A host definition comprises (1) a non-whitespace hostname or range,
        # optionally followed by (2) a series of key="some value" assignments.
        # We ignore any trailing whitespace and/or comments. For example, here
        # are a series of host definitions in a group:
        #
        # [groupname]
        # alpha
        # beta:2345 user=admin      # we'll tell shlex
        # gamma sudo=True user=root # to ignore comments

        try:
            tokens = shlex_split(line, comments=True)
        except ValueError as e:
            self._raise_error("Error parsing host definition '%s': %s" % (line, e))

        (hostnames, port) = self._expand_hostpattern(tokens[0])

        # Try to process anything remaining as a series of key=value pairs.
        variables = {}
        for t in tokens[1:]:
            if '=' not in t:
                self._raise_error("Expected key=value host variable assignment, got: %s" % (t))
            (k, v) = t.split('=', 1)
            variables[k] = self._parse_value(v)

        return hostnames, port, variables

    def _expand_hostpattern(self, hostpattern):
        '''
        Takes a single host pattern and returns a list of hostnames and an
        optional port number that applies to all of them.
        '''

        # Can the given hostpattern be parsed as a host with an optional port
        # specification?

        try:
            (pattern, port) = parse_address(hostpattern, allow_ranges=True)
        except:
            # not a recognizable host pattern
            pattern = hostpattern
            port = None

        # Once we have separated the pattern, we expand it into list of one or
        # more hostnames, depending on whether it contains any [x:y] ranges.

        if detect_range(pattern):
            hostnames = expand_hostname_range(pattern)
        else:
            hostnames = [pattern]

        return (hostnames, port)

    @staticmethod
    def _parse_value(v):
        '''
        Attempt to transform the string value from an ini file into a basic python object
        (int, dict, list, unicode string, etc).
        '''
        try:
            v = ast.literal_eval(v)
        # Using explicit exceptions.
        # Likely a string that literal_eval does not like. We wil then just set it.
        except ValueError:
            # For some reason this was thought to be malformed.
            pass
        except SyntaxError:
            # Is this a hash with an equals at the end?
            pass
        return to_text(v, nonstring='passthru', errors='surrogate_or_strict')

    def _compile_patterns(self):
        '''
        Compiles the regular expressions required to parse the inventory and
        stores them in self.patterns.
        '''

        # Section names are square-bracketed expressions at the beginning of a
        # line, comprising (1) a group name optionally followed by (2) a tag
        # that specifies the contents of the section. We ignore any trailing
        # whitespace and/or comments. For example:
        #
        # [groupname]
        # [somegroup:vars]
        # [naughty:children] # only get coal in their stockings

        self.patterns['section'] = re.compile(
            r'''^\[
                    ([^:\]\s]+)             # group name (see groupname below)
                    (?::(\w+))?             # optional : and tag name
                \]
                \s*                         # ignore trailing whitespace
                (?:\#.*)?                   # and/or a comment till the
                $                           # end of the line
            ''', re.X
        )

        # FIXME: What are the real restrictions on group names, or rather, what
        # should they be? At the moment, they must be non-empty sequences of non
        # whitespace characters excluding ':' and ']', but we should define more
        # precise rules in order to support better diagnostics.

        self.patterns['groupname'] = re.compile(
            r'''^
                ([^:\]\s]+)
                \s*                         # ignore trailing whitespace
                (?:\#.*)?                   # and/or a comment till the
                $                           # end of the line
            ''', re.X
        )
