# vim: fileencoding=utf8

import errno
import importlib
import itertools
import pyev
import signal
import socket
import sys
from datetime import datetime

try:
    from termcolor import cprint, colored
except ImportErrror:
    def cprint(msg, *args, **kwargs):
        print msg
    def colored(msg, *args, **kwargs):
        return msg

__all__ = ['Bot', 'BotModule', 'register_module']

NONBLOCKING = (errno.EAGAIN, errno.EWOULDBLOCK)
STOPSIGNALS = {signal.SIGINT: 'SIGINT', signal.SIGTERM: 'SIGTERM'}
# Event types; used in the Bot._events dict
CONNECT = 'connect'
DISCONNECT = 'disconnect'
READY = 'ready'
TERMINATE = 'terminate'
BOT_EVENTS = (CONNECT, DISCONNECT, READY, TERMINATE)
# Module event types
INIT = 'init'
RELOAD = 'reload'
UNLOAD = 'unload'
MOD_EVENTS = BOT_EVENTS + (INIT, RELOAD, UNLOAD)

modules = {}

def register_module(module):
    modules[module.name] = module

class Bot(object):
    def __init__(self, app=None, logger_name=None):
        self._logger_name = logger_name
        self.nick = None
        self.server = None
        self.ready = False
        self.loop = pyev.default_loop()
        self.sock = None
        self.watcher = None
        self._writebuf = ''
        self._readbuf = ''
        self._handlers = {} # irc events (numerics/commands)
        self._module_handlers = {} # handlers registered by modules
        self._events = {} # special events (disconnect etc.)
        self._timers = []
        self.modules = {}
        # Internal handlers
        self.on('ERROR')(self._handle_error)
        self.on('PING')(self._handle_ping)
        self.on('001')(self._handle_welcome)
        if app is not None:
            self.app = app
            self.init_app(self.app)
        else:
            self.app = None

    def init_app(self, app):
        self.app = app
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

    def send(self, line):
        """Send a line to the IRC server"""
        self._log_io('out', line)
        self._writebuf += line + '\r\n'
        self.watcher.stop()
        self.watcher.set(self.watcher.fd, self.watcher.events | pyev.EV_WRITE)
        self.watcher.start()

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
        if name not in modules or name in self.modules:
            return False
        modules[name].init_bot(self)
        return True

    def _register_module(self, module):
        if module.name in self.modules:
            msg = 'A module named %s is already registered' % module.name
            raise ValueError(msg)
        self.modules[module.name] = module
        self.logger.debug('Registered module %s' % module.name)

    def _unregister_module(self, module):
        if module.name not in self.modules:
            msg = 'A module named %s is not registered' % module.name
            raise ValueError(msg)
        del self.modules[module.name]
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
        self._logger_name = logger_name
        self._reload_module = reload
        self.g = _ModuleState()
        self.bot = None
        self._events = {}

    def init_bot(self, bot, _state=None):
        self.bot = bot
        self._init_logger()
        self.bot._register_module(self)
        self._trigger_event(INIT, _state)
        if self.bot.ready:
            self._trigger_event(READY)

    def _init_logger(self):
        if not self._logger_name:
            self.logger = self.bot.logger
        else:
            self.logger = self.bot.logger.getChild(self._logger_name)
            if self.app.debug:
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
        self.bot._unregister_module(self)
        self._trigger_event(RELOAD)
        pymod = reload(pymod)
        mod = getattr(pymod, mod_var)
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

    def command(self, name):
        """A decorator to register a command"""
        def decorator(f):
            return f
        return decorator

    def _trigger_event(self, evt, *args):
        if evt not in MOD_EVENTS:
            raise ValueError('Unknown event name')
        for handler in self._events.get(evt, []):
            handler(*args)

    def __repr__(self):
        return '<BotModule(%s)>' % self.name
