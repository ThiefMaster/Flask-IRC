"""Various structures used by Flask-IRC"""

class CommandStorage(object):
    """Stores multi-part commands.

    Performs fast lookups returning the command and any arguments which were
    not part of the command.

    >>> cs = CommandStorage()
    >>> cs
    <CommandStorage([])>
    >>> cs['playlist off'] = 'func_playlist_off'
    >>> cs['playlist'] = 'func_show_playlist'
    >>> cs['playlist on'] = 'func_playlist_on'
    >>> cs['ping'] = 'func_ping'
    >>> cs['status'] = 'func_status'
    >>> cs['help'] = 'func_help'
    >>> len(cs)
    6
    >>> cs['playlist']
    ('func_show_playlist', [])
    >>> cs['playlist x']
    ('func_show_playlist', ['x'])
    >>> cs['playlist xx y']
    ('func_show_playlist', ['xx', 'y'])
    >>> cs['playlist off']
    ('func_playlist_off', [])
    >>> cs['playlist off lol']
    ('func_playlist_off', ['lol'])
    >>> cs['nothing']
    (None, ['nothing'])
    >>> del cs['playlist x']
    Traceback (most recent call last):
      File "<stdin>", line 1, in <module>
      File "flask_irc/lookup.py", line 29, in __delitem__
        raise KeyError(cmd)
    KeyError: 'playlist x'
    >>> del cs['playlist']
    >>> len(cs)
    5
    >>> cs['playlist xxx']
    (None, ['playlist', 'xxx'])
    >>> bool({})
    False
    >>> bool({'x':1})
    True
    >>> dict(cs.iteritems())
    {'status': 'func_status', 'playlist off': 'func_playlist_off', 'playlist on': 'func_playlist_on', 'ping': 'func_ping', 'help': 'func_help'}
    >>> bool(cs)
    True
    >>> for cmd in list(cs):
    ...     del cs[cmd]
    ...
    >>> list(cs)
    []
    >>> cs._commands
    {}
    >>> bool(cs)
    False
    """
    def __init__(self, commands={}):
        self._root = {}
        self._commands = {}
        for cmd, value in commands:
            self[cmd] = value

    def __setitem__(self, cmd, value):
        container = self._root
        parts = cmd.split(' ')
        for part in parts:
            container = container.setdefault(part, {})
        if None in container:
            raise ValueError('Command %s already exists' % cmd)
        container[None] = value
        self._commands[cmd] = value

    def __getitem__(self, line):
        return self._lookup(line)[1:]

    def __contains__(self, item):
        cmd, args = self[item]
        return cmd is not None and not args

    def __delitem__(self, cmd):
        if cmd not in self:
            raise KeyError(cmd)
        container = self._lookup(cmd)[0]
        del container[None]
        del self._commands[cmd]

    def __iter__(self):
        return iter(self._commands)

    def __len__(self):
        return len(self._commands)

    def __nonzero__(self):
        return bool(self._commands)

    def iterkeys(self):
        return self._commands.iterkeys()

    def iteritems(self):
        return self._commands.iteritems()

    def itervalues(self):
        return self._commands.itervalues()

    def _lookup(self, line):
        parts = line.split(' ')
        found = None
        found_container = None
        container = self._root
        command_parts = 0
        for i, part in enumerate(parts):
            if part not in container:
                return found_container, found, parts[command_parts:]
            container = container[part]
            if None in container:
                found_container = container
                found = container[None]
                command_parts = i + 1
        return found_container, found, parts[command_parts:]

    def __repr__(self):
        return '<CommandStorage(%r)>' % self._commands.keys()
