# vim: fileencoding=utf8

import errno
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

__all__ = ['Bot']

NONBLOCKING = (errno.EAGAIN, errno.EWOULDBLOCK)
STOPSIGNALS = {signal.SIGINT: 'SIGINT', signal.SIGTERM: 'SIGTERM'}

class Bot(object):
    def __init__(self, app=None, logger_name=None):
        self._logger_name = logger_name
        if app is not None:
            self.app = app
            self.init_app(self.app)
        else:
            self.app = None
        self.nick = None
        self.server = None
        self.loop = pyev.default_loop()
        self.sock = None
        self.watcher = None
        self._writebuf = ''
        self._readbuf = ''
        delay = self.app.config['IRC_RECONNECT_DELAY']
        self._reconnect_tmr = pyev.Timer(delay, delay, self.loop, self._reconnect_cb)

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
        self._init_logger()

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
        self.logger.info('Starting event loop')
        self._connect()
        self._sigwatchers = [pyev.Signal(sig, self.loop, self._sig_cb)
            for sig in STOPSIGNALS.iterkeys()]
        for watcher in self._sigwatchers:
            watcher.start()
        self.loop.start()

    def send(self, line):
        self._log_io('out', line)
        self._writebuf += line + '\r\n'
        self.watcher.stop()
        self.watcher.set(self.watcher.fd, self.watcher.events | pyev.EV_WRITE)
        self.watcher.start()

    def _parse_line(self, line):
        self._log_io('in', line)
        msg = IRCMessage(line)
        if msg == 'PING':
            self.send('PONG :%s' % msg[0])
        elif msg == '001':
            self.server = str(msg.source)
            self.nick = msg[0]
            self.logger.info('Connected to %s with nick %s' % (self.server, self.nick))
        elif msg == 'ERROR':
            self.logger.warn('Received ERROR: %s' % msg[0])
            self._close()

    def _connected(self):
        if self.app.config['IRC_SERVER_PASS']:
            self.send('PASS :%s' % self.app.config['IRC_SERVER_PASS'])
        self.send('NICK %s' % self.app.config['IRC_NICK'])
        self.send('USER %s * * :%s' % (self.app.config['IRC_USER'],
            self.app.config['IRC_REALNAME']))

    def _sig_cb(self, watcher, revents):
        sig = STOPSIGNALS[watcher.signum]
        self.logger.info('Received signal %s; terminating' % sig)
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
            self.reconnect()
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
        else:
            if not buf:
                self.logger.warn('Socket hung up')
                self._close()
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
        self.reconnect()
        self.nick = None
        self.server = None

    def reconnect(self):
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
        self.source = None
        if line[0] == ':':
            source, line = line[1:].split(' ', 1)
            self.source = IRCSource(source)
        cmd, line = line.split(' ', 1)
        self.cmd = cmd.upper()
        if line.startswith(':'):
            self.args = [line[1:]]
        elif ' :' in line:
            line, long_arg = line.split(' :', 1)
            self.args = line.split(' ') + [long_arg]
        else:
            self.args = line.split(' ')

    def __eq__(self, other):
        return self.cmd == other

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
