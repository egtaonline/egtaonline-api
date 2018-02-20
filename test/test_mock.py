import itertools
import json
import time

import pytest
import requests

from egtaonline import api
from egtaonline import mockserver


def assert_structure(dic, struct):
    assert dic.keys() == struct.keys()
    for key, typ in struct.items():
        assert isinstance(dic[key], typ)


def is_sorted(gen, *, reverse=False):
    ai, bi = itertools.tee(gen)
    next(bi, None)
    if reverse:
        ai, bi = bi, ai
    return all(a <= b for a, b in zip(ai, bi))


def create_simulator(server, egta, name, version):
    sim = egta.get_simulator(server.create_simulator(
        name, version, conf={'key': 'value'}))
    sim.add_role('a')
    sim.add_strategy('a', '1')
    sim.add_strategy('a', '2')
    sim.add_strategy('a', '3')
    sim.add_strategy('a', '4')
    sim.add_role('b')
    sim.add_strategy('b', '5')
    sim.add_strategy('b', '6')
    sim.add_strategy('b', '7')
    return sim


def sched_complete(sched, sleep=0.001):
    while sched.get_info()['active'] and not all(
            p['requirement'] <= p['current_count'] for p
            in sched.get_requirements()['scheduling_requirements']):
        time.sleep(sleep)


def test_get_simulators():
    with mockserver.Server() as server, api.EgtaOnlineApi() as egta:
        sim1 = server.create_simulator('foo', '1')
        sim2 = server.create_simulator('bar', '1')
        sim3 = server.create_simulator('bar', '2')

        assert 3 == sum(1 for _ in egta.get_simulators())
        assert {0, 1, 2} == {s['id'] for s in egta.get_simulators()}

        assert egta.get_simulator(0)['id'] == sim1
        assert egta.get_simulator('foo')['id'] == sim1
        assert egta.get_simulator('foo', '1')['id'] == sim1
        assert egta.get_simulator(2)['id'] == sim3
        assert egta.get_simulator('bar', '1')['id'] == sim2
        assert egta.get_simulator('bar', '2')['id'] == sim3

        sim = egta.get_simulator(3)
        assert sim['id'] == 3
        with pytest.raises(requests.exceptions.HTTPError):
            sim.get_info()
        with pytest.raises(ValueError):
            egta.get_simulator('baz')
        with pytest.raises(ValueError):
            egta.get_simulator('bar')


def test_simulator():
    with mockserver.Server() as server, api.EgtaOnlineApi() as egta:
        sim = create_simulator(server, egta, 'sim', '1')

        info = sim.get_info()
        assert_structure(info, {
            'configuration': dict,
            'created_at': str,
            'email': str,
            'id': int,
            'name': str,
            'role_configuration': dict,
            'source': dict,
            'updated_at': str,
            'url': str,
            'version': str,
        })
        role_conf = {'a': ['1', '2', '3', '4'], 'b': ['5', '6', '7']}
        assert info['role_configuration'] == role_conf

        time.sleep(1)
        sim.remove_strategy('a', '3')
        new_role_conf = {'a': ['1', '2', '4'], 'b': ['5', '6', '7']}
        assert sim.get_info()['role_configuration'] == new_role_conf
        assert sim.get_info()['updated_at'] != info['updated_at']
        assert info['role_configuration'] == role_conf

        sim.remove_role('b')
        new_role_conf = {'a': ['1', '2', '4']}
        assert sim.get_info()['role_configuration'] == new_role_conf
        # Stale object didn't update
        assert info['role_configuration'] == role_conf

        # Add existing strategy
        sim.add_strategy('a', '1')
        assert sim.get_info()['role_configuration'] == new_role_conf

        sim.add_dict({'a': ['2', '3']})
        new_role_conf = {'a': ['1', '2', '3', '4']}
        assert sim.get_info()['role_configuration'] == new_role_conf

        sim.remove_dict({'a': ['4', '5', '4']})
        new_role_conf = {'a': ['1', '2', '3']}
        assert sim.get_info()['role_configuration'] == new_role_conf

        sim.remove_role('c')
        with pytest.raises(KeyError):
            sim.add_strategy('c', '8')
        # Shouldn't raise exception, because removals never do
        sim.remove_dict({'c': ['8']})


def test_get_schedulers():
    with mockserver.Server() as server, api.EgtaOnlineApi() as egta:
        sim = create_simulator(server, egta, 'sim', '1')
        sim.create_generic_scheduler('1', False, 0, 10, 0, 0)
        sched2 = sim.create_generic_scheduler('2', False, 0, 10, 0, 0)
        sched3 = sim.create_generic_scheduler('3', False, 0, 10, 0, 0)
        sim.create_generic_scheduler('4', False, 0, 10, 0, 0)
        sim.create_generic_scheduler('5', False, 0, 10, 0, 0)

        with pytest.raises(requests.exceptions.HTTPError):
            sim.create_generic_scheduler('4', False, 0, 10, 0, 0)

        assert egta.get_scheduler(2)['id'] == sched3['id']
        assert egta.get_scheduler('3')['id'] == sched3['id']

        assert 5 == sum(1 for _ in egta.get_generic_schedulers())
        assert {0, 1, 2, 3, 4} == {s['id'] for s
                                   in egta.get_generic_schedulers()}

        sched2.destroy_scheduler()
        sched3.destroy_scheduler()

        assert 3 == sum(1 for _ in egta.get_generic_schedulers())
        assert {0, 3, 4} == {s['id'] for s in egta.get_generic_schedulers()}

        with pytest.raises(requests.exceptions.HTTPError):
            egta.get_scheduler(5).get_info()
        with pytest.raises(requests.exceptions.HTTPError):
            egta.get_scheduler(2).get_info()
        with pytest.raises(ValueError):
            egta.get_scheduler('3')


def test_scheduler():
    with mockserver.Server() as server, api.EgtaOnlineApi() as egta:
        sim = create_simulator(server, egta, 'sim', '1')
        sched = sim.create_generic_scheduler('sched', True, 0, 10, 0, 0)
        assert_structure(sched, {
            'active': bool,
            'created_at': str,
            'default_observation_requirement': int,
            'id': int,
            'name': str,
            'nodes': int,
            'observations_per_simulation': int,
            'process_memory': int,
            'simulator_instance_id': int,
            'size': int,
            'time_per_observation': int,
            'updated_at': str,
        })

        info = sched.get_info()
        assert_structure(info, {
            'active': bool,
            'created_at': str,
            'default_observation_requirement': int,
            'id': int,
            'name': str,
            'nodes': int,
            'observations_per_simulation': int,
            'process_memory': int,
            'simulator_instance_id': int,
            'size': int,
            'time_per_observation': int,
            'updated_at': str,
        })

        assert_structure(sched.get_requirements(), {
            'active': bool,
            'configuration': list,
            'default_observation_requirement': int,
            'id': int,
            'name': str,
            'nodes': int,
            'observations_per_simulation': int,
            'process_memory': int,
            'scheduling_requirements': list,
            'simulator_id': int,
            'size': int,
            'time_per_observation': int,
            'type': str,
            'url': str,
        })

        sched.deactivate()
        assert not sched.get_info()['active']
        # stale info invalid
        assert info['active']

        sched.activate()
        assert sched.get_info()['active']

        sched.update(process_memory=1)
        assert sched.get_info()['process_memory'] == 1

        sched.add_dict({'a': 8})
        with pytest.raises(requests.exceptions.HTTPError):
            sched.add_role('a', 1)
        with pytest.raises(requests.exceptions.HTTPError):
            sched.add_role('c', 1)
        with pytest.raises(requests.exceptions.HTTPError):
            sched.add_role('b', 3)
        sched.add_role('b', 2)

        sched.remove_role('b')
        sched.remove_role('b')
        sched.remove_role('c')


def test_profiles():
    with mockserver.Server() as server, api.EgtaOnlineApi() as egta:
        sim = create_simulator(server, egta, 'sim', '1')
        sched1 = sim.create_generic_scheduler('sched', True, 0, 10, 0, 0)
        sched1.add_role('a', 8)
        sched1.add_role('b', 2)

        assignment = 'a: 8 1; b: 1 5, 1 7'
        symgrp = [{'role': 'a', 'strategy': '1', 'count': 8},
                  {'role': 'b', 'strategy': '5', 'count': 1},
                  {'role': 'b', 'strategy': '7', 'count': 1}]
        assert assignment == mockserver.symgrps_to_assignment(symgrp)
        prof1 = sched1.add_profile(assignment, 0)
        assert_structure(prof1, {
            'assignment': str,
            'created_at': str,
            'id': int,
            'observations_count': int,
            'role_configuration': dict,
            'simulator_instance_id': int,
            'size': int,
            'updated_at': str,
        })
        assert 0 == prof1.get_structure()['observations_count']
        for grp in prof1.get_summary()['symmetry_groups']:
            assert grp['payoff'] is None
            assert grp['payoff_sd'] is None
        assert not prof1.get_observations()['observations']
        assert not prof1.get_full_data()['observations']

        prof0 = egta.get_profile(prof1['id']).get_structure()
        assert prof1['id'] == prof0['id']
        assert prof1['id'] == sched1.add_profile(symgrp, 0)['id']

        assert prof1['id'] == sched1.update_profile(prof1, 3)['id']
        sched_complete(sched1)
        reqs = sched1.get_requirements()['scheduling_requirements']
        assert len(reqs) == 1
        assert reqs[0]['current_count'] == 3
        assert reqs[0]['requirement'] == 3
        assert reqs[0]['id'] == prof1['id']

        struct = prof1.get_structure()
        assert_structure(struct, {
            'assignment': str,
            'created_at': str,
            'id': int,
            'observations_count': int,
            'role_configuration': dict,
            'simulator_instance_id': int,
            'size': int,
            'updated_at': str,
        })
        assert struct['assignment'] == assignment
        assert struct['observations_count'] == 3
        assert struct['size'] == 10
        assert struct == prof1.get_info()
        assert struct == prof1.get_info('structure')

        summ = prof1.get_summary()
        assert_structure(summ, {
            'id': int,
            'observations_count': int,
            'simulator_instance_id': int,
            'symmetry_groups': list,
        })
        assert summ['observations_count'] == 3
        assert len(summ['symmetry_groups']) == 3
        assert summ == prof1.get_info('summary')

        obs = prof1.get_observations()
        assert_structure(obs, {
            'id': int,
            'simulator_instance_id': int,
            'symmetry_groups': list,
            'observations': list,
        })
        assert len(obs['symmetry_groups']) == 3
        assert len(obs['observations']) == 3
        assert all(len(o['symmetry_groups']) == 3 for o in obs['observations'])
        assert obs == prof1.get_info('observations')

        full = prof1.get_full_data()
        assert_structure(full, {
            'id': int,
            'simulator_instance_id': int,
            'symmetry_groups': list,
            'observations': list,
        })
        assert len(full['symmetry_groups']) == 3
        assert len(full['observations']) == 3
        assert all(len(o['players']) == 10 for o in full['observations'])
        assert full == prof1.get_info('full')

        with pytest.raises(requests.exceptions.HTTPError):
            prof1.get_info('unknown')

        sched2 = sim.create_generic_scheduler('sched2', True, 0, 10, 0, 0)
        sched2.add_role('a', 8)
        sched2.add_role('b', 2)
        prof2 = sched2.add_profile(assignment, 5)
        sched_complete(sched2)

        assert prof2['id'] == prof1['id']
        assert prof2.get_structure()['observations_count'] == 5
        assert prof1.get_structure()['observations_count'] == 5

        reqs = sched2.get_requirements()['scheduling_requirements']
        assert len(reqs) == 1
        assert reqs[0]['current_count'] == 5
        assert reqs[0]['requirement'] == 5

        reqs = sched1.get_requirements()['scheduling_requirements']
        assert len(reqs) == 1
        assert reqs[0]['current_count'] == 5
        assert reqs[0]['requirement'] == 3

        sched1.remove_profile(prof1)
        assert not sched1.get_requirements()['scheduling_requirements']

        updated_time = sched1.get_info()['updated_at']
        time.sleep(1)
        sched1.remove_profile(prof1)
        assert sched1.get_info()['updated_at'] == updated_time

        assert prof1['id'] == sched1.add_profile(assignment, 1)['id']
        reqs = sched1.get_requirements()['scheduling_requirements']
        assert len(reqs) == 1
        assert reqs[0]['current_count'] == 5
        assert reqs[0]['requirement'] == 1

        assert prof1['id'] == sched1.update_profile(prof1, 4)['id']
        reqs = sched1.get_requirements()['scheduling_requirements']
        assert len(reqs) == 1
        assert reqs[0]['current_count'] == 5
        assert reqs[0]['requirement'] == 4

        assert prof1['id'] == sched1.update_profile(prof1['id'], 6)['id']
        sched_complete(sched1)
        assert 6 == prof1.get_info()['observations_count']
        assert prof1['id'] == sched1.update_profile(assignment, 8)['id']
        sched_complete(sched1)
        assert 8 == prof1.get_info()['observations_count']

        # Test delayed scheduling
        sched1.deactivate()
        assert prof1['id'] == sched1.update_profile(symgrp, 9)['id']
        sched_complete(sched1)
        assert 8 == prof1.get_info()['observations_count']
        reqs = sched1.get_requirements()['scheduling_requirements']
        assert len(reqs) == 1
        assert reqs[0]['current_count'] == 8
        assert reqs[0]['requirement'] == 9
        sched1.activate()
        sched_complete(sched1)
        assert 9 == prof1.get_info()['observations_count']

        assert prof1['id'] == sched1.update_profile(
            {'assignment': assignment}, 12)['id']
        sched_complete(sched1)
        assert 12 == prof1.get_info()['observations_count']

        assert prof1['id'] == sched1.update_profile(
            {'symmetry_groups': symgrp}, 15)['id']
        sched_complete(sched1)
        assert 15 == prof1.get_info()['observations_count']

        sched1.remove_all_profiles()
        assert not sched1.get_requirements()['scheduling_requirements']
        sched1.remove_profile(10**10)

        assert sched2.get_requirements()['scheduling_requirements']
        sched2.remove_profile(prof2['id'])
        assert not sched2.get_requirements()['scheduling_requirements']


def test_delayed_profiles():
    with mockserver.Server() as server, api.EgtaOnlineApi() as egta:
        sim = egta.get_simulator(
            server.create_simulator('sim', '1', delay_dist=lambda: 0.2))
        sim.add_dict({'1': ['a'], '2': ['b', 'c']})
        sched = sim.create_generic_scheduler('sched', True, 0, 10, 0, 0)
        sched.add_role('1', 8)
        sched.add_role('2', 2)

        prof = sched.add_profile('1: 8 a; 2: 1 b, 1 c', 3)
        time.sleep(0.05)
        reqs = sched.get_requirements()['scheduling_requirements']
        assert len(reqs) == 1
        assert reqs[0]['current_count'] == 0
        assert reqs[0]['requirement'] == 3
        assert reqs[0]['id'] == prof['id']

        sims = egta.get_simulations()
        assert all(next(sims)['state'] == 'running' for _ in range(3))
        assert next(sims, None) is None

        time.sleep(0.15)
        sched_complete(sched)
        reqs = sched.get_requirements()['scheduling_requirements']
        assert len(reqs) == 1
        assert reqs[0]['current_count'] == 3
        assert reqs[0]['requirement'] == 3
        assert reqs[0]['id'] == prof['id']

        sims = egta.get_simulations()
        assert all(next(sims)['state'] == 'complete' for _ in range(3))
        assert next(sims, None) is None

    # Test that extra sims get killed
    with mockserver.Server() as server, api.EgtaOnlineApi() as egta:
        sim = egta.get_simulator(
            server.create_simulator('sim', '1', delay_dist=lambda: 10))
        sim.add_dict({'1': ['a'], '2': ['b', 'c']})
        sched = sim.create_generic_scheduler('sched', True, 0, 10, 0, 0)
        sched.add_role('1', 8)
        sched.add_role('2', 2)
        prof = sched.add_profile('1: 8 a; 2: 1 b, 1 c', 1)


def test_missing_profile():
    with mockserver.Server(), api.EgtaOnlineApi() as egta:
        prof = egta.get_profile(0)
        with pytest.raises(requests.exceptions.HTTPError):
            prof.get_info()


def test_get_games():
    with mockserver.Server() as server, api.EgtaOnlineApi() as egta:
        sim = create_simulator(server, egta, 'sim', '1')
        sim.create_game('a', 5)
        game2 = sim.create_game('b', 6)
        game3 = sim.create_game('c', 3)

        with pytest.raises(requests.exceptions.HTTPError):
            sim.create_game('b', 3)

        assert egta.get_game(1)['id'] == game2['id']
        assert egta.get_game('c')['id'] == game3['id']

        assert 3 == sum(1 for _ in egta.get_games())
        assert {0, 1, 2} == {g['id'] for g in egta.get_games()}

        game2.destroy_game()

        assert 2 == sum(1 for _ in egta.get_games())
        assert {0, 2} == {g['id'] for g in egta.get_games()}

        with sim.create_temp_game(3) as game4:
            assert game4.get_info()['size'] == 3
        # Temp game deleted at end of with
        with pytest.raises(requests.exceptions.HTTPError):
            game4.get_info()

        with pytest.raises(requests.exceptions.HTTPError):
            egta.get_game(3).get_info()
        with pytest.raises(requests.exceptions.HTTPError):
            egta.get_game(1).get_info()
        with pytest.raises(ValueError):
            egta.get_game('b')


def test_game():
    with mockserver.Server() as server, api.EgtaOnlineApi() as egta:
        sim = create_simulator(server, egta, 'sim', '1')
        sched = sim.create_generic_scheduler('sched', True, 0, 4, 0, 0,
                                             configuration={'k': 'v'})
        sched.add_role('a', 2)
        sched.add_role('b', 2)

        with sched.create_temp_game() as tmp_game:
            tmp_game.get_info()
        with pytest.raises(requests.exceptions.HTTPError):
            tmp_game.get_info()

        game = sched.create_game()
        game.add_role('a', 2)
        game.add_strategy('a', '1')
        game.add_role('b', 2)
        game.add_strategy('b', '5')
        game.add_strategy('b', '6')

        assert game.get_simulator()['id'] == sim['id']

        prof = sched.add_profile('a: 2 1; b: 1 5, 1 6', 0)
        assert 1 == len(sched.get_requirements()['scheduling_requirements'])
        assert not game.get_summary()['profiles']
        assert not game.get_observations()['profiles']
        assert not game.get_full_data()['profiles']

        sched.update_profile(prof, 1)
        sched.add_profile('a: 2 1; b: 2 5', 2)
        sched_complete(sched)

        size_counts = {}
        for prof in game.get_summary()['profiles']:
            counts = prof['observations_count']
            size_counts[counts] = size_counts.get(counts, 0) + 1
        assert size_counts == {1: 1, 2: 1}

        size_counts = {}
        for prof in game.get_observations()['profiles']:
            counts = len(prof['observations'])
            size_counts[counts] = size_counts.get(counts, 0) + 1
        assert size_counts == {1: 1, 2: 1}

        size_counts = {}
        for prof in game.get_full_data()['profiles']:
            for obs in prof['observations']:
                assert len(obs['players']) == 4
            counts = len(prof['observations'])
            size_counts[counts] = size_counts.get(counts, 0) + 1
        assert size_counts == {1: 1, 2: 1}

        assert game.get_info('structure') == game.get_structure()
        assert game.get_info('summary') == game.get_summary()
        assert game.get_info('observations') == game.get_observations()
        assert game.get_info('full') == game.get_full_data()

        with pytest.raises(requests.exceptions.HTTPError):
            game.get_info('missing')

        game.remove_strategy('b', '6')
        assert len(game.get_summary()['profiles']) == 1
        assert len(game.get_observations()['profiles']) == 1
        assert len(game.get_full_data()['profiles']) == 1

        game.remove_strategy('a', '4')
        game.add_dict({'a': ['2', '3']})
        game.remove_dict({'a': ['1', '3']})
        game.remove_role('b')

        updated_time = game.get_info()['updated_at']
        time.sleep(1)
        game.remove_role('b')
        assert game.get_info()['updated_at'] == updated_time


def test_get_or_create_game():
    with mockserver.Server() as server, api.EgtaOnlineApi() as egta:
        sim = create_simulator(server, egta, 'sim', '1')
        sim2 = create_simulator(server, egta, 'sim', '2')

        players = {'a': 2, 'b': 2}
        strats = {'a': ['1'], 'b': ['5', '6']}
        conf = {'key': 'val'}
        game1 = egta.create_or_get_game(sim['id'], players, strats, conf)
        summ = game1.get_summary()
        assert conf == dict(summ['configuration'])

        def unpack(name, count, strategies):
            return name, count, strategies

        for role_info in summ['roles']:
            role, count, strategies = unpack(**role_info)
            assert count == players[role]
            assert set(strategies) == set(strats[role])

        game2 = egta.create_or_get_game(sim['id'], players, strats,
                                        {'key': 'diff'})
        assert game1['id'] != game2['id']

        game3 = egta.create_or_get_game(sim['id'], players,
                                        {'a': ['1'], 'b': ['5', '7']}, conf)
        assert game1['id'] != game3['id']

        game4 = egta.create_or_get_game(sim['id'], {'a': 2, 'b': 1}, strats,
                                        conf)
        assert game1['id'] != game4['id']

        game5 = egta.create_or_get_game(sim2['id'], players, strats, conf)
        assert game1['id'] != game5['id']

        game6 = egta.create_or_get_game(sim['id'], players, strats, conf)
        assert game1['id'] == game6['id']


def test_large_game_failsafes():
    error = requests.exceptions.HTTPError('500 Server Error: Game too large!')
    with mockserver.Server() as server, api.EgtaOnlineApi() as egta:
        sim = create_simulator(server, egta, 'sim', '1')
        sched = sim.create_generic_scheduler('sched', True, 0, 4, 0, 0,
                                             configuration={'k': 'v'})
        sched.add_role('a', 2)
        sched.add_role('b', 2)

        game = sched.create_game()
        game.add_role('a', 2)
        game.add_strategy('a', '1')
        game.add_role('b', 2)
        game.add_strategy('b', '5')
        game.add_strategy('b', '6')

        sched.add_profile('a: 2 1; b: 1 5, 1 6', 1)
        sched.add_profile('a: 2 1; b: 2 5', 2)
        sched_complete(sched)

        base = game.get_observations()
        server.throw_exception(error)
        alternate = game.get_observations()
        assert base == alternate
        size_counts = {}
        for prof in alternate['profiles']:
            counts = len(prof['observations'])
            size_counts[counts] = size_counts.get(counts, 0) + 1
        assert size_counts == {1: 1, 2: 1}

        base = game.get_full_data()
        server.throw_exception(error)
        alternate = game.get_full_data()
        assert base == alternate
        size_counts = {}
        for prof in alternate['profiles']:
            for obs in prof['observations']:
                assert len(obs['players']) == 4
            counts = len(prof['observations'])
            size_counts[counts] = size_counts.get(counts, 0) + 1
        assert size_counts == {1: 1, 2: 1}


@pytest.mark.long
def test_game_json_error():
    with mockserver.Server() as server, api.EgtaOnlineApi() as egta:
        sim = create_simulator(server, egta, 'sim', '1')
        sched = sim.create_generic_scheduler('sched', True, 0, 4, 0, 0)
        sched.add_role('a', 2)
        sched.add_role('b', 2)

        game = sched.create_game()
        game.add_role('a', 2)
        game.add_strategy('a', '1')
        game.add_role('b', 2)
        game.add_strategy('b', '5')
        game.add_strategy('b', '6')

        sched.add_profile('a: 2 1; b: 1 5, 1 6', 1)
        sched.add_profile('a: 2 1; b: 2 5', 2)
        sched_complete(sched)

        server.invalid_games(9)
        size_counts = {}
        for prof in game.get_summary()['profiles']:
            counts = prof['observations_count']
            size_counts[counts] = size_counts.get(counts, 0) + 1
        assert size_counts == {1: 1, 2: 1}

        server.invalid_games(10)
        with pytest.raises(json.decoder.JSONDecodeError):
            game.get_summary()


def test_get_simulations():
    with mockserver.Server() as server, api.EgtaOnlineApi() as egta:
        assert 0 == sum(1 for _ in egta.get_simulations())

        sim1 = create_simulator(server, egta, 'sim', '1')
        sched1 = sim1.create_generic_scheduler('sched1', True, 0, 4, 0, 0)
        sched1.add_role('a', 2)
        sched1.add_role('b', 2)
        sched1.add_profile('a: 2 1; b: 1 6, 1 7', 2)

        assert sum(1 for _ in egta.get_simulations()) == 2
        simul = next(egta.get_simulations())
        assert_structure(simul, {
            'folder': int,
            'job': float,
            'profile': str,
            'simulator': str,
            'state': str,
        })
        assert_structure(egta.get_simulation(simul['folder']), {
            'error_message': str,
            'folder_number': int,
            'job': str,
            'profile': str,
            'simulator_fullname': str,
            'size': int,
            'state': str,
        })

        sim2 = create_simulator(server, egta, 'sim', '2')
        sched2 = sim2.create_generic_scheduler('sched2', True, 0, 5, 0, 0)
        sched2.add_role('a', 2)
        sched2.add_role('b', 3)
        sched2.add_profile('a: 2 1; b: 1 5, 2 7', 3)

        assert sum(1 for _ in egta.get_simulations()) == 5

        # Test simulations
        assert is_sorted((f['simulator'] for f
                          in egta.get_simulations(column='simulator')),
                         reverse=True)
        assert is_sorted((f['folder'] for f
                          in egta.get_simulations(column='folder')),
                         reverse=True)
        assert is_sorted((f['profile'] for f
                          in egta.get_simulations(column='profile')),
                         reverse=True)
        assert is_sorted((f['state'] for f
                          in egta.get_simulations(column='state')),
                         reverse=True)

        assert is_sorted(f['simulator'] for f
                         in egta.get_simulations(asc=True, column='simulator'))
        assert is_sorted(f['folder'] for f
                         in egta.get_simulations(asc=True, column='folder'))
        assert is_sorted(f['profile'] for f
                         in egta.get_simulations(asc=True, column='profile'))
        assert is_sorted(f['state'] for f
                         in egta.get_simulations(asc=True, column='state'))

        assert 0 == sum(1 for _ in egta.get_simulations(page_start=2))
        sched2.add_profile('a: 2 1; b: 1 5, 2 6', 21)
        assert 1 == sum(1 for _ in egta.get_simulations(page_start=2))


def test_exceptions():
    with mockserver.Server() as server, api.EgtaOnlineApi() as egta:
        sim = egta.get_simulator(server.create_simulator('sim', '1'))
        sim.add_role('role')
        sim.add_strategy('role', 'strategy')
        sched = sim.create_generic_scheduler('sched', True, 0, 1, 0, 0)
        sched.add_role('role', 1)
        prof = sched.add_profile('role: 1 strategy', 1)
        game = sched.create_game('game')
        game.add_role('role', 1)
        game.add_strategy('role', 'strategy')

        server.throw_exception(TimeoutError, 11)

        # Creations fail
        with pytest.raises(TimeoutError):
            sim.create_generic_scheduler('sched_2', False, 0, 0, 0, 0)
        with pytest.raises(TimeoutError):
            sched.create_game()

        # Infos fail
        with pytest.raises(TimeoutError):
            sim.get_info()
        with pytest.raises(TimeoutError):
            sched.get_info()
        with pytest.raises(TimeoutError):
            game.get_info()
        with pytest.raises(TimeoutError):
            prof.get_info()

        # Mutates fail
        with pytest.raises(TimeoutError):
            sim.add_role('r')
        with pytest.raises(TimeoutError):
            sim.add_strategy('role', 's')
        with pytest.raises(TimeoutError):
            sched.add_role('r', 1)
        with pytest.raises(TimeoutError):
            game.add_role('r', 1)
        with pytest.raises(TimeoutError):
            game.add_strategy('role', 's')

        # Succeed after done
        sim.get_info()
