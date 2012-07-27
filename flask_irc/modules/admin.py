from __future__ import absolute_import
from ..bot import BotModule

admin = BotModule('Admin', __name__)

@admin.command('module load')
def module_load(source, channel, name):
    if name not in admin.bot.modules:
        admin.bot.load_module(name)

@admin.command('module unload')
def module_unload(source, channel, name):
    if name in admin.bot.modules:
        admin.bot.modules[name].unload()

@admin.command('module reload')
def module_reload(source, channel, name):
    if name in admin.bot.modules:
        admin.bot.modules[name].reload()

@admin.event('before_command')
def admin_before_command(msg, cmd):
    print 'admin :: before command %r' % cmd
