import io
import json
import sys
import traceback
from unittest import mock

import pytest

from egtaonline import __main__ as main
from egtaonline import api
from egtaonline import mockserver


# TODO like egta change this so that server can be mocked and these can be
# tested without egtaonline


async def run(*args):
    """Run a command line and return if it ran successfully"""
    try:
        await main.amain(args)
    except SystemExit as ex:
        return not int(str(ex))
    except Exception:
        traceback.print_exc()
        return False
    return True


def stdin(inp):
    """Patch stdin with input"""
    return mock.patch.object(sys, 'stdin', io.StringIO(inp))


def stdout():
    """Patch stdout and return stringio"""
    return mock.patch.object(sys, 'stdout', io.StringIO())


def stderr():
    """Patch stderr and return stringio"""
    return mock.patch.object(sys, 'stderr', io.StringIO())


@pytest.mark.asyncio
async def test_help():
    with stderr() as err:
        assert await run('-h'), err.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize('cmd', ['sim', 'game', 'sched', 'sims'])
async def test_cmd_help(cmd):
    with stderr() as err:
        assert await run(cmd, '-h'), err.getvalue()


# TODO in python 3.6 we can probably do async fixtures with context managers

@pytest.mark.asyncio
async def test_sim():
    async with mockserver.server() as server:
        with stdout() as out, stderr() as err:
            assert await run('sim'), err.getvalue()
        assert not out.getvalue()

        server.create_simulator('sim', '1')
        with stdout() as out, stderr() as err:
            assert await run('sim'), err.getvalue()

        sim = json.loads(out.getvalue())
        with stderr() as err:
            assert await run('sim', str(sim['id'])), err.getvalue()

        with stdout() as out, stderr() as err:
            assert await run(
                'sim', sim['name'], '-n', sim['version']), err.getvalue()
        assert sim['id'] == json.loads(out.getvalue())['id']

        assert not await run('sim', '--', '-1')

        # FIXME Test sims with two simulators


@pytest.mark.asyncio
async def test_game(tmpdir):
    conf = str(tmpdir.join('conf.json'))
    with open(conf, 'w') as f:
        json.dump({}, f)

    async with mockserver.server() as server:
        with stdout() as out, stderr() as err:
            assert await run('game'), err.getvalue()
        assert not out.getvalue()

        sim_id = server.create_simulator('sim', '1')
        game_spec = {
            'players': {
                'r': 2,
            },
            'strategies': {
                'r': ['s0', 's1'],
            },
        }
        with stdin(json.dumps(game_spec['strategies'])), stderr() as err:
            assert await run('sim', str(sim_id), '-j-'), err.getvalue()
        with stdin(json.dumps(game_spec)), stdout() as out, \
                stderr() as err:
            assert await run(
                'game', str(sim_id), '-j-', '--fetch-conf',
                conf), err.getvalue()
        game = json.loads(out.getvalue())

        with stdout() as out, stderr() as err:
            assert await run('game'), err.getvalue()
        game2 = json.loads(out.getvalue())
        assert game == game2

        with stderr() as err:
            assert await run('game', str(game['id'])), err.getvalue()

        with stderr() as err:
            assert await run(
                'game', str(game['id']), '--summary'), err.getvalue()

        with stderr() as err:
            assert await run(
                'game', str(game['id']), '--observations'), err.getvalue()

        with stderr() as err:
            assert await run(
                'game', str(game['id']), '--full'), err.getvalue()

        with stdout() as out, stderr() as err:
            assert await run('game', game['name'], '-n'), err.getvalue()
        assert game['id'] == json.loads(out.getvalue())['id']


@pytest.mark.asyncio
async def test_sched():
    async with mockserver.server() as server:
        with stdout() as out, stderr() as err:
            assert await run('sched'), err.getvalue()
        assert not out.getvalue()

        sim_id = server.create_simulator('sim', '1')
        async with api.api() as egta:
            await egta.create_generic_scheduler(
                sim_id, 'sched', True, 1, 2, 1, 1)

        with stdout() as out, stderr() as err:
            assert await run('sched'), err.getvalue()
        sched = json.loads(out.getvalue())

        with stderr() as err:
            assert await run('sched', str(sched['id'])), err.getvalue()

        with stderr() as err:
            assert await run('sched', str(sched['id']), '-r'), err.getvalue()

        with stdout() as out, stderr() as err:
            assert await run('sched', sched['name'], '-n'), err.getvalue()
        assert sched['id'] == json.loads(out.getvalue())['id']


@pytest.mark.asyncio
async def test_sims():
    async with mockserver.server() as server:
        with stdout() as out, stderr() as err:
            assert await run('sched'), err.getvalue()
        assert not out.getvalue()

        sim_id = server.create_simulator('sim', '1')
        async with api.api() as egta:
            sim = await egta.get_simulator(sim_id)
            await sim.add_strategies({'r': ['s0', 's1']})
            sched = await egta.create_generic_scheduler(
                sim_id, 'sched', True, 1, 2, 1, 1)
            await sched.add_role('r', 2)
            await sched.add_profile('r: 1 s0, 1 s1', 1)
            await sched.add_profile('r: 2 s0', 2)

        with stdout() as out, stderr() as err:
            assert await run('sims'), err.getvalue()
        sims = [json.loads(line) for line in out.getvalue()[:-1].split('\n')]
        assert len(sims) == 3
        with stderr() as err:
            assert await run('sims', str(sims[0]['folder'])), err.getvalue()
