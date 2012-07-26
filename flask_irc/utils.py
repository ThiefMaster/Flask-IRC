"""Various utility functions used by Flask-IRC"""

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
