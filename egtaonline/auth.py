"""Module for handling authentication"""
import getpass
from os import path

import paramiko


_AUTH_FILE = '.egta_auth_token'
_SEARCH_PATH = [_AUTH_FILE, path.expanduser(path.join('~', _AUTH_FILE))]
USER = 'deployment'
DOMAIN = 'egtaonline.eecs.umich.edu'


def load():
    """Load an authorization token"""
    for file_name in _SEARCH_PATH:  # pragma: no branch
        if path.isfile(file_name):
            with open(file_name) as fil:
                return fil.read().strip()
    raise ValueError(
        '<no auth_token supplied or found in any of: {}>'.format(
            ', '.join(_SEARCH_PATH)))


def login(email, priority=0):
    """Login and store authentication token

    Parameters
    ----------
    priority : int
        Specifies where to save authentication token in searh path.
    """
    password = getpass.getpass('{}@{} password: '.format(USER, DOMAIN))
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.WarningPolicy())
    try:
        client.connect(DOMAIN, 22, USER, password)
        stdin, stdout, _ = client.exec_command((
            'sudo -Su postgres psql -t egtaonline3_production -c '
            '"select authentication_token from users where email = \'{}\';"'
        ).format(email.replace("'", "''")))
        stdin.write(password)
        stdin.write('\n')
        stdin.flush()
        stdin.close()
        auth_token = ''.join(stdout).strip()
        if not auth_token:
            raise ValueError('no account found for email: {}'.format(email))
        with open(_SEARCH_PATH[priority], 'w') as fil:
            fil.write(auth_token)
            fil.write('\n')
    finally:
        client.close()
