"""Various utility functions used by Flask-IRC"""

import sys

CODECS = ('utf-8', 'windows-1252', 'iso-8859-15')

def to_unicode(s):
    if isinstance(s, unicode):
        return s
    return ' '.join(map(_to_unicode, s.split(' ')))

def _to_unicode(word):
    for codec in CODECS:
        try:
            return word.decode(codec)
        except UnicodeDecodeError:
            pass
    return word.decode('ascii', 'replace')

def trim_docstring(docstring):
    if not docstring:
        return ''
    # Convert tabs to spaces (following the normal Python rules)
    # and split into a list of lines:
    lines = docstring.expandtabs().splitlines()
    # Determine minimum indentation (first line doesn't count):
    indent = sys.maxint
    for line in lines[1:]:
        stripped = line.lstrip()
        if stripped:
            indent = min(indent, len(line) - len(stripped))
    # Remove indentation (first line is special):
    trimmed = [lines[0].strip()]
    if indent < sys.maxint:
        for line in lines[1:]:
            trimmed.append(line[indent:].rstrip())
    # Strip off trailing and leading blank lines:
    while trimmed and not trimmed[-1]:
        trimmed.pop()
    while trimmed and not trimmed[0]:
        trimmed.pop(0)
    # Return a single string:
    return '\n'.join(trimmed)

def convert_formatting(s):
    """Converts $b, $u, $c, etc. to mirc-style codes"""
    s = s.replace('$b', '\002') # bold
    s = s.replace('$u', '\037') # underline
    s = s.replace('$c', '\003') # color
    return s
