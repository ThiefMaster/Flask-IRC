# vim: fileencoding=utf8

import argparse
import errno
import importlib
import inspect
import itertools
import pyev
import signal
import socket
import sys
import werkzeug.exceptions
from datetime import datetime

from .structs import CommandStorage
from .utils import to_unicode, trim_docstring

try:
    from termcolor import cprint, colored
except ImportErrror:
    def cprint(msg, *args, **kwargs):
        print msg
    def colored(msg, *args, **kwargs):
        return msg

NONBLOCKING = (errno.EAGAIN, errno.EWOULDBLOCK)
STOPSIGNALS = {signal.SIGINT: 'SIGINT', signal.SIGTERM: 'SIGTERM'}
# Event types; used in the Bot._events dict
CONNECT = 'connect'
DISCONNECT = 'disconnect'
READY = 'ready'
TERMINATE = 'terminate'
BEFORE_COMMAND = 'before_command'
BOT_EVENTS = (CONNECT, DISCONNECT, READY, TERMINATE, BEFORE_COMMAND)
# Module event types
INIT = 'init'
RELOAD = 'reload'
UNLOAD = 'unload'
MOD_EVENTS = BOT_EVENTS + (INIT, RELOAD, UNLOAD)
# Events that are relayed to all modules
COMMON_EVENTS = tuple(set(MOD_EVENTS) - set((BEFORE_COMMAND,)))

module_list = {}

class Bot(object):
    def __init__(self, app=None, logger_name=None):
        self._logger_name = logger_name
        self.nick = None
        self.server = None
        self.ready = False
        self.loop = pyev.default_loop()
        self.sock = None
        self.watcher = None
        self._stop_loop = False
        self._writebuf = ''
        self._readbuf = ''
        self._handlers = {} # irc events (numerics/commands)
        self._module_handlers = {} # handlers registered by modules
        self._events = {} # special events (disconnect etc.)
        self._timers = []
        self.modules = {}
        self._commands = CommandStorage()
        # Internal handlers
        self.on('ERROR')(self._handle_error)
        self.on('PING')(self._handle_ping)
        self.on('001')(self._handle_welcome)
        self.on('PRIVMSG')(self._handle_privmsg)
        if app is not None:
            self.app = app
            self.init_app(self.app)
        else:
            self.app = None

    def init_app(self, app):
        self.app = app
        self.loop.debug = app.debug
        app.config.setdefault('IRC_SERVER_BIND', None)
        app.config.setdefault('IRC_SERVER_HOST', '127.0.0.1')
        app.config.setdefault('IRC_SERVER_PORT', 6667)
        app.config.setdefault('IRC_SERVER_PASS', None)
        app.config.setdefault('IRC_NICK', 'FlaskBot')
        app.config.setdefault('IRC_USER', 'FlaskBot')
        app.config.setdefault('IRC_REALNAME', 'FlaskBot')
        app.config.setdefault('IRC_TRIGGER', None)
        app.config.setdefault('IRC_RECONNECT_DELAY', 2)
        app.config.setdefault('IRC_DEBUG', False)
        app.config.setdefault('IRC_MODULES', [])
        self._init_logger()
        delay = self.app.config['IRC_RECONNECT_DELAY']
        self._reconnect_tmr = pyev.Timer(delay, delay, self.loop, self._reconnect_cb)

    def _init_logger(self):
        if not self._logger_name:
            self.logger = app.logger
        else:
            self.logger = self.app.logger.getChild(self._logger_name)
            if self.app.debug:
                # Nasty hack. But it works.
                self.logger.__class__ = self.app.logger.__class__

    def _log_io(self, direction, line):
        if not self.app.config['IRC_DEBUG'] or not sys.stdout.isatty():
            return
        now = datetime.now()
        ts = now.strftime('%Y-%m-%d %H:%M:%S')
        prefix = '[%s,%03d]' % (ts, now.microsecond / 1000)
        if direction == 'in':
            print prefix, colored('<< %s' % line, 'blue', attrs=['bold'])
        elif direction == 'out':
            print prefix, colored('>> %s' % line, 'green')

    def run(self):
        """Start the bot and its event loop"""
        for name in self.app.config['IRC_MODULES']:
            self.load_module(name)
        self.logger.info('Starting event loop')
        self._connect()
        self._sigwatchers = [pyev.Signal(sig, self.loop, self._sig_cb)
            for sig in STOPSIGNALS.iterkeys()]
        for watcher in self._sigwatchers:
            watcher.start()
        self.loop.start()

    def stop(self, graceful=True):
        """Stop the bot and its event loop.

        If the graceful flag is set, the write queue is flushed before
        the event loop is stopped.
        """
        if not graceful:
            self.loop.stop()
        else:
            self.watcher.stop()
            self.watcher.set(self.watcher.fd, pyev.EV_WRITE)
            self.watcher.start()
            self._stop_loop = True

    def send(self, line):
        """Send a line to the IRC server"""
        line = line.encode('utf-8')
        self._log_io('out', line)
        self._writebuf += line + '\r\n'
        self.watcher.stop()
        self.watcher.set(self.watcher.fd, self.watcher.events | pyev.EV_WRITE)
        self.watcher.start()

    def send_multi(self, fmt, data):
        for item in data:
            self.send(fmt % (item or ' '))

    def on(self, cmd, _module=None):
        """A decorator to register a handler for an IRC command"""
        def decorator(f):
            self._handlers.setdefault(cmd, []).append(f)
            # Record module handlers
            inst = getattr(f, 'im_self', None)
            if isinstance(inst, BotModule):
                self._module_handlers.setdefault(inst.name, []).append((cmd, f))
            return f
        return decorator

    def event(self, evt):
        """A decorator to register a handler for an event"""
        if evt not in BOT_EVENTS:
            raise ValueError('Unknown event name')
        def decorator(f):
            self._events.setdefault(evt, []).append(f)
            return f
        return decorator

    def after(self, delay, func):
        """Creates a timer that calls func after delay seconds"""
        def cb(watcher, revents):
            self._timers.remove(watcher)
            func()
        tmr = pyev.Timer(delay, 0, self.loop, cb)
        tmr.start()
        self._timers.append(tmr)

    def load_module(self, name):
        """Loads a module"""
        if name not in module_list or name in self.modules:
            return False
        module_list[name].init_bot(self)
        return True

    def _register_module(self, module):
        if module.name in self.modules:
            msg = 'A module named %s is already registered' % module.name
            raise ValueError(msg)
        if any(cmd in self._commands for cmd in module._commands):
            msg = 'The module %s contains a command colliding with an existing command' % (
                module.name)
            raise ValueError(msg)
        self.modules[module.name] = module
        for cmd, func in module._commands.iteritems():
            self._commands[cmd] = func
        self.logger.debug('Registered module %s' % module.name)

    def _unregister_module(self, module):
        if module.name not in self.modules:
            msg = 'A module named %s is not registered' % module.name
            raise ValueError(msg)
        del self.modules[module.name]
        # Remove module's commands
        for cmd, func in module._commands.iteritems():
            del self._commands[cmd]
        # Remove module's handlers
        for cmd, f in self._module_handlers.get(module.name, []):
            self._handlers[cmd].remove(f)
        self._module_handlers.pop(module.name, None)
        self.logger.debug('Unregistered module %s' % module.name)

    def trigger_ready(self):
        """Triggers the 'ready' event"""
        if not self.ready:
            self.ready = True
            self._trigger_event(READY)

    def _handle_error(self, msg):
        self.logger.warn('Received ERROR: %s' % msg[0])
        self._close()
        self._reconnect()

    def _handle_ping(self, msg):
        self.send('PONG :%s' % msg[0])

    def _handle_welcome(self, msg):
        self.server = str(msg.source)
        self.nick = msg[0]
        self.logger.info('Connected to %s with nick %s' % (self.server, self.nick))

    def _handle_privmsg(self, msg):
        line = msg[1]
        if msg[0] == self.nick:
            channel = None
        else:
            channel = msg[0]
            trigger = self.app.config['IRC_TRIGGER']
            if not trigger or not line.startswith(trigger):
                return
            line = line[len(trigger):]
        try:
            cmd, args = self._commands.lookup(line)
        except ValueError, e:
            self.send('NOTICE %s :%s' % (msg.source.nick, e))
            return
        if cmd:
            base_url = self.app.config.get('BASE_URL')
            with self.app.test_request_context(base_url=base_url):
                self._run_command(msg, channel, cmd, args)

    def _run_command(self, msg, channel, cmd, args):
        try:
            self._trigger_event(BEFORE_COMMAND, msg, cmd)
            cmd.module._trigger_event(BEFORE_COMMAND, msg, cmd)
            ret = cmd(msg.source, channel, args)
        except CommandAborted, e:
            self.send_multi('NOTICE %s :%%s' % msg.source.nick, unicode(e).splitlines())
            return
        except werkzeug.exceptions.Forbidden:
            self.send('NOTICE %s :Access denied.' % msg.source.nick)
            return
        else:
            log = '(%s) [%s]: %s %s' % (channel or '', msg.source.nick, cmd.name,
                ' '.join(args))
            cmd.module.logger.info(log.rstrip())
        if not ret:
            return
        self.send_multi('NOTICE %s :%%s' % msg.source.nick, ret)

    def _parse_line(self, line):
        self._log_io('in', line)
        msg = IRCMessage(line)
        for handler in self._handlers.get(msg.cmd, []):
            handler(msg)

    def _trigger_event(self, evt, *args):
        if evt not in BOT_EVENTS:
            raise ValueError('Unknown event name')
        for handler in self._events.get(evt, []):
            handler(*args)
        if evt in COMMON_EVENTS:
            for module in self.modules.itervalues():
                module._trigger_event(evt, *args)

    def _connected(self):
        self._trigger_event(CONNECT)
        if self.app.config['IRC_SERVER_PASS']:
            self.send('PASS :%s' % self.app.config['IRC_SERVER_PASS'])
        self.send('NICK %s' % self.app.config['IRC_NICK'])
        self.send('USER %s * * :%s' % (self.app.config['IRC_USER'],
            self.app.config['IRC_REALNAME']))

    def _sig_cb(self, watcher, revents):
        sig = STOPSIGNALS[watcher.signum]
        self.logger.info('Received signal %s; terminating' % sig)
        self._trigger_event(TERMINATE)
        self._close()
        self.loop.stop(pyev.EVBREAK_ALL)

    def _connect(self):
        bind_host = self.app.config['IRC_SERVER_BIND']
        host = self.app.config['IRC_SERVER_HOST']
        port = self.app.config['IRC_SERVER_PORT']
        # Resolve bind host (local)
        try:
            ai_local = socket.getaddrinfo(bind_host, 0, 0, 0, socket.SOL_TCP, socket.AI_PASSIVE)
        except Exception, e:
            self.logger.error('Could not resolve local host: %s' % e)
            return
        # Resolve connect host (remote)
        try:
            ai_remote = socket.getaddrinfo(host, port, 0, 0, socket.SOL_TCP)
        except Exception, e:
            self.logger.error('Could not resolve remote host: %s' % e)
            return
        # Get all local/remote combinations that make sense (same protocols)
        combos = itertools.product(ai_local, ai_remote)
        combos = itertools.ifilter(
            lambda c: all(c[0][i] == c[1][i] for i in xrange(3)),
            combos)
        s = None
        for local, remote in combos:
            af, socktype, proto = local[:3]
            # Create socket
            try:
                s = socket.socket(af, socktype, proto)
            except socket.error, msg:
                s = None
                continue
            # Bind socket to local address
            try:
                s.bind(local[4])
            except socket.error, msg:
                s.close()
                s = None
                continue
            # Connect socket to remote address
            try:
                s.connect(remote[4])
            except socket.error, msg:
                s.close()
                s = None
                continue
            self.logger.info('Connected to %s:%d' % remote[4])
        if not s:
            self.logger.error('Could not connect')
            self._reconnect()
            return
        s.setblocking(0)
        self.sock = s
        self.watcher = pyev.Io(s, pyev.EV_READ, self.loop, self._io_cb)
        self.watcher.start()
        self._connected()

    def _io_cb(self, watcher, revents):
        if revents & pyev.EV_READ:
            self._io_read()
        if revents & pyev.EV_WRITE:
            self._io_write()
        if self._stop_loop and not (self.watcher.events & pyev.EV_WRITE):
            self.loop.stop()
            self._stop_loop = False

    def _io_read(self):
        try:
            buf = self.sock.recv(1024)
        except socket.error, e:
            if e.args[0] not in NONBLOCKING:
                self.logger.warn('Error reading from socket: %s' % e)
                self._close()
                self._reconnect()
        else:
            if not buf:
                self.logger.warn('Socket hung up')
                self._close()
                self._reconnect()
            else:
                self._readbuf += buf
                while '\n' in self._readbuf:
                    pos = self._readbuf.index('\n')
                    line = self._readbuf[:pos].rstrip('\r')
                    self._readbuf = self._readbuf[pos + 1:]
                    self._parse_line(line)

    def _io_write(self):
        try:
            num = self.sock.send(self._writebuf)
        except socket.error, e:
            if e.args[0] not in NONBLOCKING:
                self.logger.warn('Error writing to socket: %s' % e)
                self._close()
                self._reconnect()
        else:
            self._writebuf = self._writebuf[num:]
            if not self._writebuf:
                self.watcher.stop()
                self.watcher.set(self.watcher.fd, self.watcher.events & ~pyev.EV_WRITE)
                self.watcher.start()

    def _close(self):
        self.sock.close()
        self.sock = None
        self.watcher.stop()
        self.watcher = None
        self._readbuf = ''
        self._writebuf = ''
        self._trigger_event(DISCONNECT)
        self.nick = None
        self.server = None
        self.ready = False

    def _reconnect(self):
        self._reconnect_tmr.reset()
        delay = self.app.config['IRC_RECONNECT_DELAY']
        self.logger.debug('Reconnecting in %us' % delay)

    def _reconnect_cb(self, watcher, revents):
        self._reconnect_tmr.stop()
        self.logger.debug('Reconnecting')
        self._connect()


class IRCMessage(object):
    def __init__(self, line):
        line = to_unicode(line)
        self.line = line
        if line[0] == ':':
            source, line = line[1:].split(' ', 1)
            self.source = IRCSource(source)
        else:
            self.source = IRCSourceNone()
        cmd, line = line.split(' ', 1)
        self.cmd = cmd.upper()
        if line.startswith(':'):
            self.args = [line[1:]]
        elif ' :' in line:
            line, long_arg = line.split(' :', 1)
            self.args = line.split(' ') + [long_arg]
        else:
            self.args = line.split(' ')

    def __getitem__(self, key):
        return self.args[key]

    def __str__(self):
        return '<source=%s, cmd=%s, args=%r>' % (self.source, self.cmd, self.args)

    def __repr__(self):
        if self.source:
            return '<IRCMessage(%r) from %s>' % (self.line, self.source)
        else:
            return '<IRCMessage(%r)>' % self.line


class IRCSource(object):
    def __init__(self, source):
        self.source = source
        self.complete = '!' in source
        if self.complete:
            self.nick, ident_host = source.split('!', 1)
            self.ident, self.host = ident_host.split('@', 1)
        else:
            self.nick = source
            self.ident = self.host = None

    def __str__(self):
        if self.complete:
            return '%s!%s@%s' % (self.nick, self.ident, self.host)
        else:
            return '%s' % self.nick

    def __repr__(self):
        return 'IRCSource(%r)' % self.source


class IRCSourceNone(object):
    def __init__(self):
        self.source = None
        self.complete = False
        self.nick = self.ident = self.host = None

    def __nonzero__(self):
        return False

    def __str__(self):
        return ''

    def __repr__(self):
        return 'IRCSourceNone()'


class _ModuleState(object):
    def __repr__(self):
        return '<ModuleState(%r)>' % self.__dict__


class BotModule(object):
    def __init__(self, name, import_name=None, logger_name=None):
        self._import_name = import_name
        self.name = name
        self.logger_name = logger_name
        self._reload_module = reload
        self.g = _ModuleState()
        self.bot = None
        self._events = {}
        self._commands = {}
        if self.name not in module_list:
            # Register if the module is new (i.e. not just reloaded)
            module_list[self.name] = self

    def init_bot(self, bot, _state=None):
        self.bot = bot
        self._init_logger()
        self.bot._register_module(self)
        self._trigger_event(INIT, _state)
        if self.bot.ready:
            self._trigger_event(READY)

    def _init_logger(self):
        if not self.logger_name:
            self.logger = self.bot.logger
        else:
            self.logger = self.bot.logger.getChild(self.logger_name)
            if self.bot.app.debug:
                # Nasty hack. But it works.
                self.logger.__class__ = self.bot.logger.__class__

    def reload(self):
        """Reloads the module (if it's reloadable)"""
        if not self._import_name:
            return False
        pymod = importlib.import_module(self._import_name)
        mod_var = None
        for name in dir(pymod):
            if name[0] == '_':
                continue
            candidate = getattr(pymod, name)
            if not isinstance(candidate, BotModule):
                continue
            if candidate.name == self.name:
                mod_var = name
                break
        if not mod_var:
            return False
        try:
            pymod = reload(pymod)
        except Exception, e:
            self.logger.error('Could not reload module: %s' % e)
            return False
        self.bot._unregister_module(self)
        self._trigger_event(RELOAD)
        mod = getattr(pymod, mod_var)
        module_list[mod.name] = mod # re-register new module
        mod.init_bot(self.bot, _state=self.g)
        return True

    def unload(self):
        """Unloads the module"""
        self.bot._unregister_module(self)
        self._trigger_event(UNLOAD)

    def event(self, evt):
        """A decorator to register a handler for an event"""
        if evt not in MOD_EVENTS:
            raise ValueError('Unknown event name')
        def decorator(f):
            self._events.setdefault(evt, []).append(f)
            return f
        return decorator

    def decorate(self, decorator):
        """Applies the decorator on all registered command functions.

        This is used to restrict the privileges of built-in modules since all
        commands are public by default which is usually not suitable for a
        production environment."""
        for cmd in self._commands.itervalues():
            cmd._func = decorator(cmd._func)

    def command(self, name, greedy=False):
        """A decorator to register a command

        If the greedy flag is set the last positional argument will include
        all following unused arguments."""
        def decorator(f):
            if name in self._commands:
                raise ValueError('A command named %s already exists' % name)
            self._commands[name] = _BotCommand(self, name, f, greedy)
            return f
        return decorator

    def _trigger_event(self, evt, *args):
        if evt not in MOD_EVENTS:
            raise ValueError('Unknown event name')
        for handler in self._events.get(evt, []):
            handler(*args)

    def __repr__(self):
        return '<BotModule(%s)>' % self.name

    def __str__(self):
        return self.name


class CommandAborted(Exception): pass

class _BotCommand(object):
    def __init__(self, module, name, func, greedy):
        self.module = module
        self.name = name
        self._func = func
        self._greedy = greedy
        self._greedy_arg = None
        self.shorthelp = None
        self.longhelp = None
        if func.__doc__:
            parts = trim_docstring(func.__doc__).split('\n', 1)
            self.shorthelp = parts[0].strip()
            if len(parts) > 1:
                self.longhelp = parts[1].strip()
        self._make_parser()

    def _make_parser(self):
        description = self.longhelp or self.shorthelp
        if description:
            # Let argparse deal with wrapping the help
            description = description.replace('\n', ' ')
        self._parser = _BotArgumentParser(prog=self.name, description=description)
        # HACK: We need the original signature in case of decorator usage.
        inner_func = getattr(self._func, '_wrapped', self._func)
        args, varargs, keywords, defaults = inspect.getargspec(inner_func)
        if keywords:
            raise ValueError('A command function cannot accept **kwargs')
        if self._greedy and varargs:
            raise ValueError('A command with a greedy argument cannot accept *args')
        self._varargs = bool(varargs)
        del args[:2] # skip `source` and `channel` arguments
        # {argname: defaultvalue} mapping
        defaults = dict(zip(*[reversed(l) for l in (args, defaults or [])]))
        # The greedy arg is the last one without a default value
        if self._greedy:
            self._greedy_arg = [arg for arg in args if arg not in defaults][-1]
        for arg in args:
            if arg in defaults:
                default = defaults[arg]
                argspec = ['--%s' % arg]
                if arg[0] != 'h' and not any(arg[0] == a[0] and arg != a for a in args):
                    argspec.append('-%s' % arg[0])
                if isinstance(default, bool):
                    action = 'store_%s' % str(not default).lower()
                    self._parser.add_argument(*argspec, dest=arg, required=False,
                        default=default, action=action)
                else:
                    self._parser.add_argument(*argspec, dest=arg, required=False,
                        default=default, type=unicode, metavar=arg.upper())
            else:
                metavar = arg.upper()
                if arg == self._greedy_arg:
                    metavar += '...'
                self._parser.add_argument(dest=arg, metavar=metavar, type=unicode)
        if self._varargs:
            self._parser.usage = self._parser.format_usage().rstrip() + ' ...\n'

    def _format_output(self, output):
        if not output:
            return None
        elif isinstance(output, basestring):
            ret = [output] # a single string
        else:
            ret = list(output) # probably a generator
        return map(to_unicode, ret)

    def __call__(self, source, channel, args):
        self._parser.reset()
        try:
            if self._varargs or self._greedy:
                namespace, remaining = self._parser.parse_known_args(args)
            else:
                namespace = self._parser.parse_args(args)
                remaining = []
        except _ParserExit, e:
            raise CommandAborted(e.message)
        kwargs = namespace.__dict__
        if self._greedy:
            # Merge last arg with remaining args
            greedy_value = ' '.join([kwargs[self._greedy_arg]] + remaining)
            kwargs[self._greedy_arg] = greedy_value
            remaining = []
        return self._format_output(self._func(source, channel, *remaining, **kwargs))

    def __hash__(self):
        return hash((self.name, self._func))

    def __eq__(self, other):
        return self.name == other.name and self._func == other._func

    def __ne__(self, other):
        return not (self == other)

    def __repr__(self):
        return "<BotCommand('%s', '%s', %r)>" % (self.module, self.name, self._func)


class _ParserExit(Exception): pass

class _BotArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        self.output = ''
        super(_BotArgumentParser, self).__init__(*args, **kwargs)

    def reset(self):
        self.output = ''

    def print_usage(self, file=None):
        self.output += self.format_usage()

    def print_help(self, file=None):
        self.output += self.format_help()

    def exit(self, status=0, message=None):
        if message:
            self.output += message
        raise _ParserExit(self.output)
