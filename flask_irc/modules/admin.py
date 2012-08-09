from __future__ import absolute_import
from ..bot import BotModule, CommandAborted
from ..bot import module_list as bot_module_list

admin = BotModule('Admin', __name__)

@admin.event('init')
def admin_init(state=None):
    if state:
        admin.g = state
    else:
        admin.g.confirm = set()

@admin.command('module load')
def module_load(source, channel, module):
    """Loads a module.

    Loads a module that is currently not loaded.
    """
    if module in admin.bot.modules:
        raise CommandAborted('The module %s is already loaded.' % module)
    if not admin.bot.load_module(module):
        raise CommandAborted('The module %s could not be loaded.' % module)
    return 'The module %s has been loaded.' % module

@admin.command('module unload')
def module_unload(source, channel, module):
    """Unloads a module.

    Unloads a module that is currently loaded. No module state is preserved.
    """
    if module not in admin.bot.modules:
        raise CommandAborted('The module %s is not loaded.' % module)
    admin.bot.modules[module].unload()
    return 'The module %s has been unloaded.' % module

@admin.command('module reload')
def module_reload(source, channel, module):
    """Reloads a module.

    Reloads a module that is currently loaded. The current state of the module
    is kept, i.e. besides the module's new code being loaded there should be no
    noticable side-effects.
    """
    if module not in admin.bot.modules:
        raise CommandAborted('The module %s is not loaded.' % module)
    if not admin.bot.modules[module].reload():
        raise CommandAborted('The module %s could not be reloaded.' % module)
    return 'The module %s has been reloaded.' % module

@admin.command('module list')
def module_list(source, channel, active=False):
    """Shows a list of all modules.

    Shows a list of all modules. If the 'active' switch is present, only
    currently active modules are shown.
    """
    if active:
        yield 'Active modules:'
        lst = sorted(admin.bot.modules)
    else:
        yield 'Available modules (* = active):'
        modules = set(admin.bot.modules) | set(bot_module_list)
        lst = sorted('%s%s' % (mod, '*' if mod in admin.bot.modules else '')
            for mod in modules)
    for line in lst:
        yield '  ' + line

@admin.command('die', greedy=True)
def die(source, channel, reason, force=False, ):
    """Terminates the bot."""
    if source.source in admin.g.confirm:
        msg = 'Terminated by %s' % source
        if reason:
            msg += ': %s' % reason
        admin.bot.logger.warn(msg)
        admin.bot.send('QUIT :%s' % reason or 'Received DIE')
        admin.bot.stop(graceful=not force)
    else:
        admin.g.confirm.add(source.source)
        def _expire():
            admin.g.confirm.remove(source.source)
        admin.bot.after(5, _expire)
        return 'Re-run this command within five seconds to confirm it.'
