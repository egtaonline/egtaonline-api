"""Tests for mock server and api"""
import asyncio
import itertools
import json

import jsonschema
import pytest
import requests

from egtaonline import api
from egtaonline import mockserver


def validate_object(obj, obj_schema):
    """Validate a required object schema"""
    jsonschema.validate(obj, {
        'type': 'object',
        'properties': obj_schema,
        'required': list(obj_schema),
    })


def is_sorted(gen, *, reverse=False):
    """Test if a generator is sorted"""
    ait, bit = itertools.tee(gen)
    next(bit, None)
    if reverse:
        ait, bit = bit, ait
    return all(a <= b for a, b in zip(ait, bit))


# TODO in python3.6 we may be able to use async fixtures, but async_generator
# didn't work with teardowns


# TODO This is a cheap way around the lack of async generators in python3.5
async def agather(aiter):
    """Gather an async iterator into a list"""
    lst = []
    async for elem in aiter:
        lst.append(elem)
    return lst


async def create_simulator(server, egta, name, version):
    """Create a simulator that's semi configured"""
    sim = await egta.get_simulator(server.create_simulator(
        name, version, conf={'key': 'value'}))
    await sim.add_strategies({
        'a': ['1', '2', '3', '4'],
        'b': ['5', '6', '7'],
    })
    return sim


async def sched_complete(sched, sleep=0.001):
    """Wait for scheduler to complete"""
    while (await sched.get_info())['active'] and not all(  # pragma: no branch
            p['requirement'] <= p['current_count'] for p
            in (await sched.get_requirements())['scheduling_requirements']):
        await asyncio.sleep(sleep)  # pragma: no cover


@pytest.mark.asyncio
async def test_get_simulators():
    """Test getting simulators"""
    async with mockserver.server() as server, \
            api.api(num_tries=3, retry_delay=0.5) as egta:
        sim1 = server.create_simulator('foo', '1')
        sim2 = server.create_simulator('bar', '1')
        sim3 = server.create_simulator('bar', '2')

        assert sum(1 for _ in await egta.get_simulators()) == 3
        assert {0, 1, 2} == {s['id'] for s in await egta.get_simulators()}

        sim = await egta.get_simulator(0)
        assert sim['id'] == sim1
        sim = await egta.get_simulator_fullname('foo-1')
        assert sim['id'] == sim1
        sim = await egta.get_simulator(2)
        assert sim['id'] == sim3
        sim = await egta.get_simulator_fullname('bar-1')
        assert sim['id'] == sim2
        sim = await egta.get_simulator_fullname('bar-2')
        assert sim['id'] == sim3

        with pytest.raises(requests.exceptions.HTTPError):
            await egta.get_simulator(3)
        with pytest.raises(AssertionError):
            await egta.get_simulator_fullname('baz')


@pytest.mark.asyncio
async def test_simulator():
    """Test simulator api"""
    async with mockserver.server() as server, \
            api.api(num_tries=3, retry_delay=0.5) as egta:
        sim = await create_simulator(server, egta, 'sim', '1')
        info = await sim.get_info()
        validate_object(info, {
            'configuration': {'type': 'object'},
            'created_at': {'type': 'string'},
            'email': {'type': 'string'},
            'id': {'type': 'integer'},
            'name': {'type': 'string'},
            'role_configuration': {'type': 'object'},
            'source': {'type': 'object'},
            'updated_at': {'type': 'string'},
            'url': {'type': 'string'},
            'version': {'type': 'string'},
        })
        role_conf = {'a': ['1', '2', '3', '4'], 'b': ['5', '6', '7']}
        assert info['role_configuration'] == role_conf

        await asyncio.sleep(1)
        await sim.remove_strategy('a', '3')
        new_role_conf = {'a': ['1', '2', '4'], 'b': ['5', '6', '7']}
        new_info = await sim.get_info()
        assert new_info['role_configuration'] == new_role_conf
        assert new_info['updated_at'] != info['updated_at']
        assert info['role_configuration'] == role_conf

        await sim.remove_role('b')
        new_role_conf = {'a': ['1', '2', '4']}
        new_info = await sim.get_info()
        assert new_info['role_configuration'] == new_role_conf
        # Stale object didn't update
        assert info['role_configuration'] == role_conf

        # Add existing strategy
        await sim.add_strategy('a', '1')
        new_info = await sim.get_info()
        assert new_info['role_configuration'] == new_role_conf

        await sim.add_strategy('a', '2')
        await sim.add_strategy('a', '3')
        new_role_conf = {'a': ['1', '2', '3', '4']}
        new_info = await sim.get_info()
        assert new_info['role_configuration'] == new_role_conf

        await sim.remove_strategies({'a': ['4', '5', '4']})
        new_role_conf = {'a': ['1', '2', '3']}
        new_info = await sim.get_info()
        assert new_info['role_configuration'] == new_role_conf

        await sim.remove_role('c')
        with pytest.raises(KeyError):
            await sim.add_strategy('c', '8')
        # Shouldn't raise exception, because removals never do
        await sim.remove_strategies({'c': ['8']})


@pytest.mark.asyncio
async def test_get_schedulers():
    """Test getting schedulers"""
    async with mockserver.server() as server, \
            api.api(num_tries=3, retry_delay=0.5) as egta:
        sim = await create_simulator(server, egta, 'sim', '1')
        await sim.create_generic_scheduler('1', False, 0, 10, 0, 0)
        sched2 = await egta.create_generic_scheduler(
            sim['id'], '2', False, 0, 10, 0, 0)
        sched3 = await sim.create_generic_scheduler('3', False, 0, 10, 0, 0)
        await sim.create_generic_scheduler('4', False, 0, 10, 0, 0)
        await sim.create_generic_scheduler('5', False, 0, 10, 0, 0)

        with pytest.raises(requests.exceptions.HTTPError):
            await sim.create_generic_scheduler('4', False, 0, 10, 0, 0)

        sched = await egta.get_scheduler(2)
        assert sched['id'] == sched3['id']
        sched = await egta.get_scheduler_name('3')
        assert sched['id'] == sched3['id']

        assert sum(1 for _ in await egta.get_generic_schedulers()) == 5
        assert {0, 1, 2, 3, 4} == {
            s['id'] for s in await egta.get_generic_schedulers()}

        await sched2.destroy_scheduler()
        await sched3.destroy_scheduler()

        assert sum(1 for _ in await egta.get_generic_schedulers()) == 3
        assert {0, 3, 4} == {
            s['id'] for s in await egta.get_generic_schedulers()}

        with pytest.raises(requests.exceptions.HTTPError):
            await egta.get_scheduler(5)
        with pytest.raises(requests.exceptions.HTTPError):
            await egta.get_scheduler(2)
        with pytest.raises(AssertionError):
            await egta.get_scheduler_name('3')


@pytest.mark.asyncio
async def test_scheduler():
    """Test scheduler api"""
    async with mockserver.server() as server, \
            api.api(num_tries=3, retry_delay=0.5) as egta:
        sim = await create_simulator(server, egta, 'sim', '1')
        sched = await sim.create_generic_scheduler('sched', True, 0, 10, 0, 0)
        validate_object(sched, {
            'active': {'type': 'boolean'},
            'created_at': {'type': 'string'},
            'default_observation_requirement': {'type': 'integer'},
            'id': {'type': 'integer'},
            'name': {'type': 'string'},
            'nodes': {'type': 'integer'},
            'observations_per_simulation': {'type': 'integer'},
            'process_memory': {'type': 'integer'},
            'simulator_instance_id': {'type': 'integer'},
            'size': {'type': 'integer'},
            'time_per_observation': {'type': 'integer'},
            'updated_at': {'type': 'string'},
        })

        info = await sched.get_info()
        validate_object(info, {
            'active': {'type': 'boolean'},
            'created_at': {'type': 'string'},
            'default_observation_requirement': {'type': 'integer'},
            'id': {'type': 'integer'},
            'name': {'type': 'string'},
            'nodes': {'type': 'integer'},
            'observations_per_simulation': {'type': 'integer'},
            'process_memory': {'type': 'integer'},
            'simulator_instance_id': {'type': 'integer'},
            'size': {'type': 'integer'},
            'time_per_observation': {'type': 'integer'},
            'updated_at': {'type': 'string'},
        })

        validate_object((await sched.get_requirements()), {
            'active': {'type': 'boolean'},
            'configuration': {'type': 'array'},
            'default_observation_requirement': {'type': 'integer'},
            'id': {'type': 'integer'},
            'name': {'type': 'string'},
            'nodes': {'type': 'integer'},
            'observations_per_simulation': {'type': 'integer'},
            'process_memory': {'type': 'integer'},
            'scheduling_requirements': {'type': 'array'},
            'simulator_id': {'type': 'integer'},
            'size': {'type': 'integer'},
            'time_per_observation': {'type': 'integer'},
            'type': {'type': 'string'},
            'url': {'type': 'string'},
        })

        await sched.deactivate()
        assert not (await sched.get_info())['active']
        # stale info invalid
        assert info['active']

        await sched.activate()
        assert (await sched.get_info())['active']

        await sched.update(process_memory=1)
        assert (await sched.get_info())['process_memory'] == 1

        await sched.add_roles({'a': 8})
        with pytest.raises(requests.exceptions.HTTPError):
            await sched.add_role('a', 1)
        with pytest.raises(requests.exceptions.HTTPError):
            await sched.add_role('c', 1)
        with pytest.raises(requests.exceptions.HTTPError):
            await sched.add_role('b', 3)
        await sched.add_role('b', 2)

        await sched.remove_role('b')
        await sched.remove_roles(['b', 'c'])


@pytest.mark.asyncio
async def test_profiles(): # pylint: disable=too-many-statements,too-many-locals
    """Test profile api"""
    async with mockserver.server() as server, \
            api.api(num_tries=3, retry_delay=0.5) as egta:
        sim = await create_simulator(server, egta, 'sim', '1')
        sched1 = await sim.create_generic_scheduler('sched', True, 0, 10, 0, 0)
        await sched1.add_roles({'a': 8, 'b': 2})

        assignment = 'a: 8 1; b: 1 5, 1 7'
        symgrp = [{'role': 'a', 'strategy': '1', 'count': 8},
                  {'role': 'b', 'strategy': '5', 'count': 1},
                  {'role': 'b', 'strategy': '7', 'count': 1}]
        assert assignment == mockserver.symgrps_to_assignment(symgrp)
        prof1 = await sched1.add_profile(assignment, 0)
        validate_object(prof1, {
            'assignment': {'type': 'string'},
            'created_at': {'type': 'string'},
            'id': {'type': 'integer'},
            'observations_count': {'type': 'integer'},
            'role_configuration': {'type': 'object'},
            'simulator_instance_id': {'type': 'integer'},
            'size': {'type': 'integer'},
            'updated_at': {'type': 'string'},
        })
        assert (await prof1.get_structure())['observations_count'] == 0
        for grp in (await prof1.get_summary())['symmetry_groups']:
            assert grp['payoff'] is None
            assert grp['payoff_sd'] is None
        assert not (await prof1.get_observations())['observations']
        assert not (await prof1.get_full_data())['observations']

        prof0 = await egta.get_profile(prof1['id'])
        assert prof1['id'] == prof0['id']
        assert prof1['id'] == (await sched1.add_profile(symgrp, 0))['id']

        await sched1.remove_profile(prof1['id'])
        await sched1.add_profile(assignment, 3)
        await sched_complete(sched1)
        reqs = (await sched1.get_requirements())['scheduling_requirements']
        assert len(reqs) == 1
        assert reqs[0]['current_count'] == 3
        assert reqs[0]['requirement'] == 3
        assert reqs[0]['id'] == prof1['id']

        struct = await prof1.get_structure()
        validate_object(struct, {
            'assignment': {'type': 'string'},
            'created_at': {'type': 'string'},
            'id': {'type': 'integer'},
            'observations_count': {'type': 'integer'},
            'role_configuration': {'type': 'object'},
            'simulator_instance_id': {'type': 'integer'},
            'size': {'type': 'integer'},
            'updated_at': {'type': 'string'},
        })
        assert struct['assignment'] == assignment
        assert struct['observations_count'] == 3
        assert struct['size'] == 10

        summ = await prof1.get_summary()
        validate_object(summ, {
            'id': {'type': 'integer'},
            'observations_count': {'type': 'integer'},
            'simulator_instance_id': {'type': 'integer'},
            'symmetry_groups': {'type': 'array'},
        })
        assert summ['observations_count'] == 3
        assert len(summ['symmetry_groups']) == 3

        obs = await prof1.get_observations()
        validate_object(obs, {
            'id': {'type': 'integer'},
            'simulator_instance_id': {'type': 'integer'},
            'symmetry_groups': {'type': 'array'},
            'observations': {'type': 'array'},
        })
        assert len(obs['symmetry_groups']) == 3
        assert len(obs['observations']) == 3
        assert all(len(o['symmetry_groups']) == 3
                   for o in obs['observations'])

        full = await prof1.get_full_data()
        validate_object(full, {
            'id': {'type': 'integer'},
            'simulator_instance_id': {'type': 'integer'},
            'symmetry_groups': {'type': 'array'},
            'observations': {'type': 'array'},
        })
        assert len(full['symmetry_groups']) == 3
        assert len(full['observations']) == 3
        assert all(len(o['players']) == 10 for o in full['observations'])

        sched2 = await sim.create_generic_scheduler(
            'sched2', True, 0, 10, 0, 0)
        await sched2.add_roles({'a': 8, 'b': 2})
        prof2 = await sched2.add_profile(assignment, 5)
        await sched_complete(sched2)

        assert prof2['id'] == prof1['id']
        assert (await prof2.get_structure())['observations_count'] == 5
        assert (await prof1.get_structure())['observations_count'] == 5

        reqs = (await sched2.get_requirements())['scheduling_requirements']
        assert len(reqs) == 1
        assert reqs[0]['current_count'] == 5
        assert reqs[0]['requirement'] == 5

        reqs = (await sched1.get_requirements())['scheduling_requirements']
        assert len(reqs) == 1
        assert reqs[0]['current_count'] == 5
        assert reqs[0]['requirement'] == 3

        await sched1.remove_profile(prof1['id'])
        assert not (await sched1.get_requirements())['scheduling_requirements']

        updated_time = (await sched1.get_info())['updated_at']
        await asyncio.sleep(1)
        await sched1.remove_profile(prof1['id'])
        assert (await sched1.get_info())['updated_at'] == updated_time

        assert prof1['id'] == (await sched1.add_profile(assignment, 1))['id']
        reqs = (await sched1.get_requirements())['scheduling_requirements']
        assert len(reqs) == 1
        assert reqs[0]['current_count'] == 5
        assert reqs[0]['requirement'] == 1

        # Test delayed scheduling
        await sched1.deactivate()
        await sched1.remove_profile(prof1['id'])
        await sched1.add_profile(assignment, 9)
        await sched_complete(sched1)
        assert (await prof1.get_structure())['observations_count'] == 5
        reqs = (await sched1.get_requirements())['scheduling_requirements']
        assert len(reqs) == 1
        assert reqs[0]['current_count'] == 5
        assert reqs[0]['requirement'] == 9
        await sched1.activate()
        await sched_complete(sched1)
        assert (await prof1.get_structure())['observations_count'] == 9

        await sched1.remove_all_profiles()
        assert not (await sched1.get_requirements())['scheduling_requirements']
        await sched1.remove_profile(10**10)

        assert (await sched2.get_requirements())['scheduling_requirements']
        await sched2.remove_profile(prof2['id'])
        assert not (await sched2.get_requirements())['scheduling_requirements']


@pytest.mark.asyncio
async def test_delayed_profiles():
    """Test mock server with delayed profile scheduling"""
    async with mockserver.server() as server, api.api() as egta:
        sim = await egta.get_simulator(
            server.create_simulator('sim', '1', delay_dist=lambda: 0.5))
        await sim.add_strategies({'1': ['a'], '2': ['b', 'c']})
        sched = await sim.create_generic_scheduler('sched', True, 0, 10, 0, 0)
        await sched.add_roles({'1': 8, '2': 2})

        prof = await sched.add_profile('1: 8 a; 2: 1 b, 1 c', 3)
        reqs = (await sched.get_requirements())['scheduling_requirements']
        assert len(reqs) == 1
        assert reqs[0]['current_count'] == 0
        assert reqs[0]['requirement'] == 3
        assert reqs[0]['id'] == prof['id']

        count = 0
        async for sim in egta.get_simulations():
            assert sim['state'] == 'running'
            count += 1
        assert count == 3

        await asyncio.sleep(0.5)
        await sched_complete(sched)
        reqs = (await sched.get_requirements())['scheduling_requirements']
        assert len(reqs) == 1
        assert reqs[0]['current_count'] == 3
        assert reqs[0]['requirement'] == 3
        assert reqs[0]['id'] == prof['id']

        count = 0
        async for sim in egta.get_simulations():
            assert sim['state'] == 'complete'
            count += 1
        assert count == 3

    # Test that extra sims get killed
    async with mockserver.server() as server, api.api() as egta:
        sim = await egta.get_simulator(
            server.create_simulator('sim', '1', delay_dist=lambda: 10))
        await sim.add_strategies({'1': ['a'], '2': ['b', 'c']})
        sched = await sim.create_generic_scheduler('sched', True, 0, 10, 0, 0)
        await sched.add_roles({'1': 8, '2': 2})
        # Need two profiles here because the other will be in the queue
        await sched.add_profile('1: 8 a; 2: 1 b, 1 c', 1)
        await sched.add_profile('1: 8 a; 2: 2 b', 1)


@pytest.mark.asyncio
async def test_missing_profile():
    """Test getting missing profile"""
    async with mockserver.server(), \
            api.api(num_tries=3, retry_delay=0.5) as egta:
        with pytest.raises(requests.exceptions.HTTPError):
            await egta.get_profile(0)


@pytest.mark.asyncio
async def test_get_games():
    """Test getting games"""
    async with mockserver.server() as server, \
            api.api(num_tries=3, retry_delay=0.5) as egta:
        sim = await create_simulator(server, egta, 'sim', '1')
        await sim.create_game('a', 5)
        game2 = await egta.create_game(sim['id'], 'b', 6)
        game3 = await sim.create_game('c', 3)

        with pytest.raises(requests.exceptions.HTTPError):
            await sim.create_game('b', 3)

        assert (await egta.get_game(1))['id'] == game2['id']
        assert (await egta.get_game_name('c'))['id'] == game3['id']

        assert sum(1 for _ in await egta.get_games()) == 3
        assert {0, 1, 2} == {g['id'] for g in await egta.get_games()}

        await game2.destroy_game()

        assert sum(1 for _ in await egta.get_games()) == 2
        assert {0, 2} == {g['id'] for g in await egta.get_games()}

        with pytest.raises(requests.exceptions.HTTPError):
            await egta.get_game(3)
        with pytest.raises(requests.exceptions.HTTPError):
            await egta.get_game(1)
        with pytest.raises(AssertionError):
            await egta.get_game_name('b')


@pytest.mark.asyncio
async def test_game():
    """Test game mocks"""
    async with mockserver.server() as server, \
            api.api(num_tries=3, retry_delay=0.5) as egta:
        sim = await create_simulator(server, egta, 'sim', '1')
        sched = await sim.create_generic_scheduler(
            'sched', True, 0, 4, 0, 0, configuration={'k': 'v'})
        await sched.add_roles({'a': 2, 'b': 2})

        game = await sched.create_game()
        await game.add_symgroups([
            ('a', 2, ['1']), ('b', 2, ['5', '6'])])

        prof = await sched.add_profile('a: 2 1; b: 1 5, 1 6', 0)
        await sched_complete(sched)
        reqs = await sched.get_requirements()
        assert len(reqs['scheduling_requirements']) == 1
        assert not (await game.get_summary())['profiles']
        assert not (await game.get_observations())['profiles']
        assert not (await game.get_full_data())['profiles']

        await sched.remove_profile(prof['id'])
        await sched.add_profile(prof['assignment'], 1)
        await sched.add_profile('a: 2 1; b: 2 5', 2)
        await sched_complete(sched)

        size_counts = {}
        for prof in (await game.get_summary())['profiles']:
            counts = prof['observations_count']
            size_counts[counts] = size_counts.get(counts, 0) + 1
        assert size_counts == {1: 1, 2: 1}

        size_counts = {}
        for prof in (await game.get_observations())['profiles']:
            counts = len(prof['observations'])
            size_counts[counts] = size_counts.get(counts, 0) + 1
        assert size_counts == {1: 1, 2: 1}

        size_counts = {}
        for prof in (await game.get_full_data())['profiles']:
            for obs in prof['observations']:
                assert len(obs['players']) == 4
            counts = len(prof['observations'])
            size_counts[counts] = size_counts.get(counts, 0) + 1
        assert size_counts == {1: 1, 2: 1}

        await game.remove_strategy('b', '6')
        assert len((await game.get_summary())['profiles']) == 1
        assert len((await game.get_observations())['profiles']) == 1
        assert len((await game.get_full_data())['profiles']) == 1

        await game.remove_strategy('a', '4')
        await game.add_strategies({'a': ['2', '3']})
        await game.remove_strategies({'a': ['1', '3']})
        await game.remove_roles(['b'])

        updated_time = (await game.get_structure())['updated_at']
        await asyncio.sleep(1)
        await game.remove_role('b')
        assert (await game.get_structure())['updated_at'] == updated_time

        await game.add_roles({'b': 2})
        sched = await game.create_generic_scheduler('scheder', False, 1, 1, 1)
        assert sched['size'] == game['size']


@pytest.mark.asyncio
async def test_canon_game(): # pylint: disable=too-many-locals
    """Test that canon game creates proper games"""
    async with mockserver.server() as server, \
            api.api(num_tries=3, retry_delay=0.5) as egta:
        sim = await create_simulator(server, egta, 'sim', '1')
        sim2 = await create_simulator(server, egta, 'sim', '2')

        symgrps = [('a', 2, ['1']), ('b', 2, ['5', '6'])]
        conf = {'key': 'val'}
        game1 = await sim.get_canon_game(symgrps, conf)
        summ = await game1.get_summary()
        assert conf == dict(summ['configuration'])

        def unpack(name, count, strategies):
            """Helper to unpack dictionaries"""
            return name, count, strategies

        symgrp_dict = {}
        for role_info in summ['roles']:
            role, count, strategies = unpack(**role_info)
            symgrp_dict[role] = (role, count, set(strategies))
        for role, count, strats in symgrps:
            _, cnt, strt = symgrp_dict[role]
            assert cnt == count
            assert strt == set(strats)

        game2 = await egta.get_canon_game(
            sim['id'], symgrps, {'key': 'diff'})
        assert game1['id'] != game2['id']

        game3 = await egta.get_canon_game(
            sim['id'], [('a', 2, ['1']), ('b', 2, ['5', '7'])], conf)
        assert game1['id'] != game3['id']

        game4 = await egta.get_canon_game(
            sim['id'], [('a', 2, ['1']), ('b', 1, ['5', '6'])], conf)
        assert game1['id'] != game4['id']

        game5 = await egta.get_canon_game(sim2['id'], symgrps, conf)
        assert game1['id'] != game5['id']

        game6 = await egta.get_canon_game(sim['id'], symgrps, conf)
        assert game1['id'] == game6['id']


def _raise(ex):
    """Raise an exception"""
    raise ex


@pytest.mark.asyncio
async def test_large_game_failsafes():
    """Test that large games fallback to gathering profile data"""
    async with mockserver.server() as server, \
            api.api(num_tries=3, retry_delay=0.5) as egta:
        sim = await create_simulator(server, egta, 'sim', '1')
        error = requests.exceptions.HTTPError(
            '500 Server Error: Game too large!')
        sched = await sim.create_generic_scheduler(
            'sched', True, 0, 4, 0, 0, configuration={'k': 'v'})
        await sched.add_roles({'a': 2, 'b': 2})

        game = await sched.create_game()
        await game.add_symgroups([
            ('a', 2, ['1']), ('b', 2, ['5', '6'])])

        await sched.add_profile('a: 2 1; b: 1 5, 1 6', 1)
        await sched.add_profile('a: 2 1; b: 2 5', 2)
        await sched_complete(sched)

        base = await game.get_observations()
        server.custom_response(lambda: _raise(error))
        alternate = await game.get_observations()
        assert base == alternate
        size_counts = {}
        for prof in alternate['profiles']:
            counts = len(prof['observations'])
            size_counts[counts] = size_counts.get(counts, 0) + 1
        assert size_counts == {1: 1, 2: 1}

        base = await game.get_full_data()
        server.custom_response(lambda: _raise(error))
        alternate = await game.get_full_data()
        assert base == alternate
        size_counts = {}
        for prof in alternate['profiles']:
            for obs in prof['observations']:
                assert len(obs['players']) == 4
            counts = len(prof['observations'])
            size_counts[counts] = size_counts.get(counts, 0) + 1
        assert size_counts == {1: 1, 2: 1}


@pytest.mark.asyncio
async def test_profile_json_error():
    """Test invalid profile json triggers retry"""
    async with mockserver.server() as server, \
            api.api(num_tries=3, retry_delay=0.5) as egta:
        sim = await create_simulator(server, egta, 'sim', '1')
        sched = await sim.create_generic_scheduler('sched', True, 0, 4, 0, 0)
        await sched.add_roles({'a': 2, 'b': 2})

        game = await sched.create_game()
        await game.add_symgroups([
            ('a', 2, ['1']), ('b', 2, ['5', '6'])])

        prof = await sched.add_profile('a: 2 1; b: 2 5', 2)
        await sched_complete(sched)

        server.custom_response(lambda: '', 2)
        summ = await prof.get_summary()
        assert 'id' in summ
        assert 'observations_count' in summ
        assert 'symmetry_groups' in summ

        server.custom_response(lambda: '', 3)
        with pytest.raises(json.decoder.JSONDecodeError):
            await prof.get_summary()

        server.custom_response(lambda: '{}', 3)
        with pytest.raises(jsonschema.ValidationError):
            await prof.get_summary()


@pytest.mark.asyncio
async def test_game_json_error():
    """Test returning invalid json in games triggers retry"""
    async with mockserver.server() as server, \
            api.api(num_tries=3, retry_delay=0.5) as egta:
        sim = await create_simulator(server, egta, 'sim', '1')
        sched = await sim.create_generic_scheduler('sched', True, 0, 4, 0, 0)
        await sched.add_roles({'a': 2, 'b': 2})

        game = await sched.create_game()
        await game.add_symgroups([
            ('a', 2, ['1']), ('b', 2, ['5', '6'])])

        await sched.add_profile('a: 2 1; b: 1 5, 1 6', 1)
        await sched.add_profile('a: 2 1; b: 2 5', 2)
        await sched_complete(sched)

        server.custom_response(lambda: '', 2)
        summ = await game.get_summary()
        size_counts = {}
        for prof in summ['profiles']:
            counts = prof['observations_count']
            size_counts[counts] = size_counts.get(counts, 0) + 1
        assert size_counts == {1: 1, 2: 1}

        server.custom_response(lambda: '', 3)
        with pytest.raises(json.decoder.JSONDecodeError):
            await game.get_summary()

        server.custom_response(lambda: '{}', 3)
        with pytest.raises(jsonschema.ValidationError):
            await game.get_summary()


@pytest.mark.asyncio
async def test_get_simulations():
    """Test getting simulations"""
    async with mockserver.server() as server, \
            api.api(num_tries=3, retry_delay=0.5) as egta:
        assert not await agather(egta.get_simulations())
        sim1 = await create_simulator(server, egta, 'sim', '1')
        sched1 = await sim1.create_generic_scheduler(
            'sched1', True, 0, 4, 0, 0)
        await sched1.add_roles({'a': 2, 'b': 2})
        await sched1.add_profile('a: 2 1; b: 1 6, 1 7', 2)

        assert len(await agather(egta.get_simulations())) == 2
        simul = next(iter(await agather(egta.get_simulations())))
        validate_object(simul, {
            'folder': {'type': 'integer'},
            'job': {'type': 'number'},
            'profile': {'type': 'string'},
            'simulator': {'type': 'string'},
            'state': {'type': 'string'},
        })
        validate_object(await egta.get_simulation(simul['folder']), {
            'error_message': {'type': 'string'},
            'folder_number': {'type': 'integer'},
            'job': {'type': 'string'},
            'profile': {'type': 'string'},
            'simulator_fullname': {'type': 'string'},
            'size': {'type': 'integer'},
            'state': {'type': 'string'},
        })

        sim2 = await create_simulator(server, egta, 'sim', '2')
        sched2 = await sim2.create_generic_scheduler(
            'sched2', True, 0, 5, 0, 0)
        await sched2.add_roles({'a': 2, 'b': 3})
        await sched2.add_profile('a: 2 1; b: 1 5, 2 7', 3)
        assert len(await agather(egta.get_simulations())) == 5

        # Test simulations
        assert is_sorted(  # pragma: no branch
            (f['simulator'] for f
             in await agather(egta.get_simulations(column='simulator'))),
            reverse=True)
        assert is_sorted(  # pragma: no branch
            (f['folder'] for f
             in await agather(egta.get_simulations(column='folder'))),
            reverse=True)
        assert is_sorted(  # pragma: no branch
            (f['profile'] for f
             in await agather(egta.get_simulations(column='profile'))),
            reverse=True)
        assert is_sorted(  # pragma: no branch
            (f['state'] for f
             in await agather(egta.get_simulations(column='state'))),
            reverse=True)

        assert is_sorted(  # pragma: no branch
            f['simulator'] for f
            in await agather(egta.get_simulations(
                asc=True, column='simulator')))
        assert is_sorted(  # pragma: no branch
            f['folder'] for f
            in await agather(egta.get_simulations(asc=True, column='folder')))
        assert is_sorted(  # pragma: no branch
            f['profile'] for f
            in await agather(egta.get_simulations(asc=True, column='profile')))
        assert is_sorted(  # pragma: no branch
            f['state'] for f
            in await agather(egta.get_simulations(asc=True, column='state')))

        assert not await agather(egta.get_simulations(page_start=2))
        await sched2.add_profile('a: 2 1; b: 1 5, 2 6', 21)
        assert len(await agather(egta.get_simulations(page_start=2))) == 1


@pytest.mark.asyncio
async def test_exception_open():
    """Test that exceptions are even thrown on open"""
    async with mockserver.server() as server:
        server.custom_response(lambda: _raise(TimeoutError))
        with pytest.raises(TimeoutError):
            async with api.api():
                pass  # pragma: no cover


@pytest.mark.asyncio
async def test_exceptions():
    """Test that exceptions can be properly set"""
    async with mockserver.server() as server, \
            api.api(num_tries=3, retry_delay=0.5) as egta:
        sim = await create_simulator(server, egta, 'sim', '1')
        await sim.add_strategies({'role': ['strategy']})
        sched = await sim.create_generic_scheduler('sched', True, 0, 1, 0, 0)
        await sched.add_role('role', 1)
        prof = await sched.add_profile('role: 1 strategy', 1)
        game = await sched.create_game('game')
        await game.add_symgroup('role', 1, ['strategy'])

        server.custom_response(lambda: _raise(TimeoutError), 11)

        # Creations fail
        with pytest.raises(TimeoutError):
            await sim.create_generic_scheduler('sched_2', False, 0, 0, 0, 0)
        with pytest.raises(TimeoutError):
            await sched.create_game()

        # Infos fail
        with pytest.raises(TimeoutError):
            await sim.get_info()
        with pytest.raises(TimeoutError):
            await sched.get_info()
        with pytest.raises(TimeoutError):
            await game.get_structure()
        with pytest.raises(TimeoutError):
            await prof.get_structure()

        # Mutates fail
        with pytest.raises(TimeoutError):
            await sim.add_role('r')
        with pytest.raises(TimeoutError):
            await sim.add_strategy('role', 's')
        with pytest.raises(TimeoutError):
            await sched.add_role('r', 1)
        with pytest.raises(TimeoutError):
            await game.add_role('r', 1)
        with pytest.raises(TimeoutError):
            await game.add_strategy('role', 's')

        # Succeed after done
        assert sim['id'] == (await sim.get_info())['id']


@pytest.mark.asyncio
async def test_threading():
    """Test that no errors arise when multi-threading"""
    async with mockserver.server() as server, \
            api.api(num_tries=3, retry_delay=0.5) as egta:
        sim = await create_simulator(server, egta, 'sim', '1')
        await asyncio.gather(*[
            sim.add_strategies({'r{:d}'.format(i): ['s{:d}'.format(i)]})
            for i in range(10)])
