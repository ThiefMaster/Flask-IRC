"""Various structures used by Flask-IRC"""

import itertools

class CommandStorage(object):
    """Stores multi-part commands.

    Performs fast lookups returning the command and any arguments which were
    not part of the command.

    >>> cs = CommandStorage()
    >>> cs
    <CommandStorage([])>
    >>> cs['playlist off'] = 'func_playlist_off'
    >>> cs['PLAYlist'] = 'func_show_playlist'
    >>> cs['playlist on'] = 'func_playlist_on'
    >>> cs['ping'] = 'func_ping'
    >>> cs['status'] = 'func_status'
    >>> cs['help'] = 'func_help'
    >>> len(cs)
    6
    >>> cs.lookup('playlist')
    ('func_show_playlist', [])
    >>> cs.lookup('playLIST x')
    ('func_show_playlist', ['x'])
    >>> cs.lookup('playlist xX yYyY')
    ('func_show_playlist', ['xX', 'yYyY'])
    >>> cs.lookup('playlist off')
    ('func_playlist_off', [])
    >>> cs.lookup('playlist off lol')
    ('func_playlist_off', ['lol'])
    >>> cs.lookup('nothing')
    (None, ['nothing'])
    >>> del cs['playlist x']
    Traceback (most recent call last):
      File "<stdin>", line 1, in <module>
      File "flask_irc/lookup.py", line 29, in __delitem__
        raise KeyError(cmd)
    KeyError: 'playlist x'
    >>> 'playlist' in cs
    True
    >>> del cs['playlist']
    >>> 'playlist' in cs
    False
    >>> len(cs)
    5
    >>> cs.lookup('playlist xxx')
    (None, ['playlist', 'xxx'])
    >>> dict(cs.iteritems())
    {'status': 'func_status', 'playlist off': 'func_playlist_off', 'playlist on': 'func_playlist_on', 'ping': 'func_ping', 'help': 'func_help'}
    >>> bool(cs)
    True
    >>> list(cs.iterkeys())
    ['status', 'playlist on', 'help', 'ping', 'playlist off']
    >>> list(cs.itervalues())
    ['func_status', 'func_playlist_on', 'func_help', 'func_ping', 'func_playlist_off']
    >>> list(cs)
    ['status', 'playlist on', 'help', 'ping', 'playlist off']
    >>> list(iter(cs))
    ['status', 'playlist on', 'help', 'ping', 'playlist off']
    >>> for cmd in list(cs):
    ...     del cs[cmd]
    ...
    >>> list(cs)
    []
    >>> cs
    <CommandStorage([])>
    >>> bool(cs)
    False
    """
    def __init__(self, commands={}, splitter=lambda s: s.split(' ')):
        self._dict = {}
        self._splitter = splitter
        for cmd, value in commands.iteritems():
            self[cmd] = value

    def _get_key(self, cmd):
        return tuple(self._splitter(cmd.lower()))

    def __setitem__(self, cmd, value):
        key = self._get_key(cmd)
        if key in self._dict:
            raise ValueError('Command %s already exists' % cmd)
        self._dict[key] = value

    def __getitem__(self, cmd):
        return self._dict[self._get_key(cmd)]

    def __contains__(self, cmd):
        return self._get_key(cmd) in self._dict

    def __delitem__(self, cmd):
        try:
            del self._dict[self._get_key(cmd)]
        except KeyError:
            raise KeyError(cmd)

    def __iter__(self):
        return itertools.imap(' '.join, self._dict)

    def __len__(self):
        return len(self._dict)

    def __nonzero__(self):
        return bool(self._dict)

    def iterkeys(self):
        return iter(self)

    def iteritems(self):
        return itertools.izip(self.iterkeys(), self.itervalues())

    def itervalues(self):
        return self._dict.itervalues()

    def lookup(self, line):
        args = self._splitter(line)
        parts = self._get_key(line)
        # start with longest match, work backwards
        # first match found will be longest match
        for index in xrange(len(parts), -1, -1):
            try:
                function = self._dict[parts[:index]]
            except KeyError:
                pass # no match found, try next one
            else:
                break # match, stop search
        else:
            function = None # if all else fails, report None as function
        return function, args[index:]

    def __repr__(self):
        return '<CommandStorage(%r)>' % map(' '.join, self._dict)
