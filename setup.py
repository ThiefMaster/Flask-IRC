"""
Flask-IRC
----------

A Flask extension to create an IRC bot.
"""
from setuptools import setup


setup(
    name='Flask-IRC',
    version='0.1',
    url='https://github.com/ThiefMaster/Flask-IRC',
    license='BSD',
    author='Adrian Moennich',
    author_email='adrian@planetcoding.net',
    description='Flask extension to create an IRC bot',
    long_description=__doc__,
    packages=['flask_irc'],
    zip_safe=False,
    platforms='any',
    install_requires=[
        'Flask',
        'irc',
    ],
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Environment :: Console',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: BSD License',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Topic :: Software Development :: Libraries :: Python Modules'
        'Topic :: Communications :: Chat :: Internet Relay Chat',
    ]
)
