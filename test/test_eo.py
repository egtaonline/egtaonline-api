import json
import subprocess
from os import path

import pytest


DIR = path.dirname(path.realpath(__file__))
EO = path.join(DIR, '..', 'bin', 'eo')

# TODO like egta change this so that server can be mocked and these can be
# tested without egtaonline


def run(*cmd):
    res = subprocess.run(
        (EO,) + cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = res.stdout.decode('utf-8')
    err = res.stderr.decode('utf-8')
    return not res.returncode, out, err


def test_help():
    succ, _, err = run('-h')
    assert succ, err


@pytest.mark.parametrize('cmd', ['sim', 'game', 'sched', 'sims'])
def test_cmd_help(cmd):
    succ, _, err = run(cmd, '-h')
    assert succ, err


@pytest.mark.egta
def test_sim():
    succ, out, err = run('sim')
    assert succ, err

    sim = json.loads(out)[0]
    succ, _, err = run('sim', str(sim['id']))
    assert succ, err

    succ, out, err = run('sim', sim['name'], '-n', sim['version'])
    assert succ, err
    assert sim['id'] == json.loads(out)['id']

    succ, _, err = run('sim', '--', '-1')
    assert not succ


@pytest.mark.egta
def test_game():
    succ, out, err = run('game')
    assert succ, err
    game = json.loads(out)[0]

    succ, _, err = run('game', str(game['id']))
    assert succ, err

    succ, out, err = run('game', game['name'], '-n')
    assert succ, err
    assert game['id'] == json.loads(out)['id']

    succ, _, err = run('game', str(game['id']), '--summary')
    assert succ, err

    succ, _, err = run('game', str(game['id']), '--observations')
    assert succ, err

    succ, _, err = run('game', str(game['id']), '--full')
    assert succ, err


@pytest.mark.egta
def test_sched():
    succ, out, err = run('sched')
    assert succ, err
    sched = json.loads(out)[0]

    succ, _, err = run('sched', str(sched['id']))
    assert succ, err

    succ, out, err = run('sched', sched['name'], '-n')
    assert succ, err
    assert sched['id'] == json.loads(out)['id']

    succ, _, err = run('sched', str(sched['id']), '-r')
    assert succ, err


@pytest.mark.egta
def test_sims():
    proc = subprocess.Popen(
        [EO, 'sims'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sim = json.loads(proc.stdout.readline().decode('utf-8'))
    proc.terminate()
    assert proc.wait(1), proc.stderr.read().decode('utf-8')

    succ, _, err = run('sims', str(sim['folder']))
    assert succ, err
