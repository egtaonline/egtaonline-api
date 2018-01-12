import collections
import time

import pytest

from egtaonline import api
from egtaonline import mockserver


class _fdict(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__hash = None

    def __hash__(self):
        if self.__hash is None:
            self.__hash = hash(frozenset(self.items()))
        return self.__hash


def describe_structure(obj, illegal=(), nums=False):
    """Compute an object that represents the recursive structure"""
    if isinstance(obj, dict):
        return _fdict((k, describe_structure(v, illegal, nums))
                      for k, v in obj.items()
                      if k not in illegal)
    elif isinstance(obj, list):  # FIXME Iterable
        counts = collections.Counter(
            describe_structure(o, illegal, nums) for o in obj)
        return _fdict(counts)
    # NaNs are represented as None
    elif nums and isinstance(obj, (int, float, type(None))):
        return float
    else:
        return type(obj)


def assert_dicts_types(actual, expected, illegal=(), nums=False):
    assert (describe_structure(actual, illegal, nums) ==
            describe_structure(expected, illegal, nums))


_illegal_keys = {'created_at', 'updated_at', 'simulator_instance_id'}


def assert_dicts_equal(actual, expected, illegal=()):
    assert actual.keys() == expected.keys(), \
        "keys weren't equal"
    assert ({k: v for k, v in actual.items()  # pragma: no branch
             if k not in _illegal_keys and k not in illegal} ==
            {k: v for k, v in expected.items()
             if k not in _illegal_keys and k not in illegal})


def sched_complete(sched, sleep=0.001):
    while sched.get_info()['active'] and not all(
            p['requirement'] <= p['current_count'] for p
            in sched.get_requirements()['scheduling_requirements']):
        time.sleep(sleep)


def get_existing_objects(egta):
    illegal_sched_ids = set()
    while True:
        true_sched = next(  # pragma: no branch
            s for s in egta.get_generic_schedulers()
            if s['id'] not in illegal_sched_ids)
        try:
            true_sim = egta.get_simulator(
                true_sched.get_requirements()['simulator_id'])
            true_game = next(  # pragma: no branch
                g for g in egta.get_games()
                if g['simulator_instance_id'] ==
                true_sched['simulator_instance_id'])
            assert true_sched.get_requirements().get(
                'scheduling_requirements', ())
            return true_sim.get_info(), true_sched, true_game
        except (StopIteration, AssertionError):  # pragma: no cover
            illegal_sched_ids.add(true_sched['id'])


@pytest.mark.egta
def test_parity():
    # Get egta data
    with api.EgtaOnlineApi() as egta:
        true_sim, true_sched, true_game = get_existing_objects(egta)
        reqs = true_sched.get_requirements()
        prof_info = [
            (prof.get_info(), prof.get_summary(), prof.get_observations(),
             prof.get_full_data()) for prof in reqs['scheduling_requirements']]
        game_info = true_game.get_info()
        game_summ = true_game.get_summary()

    # Replicate in mock
    with mockserver.Server() as server, api.EgtaOnlineApi() as egta:
        for i in range(true_sim['id']):
            server.create_simulator('sim', str(i))
        mock_sim = egta.get_simulator(server.create_simulator(
            true_sim['name'], true_sim['version'], true_sim['email'],
            true_sim['configuration']))
        mock_sim.add_dict(true_sim['role_configuration'])

        assert_dicts_types(true_sim, mock_sim.get_info())
        assert_dicts_equal(true_sim, mock_sim.get_info())

        for i in range(true_sched['id']):
            mock_sim.create_generic_scheduler(str(i), False, 0, 0, 0, 0)
        mock_sched = mock_sim.create_generic_scheduler(
            true_sched['name'], true_sched['active'],
            true_sched['process_memory'], true_sched['size'],
            true_sched['time_per_observation'],
            true_sched['observations_per_simulation'], true_sched['nodes'],
            dict(reqs['configuration']))

        assert_dicts_types(true_sched, mock_sched.get_info())
        assert_dicts_equal(true_sched, mock_sched.get_info())

        game_sched = mock_sim.create_generic_scheduler(
            'temp', True, 0, true_sched['size'], 0, 0, 1,
            dict(reqs['configuration']))

        for role, count in prof_info[0][0]['role_configuration'].items():
            mock_sched.add_role(role, int(count))
            game_sched.add_role(role, int(count))

        mock_sched.activate()
        for prof, (info, summ, obs, full) in zip(
                reqs['scheduling_requirements'], prof_info):
            sp = game_sched.add_profile(
                info['assignment'], prof['current_count'])
            mp = mock_sched.add_profile(
                info['assignment'], prof['requirement'])
            sched_complete(game_sched)
            sched_complete(mock_sched)
            assert sp.get_info()['observations_count'] == prof['current_count']
            assert sp['id'] == mp['id']

            assert_dicts_types(info, mp.get_info())
            assert_dicts_equal(info, mp.get_info(), {'id'})

            assert_dicts_types(summ, mp.get_summary(), (), True)
            assert_dicts_types(obs, mp.get_observations(),
                               {'extended_features', 'features'},
                               True)
            assert_dicts_types(full, mp.get_full_data(),
                               {'extended_features', 'features', 'e', 'f'},
                               True)

        for i in range(true_game['id']):
            mock_sim.create_game(str(i), 0)
        mock_game = mock_sim.create_game(
            true_game['name'], true_game['size'],
            dict(game_summ['configuration'])).get_info()
        assert_dicts_types(game_info, mock_game.get_info())
        assert_dicts_equal(game_info, mock_game.get_info())

        for grp in game_summ['roles']:
            role = grp['name']
            mock_game.add_role(role, grp['count'])
            for strat in grp['strategies']:
                mock_game.add_strategy(role, strat)
        # Schedule next profiles
        for prof in game_summ['profiles']:
            game_sched.add_profile(
                prof['symmetry_groups'], prof['observations_count'])
        sched_complete(game_sched)

        assert_dicts_types(game_summ, mock_game.get_summary(), (), True)
        # TODO Assert full_data and observations


@pytest.mark.egta
def test_equality():
    with api.EgtaOnlineApi() as egta:
        summ = next(  # pragma: no branch
            g for g in (g.get_summary() for g in egta.get_games())
            if g['profiles'])
        prof = summ['profiles'][0]

        assert prof.get_structure() == prof.get_info()
        assert prof.get_summary() == prof.get_info('summary')
        assert prof.get_observations() == prof.get_info('observations')
        assert prof.get_full_data() == prof.get_info('full')

        assert summ.get_structure() == summ.get_info()
        assert summ == summ.get_info('summary')
        assert summ.get_observations() == summ.get_info('observations')
        assert summ.get_full_data() == summ.get_info('full')


@pytest.mark.egta
def test_gets():
    with api.EgtaOnlineApi() as egta:
        with pytest.raises(ValueError):
            egta.get_simulator('this name is impossible I hope')

        sims = {}
        for sim in egta.get_simulators():
            sims.setdefault(sim['name'], []).append(sim)

        sim = next(  # pragma: no branch
            s for s, *rest in sims.values() if not rest)
        assert egta.get_simulator(sim['id']).get_info()['id'] == sim['id']
        assert egta.get_simulator(sim['name'])['id'] == sim['id']
        assert egta.get_simulator(
            sim['name'], sim['version'])['id'] == sim['id']

        sim = next(  # pragma: no branch
            s for s, *rest in sims.values() if rest)
        assert egta.get_simulator(sim['id']).get_info()['id'] == sim['id']
        with pytest.raises(ValueError):
            egta.get_simulator(sim['name'])
        assert egta.get_simulator(
            sim['name'], sim['version'])['id'] == sim['id']

        sched = next(egta.get_generic_schedulers())
        assert egta.get_scheduler(sched['id']).get_info()['id'] == sched['id']
        assert egta.get_scheduler(sched['name'])['id'] == sched['id']
        with pytest.raises(ValueError):
            egta.get_scheduler('this name is impossible I hope')

        game = next(egta.get_games())
        assert egta.get_game(game['id']).get_info()['id'] == game['id']
        assert egta.get_game(game['name'])['id'] == game['id']
        with pytest.raises(ValueError):
            egta.get_game('this name is impossible I hope')

        fold = next(egta.get_simulations())
        assert egta.get_simulation(
            fold['folder'])['folder_number'] == fold['folder']
        for sort in ['job', 'folder', 'profile', 'simulator', 'state']:
            assert 'folder' in next(egta.get_simulations(column=sort))
        assert next(egta.get_simulations(page_start=10**9), None) is None

        reqs = (  # pragma: no branch
            s.get_requirements() for s in egta.get_generic_schedulers())
        sched = next(  # pragma: no branch
            s for s in reqs if s['scheduling_requirements'])
        prof = sched['scheduling_requirements'][0]
        assert egta.get_profile(prof['id']).get_info()['id'] == prof['id']


@pytest.mark.egta
def test_modify_simulator():
    # XXX This is very "dangerous" because we're just finding and modifying a
    # random simulator. However, adding and removing a random role shouldn't
    # really affect anything, so this should be fine
    role = '__unique_role__'
    strat1 = '__unique_strategy_1__'
    strat2 = '__unique_strategy_2__'
    with api.EgtaOnlineApi() as egta:
        # Old sims seem frozen so > 100
        sim = next(  # pragma: no branch
            s for s in egta.get_simulators() if s['id'] > 100)
        try:
            sim.add_dict({role: [strat1]})
            assert sim.get_info()['role_configuration'][role] == [strat1]

            sim.add_strategy(role, strat2)
            expected = [strat1, strat2]
            assert sim.get_info()['role_configuration'][role] == expected

            sim.remove_dict({role: [strat1]})
            assert sim.get_info()['role_configuration'][role] == [strat2]
        finally:
            sim.remove_role(role)

        assert role not in sim.get_info()['role_configuration']


# TODO These would be a little nicer if there was a "temp scheduler" and "temp
# game" context manager


@pytest.mark.egta
def test_modify_scheduler():
    with api.EgtaOnlineApi() as egta:
        sim = next(s for s in egta.get_simulators()  # pragma: no branch
                   if next(iter(s['role_configuration'].values()), None))
        sched = game = None
        try:
            sched = sim.create_generic_scheduler(
                '__unique_scheduler__', False, 0, 1, 0, 0)
            sched.activate()
            sched.deactivate()

            role = next(iter(sim['role_configuration']))
            strat = sim['role_configuration'][role][0]
            symgrps = [{'role': role, 'strategy': strat, 'count': 1}]
            assignment = api.symgrps_to_assignment(symgrps)

            sched.add_role(role, 1)

            reqs = sched.get_requirements()['scheduling_requirements']
            assert not reqs

            prof = sched.add_profile(symgrps, 1)
            reqs = sched.get_requirements()['scheduling_requirements']
            assert len(reqs) == 1
            assert reqs[0]['requirement'] == 1

            assert sched.update_profile(prof['id'], 2)['id'] == prof['id']
            reqs = sched.get_requirements()['scheduling_requirements']
            assert len(reqs) == 1
            assert reqs[0]['requirement'] == 2

            assert sched.update_profile(assignment, 4)['id'] == prof['id']
            reqs = sched.get_requirements()['scheduling_requirements']
            assert len(reqs) == 1
            assert reqs[0]['requirement'] == 4

            assert sched.update_profile(prof, 5)['id'] == prof['id']
            reqs = sched.get_requirements()['scheduling_requirements']
            assert len(reqs) == 1
            assert reqs[0]['requirement'] == 5

            sched.remove_profile(prof)
            reqs = sched.get_requirements()['scheduling_requirements']
            assert not reqs

            assert sched.update_profile(symgrps, 3)['id'] == prof['id']
            reqs = sched.get_requirements()['scheduling_requirements']
            assert len(reqs) == 1
            assert reqs[0]['requirement'] == 3

            sched.remove_all_profiles()
            reqs = sched.get_requirements()['scheduling_requirements']
            assert not reqs

            sched.remove_role(role)

            game = sched.create_game()

        finally:
            if sched is not None:  # pragma: no branch
                sched.destroy_scheduler()
            if game is not None:  # pragma: no branch
                game.destroy_game()


@pytest.mark.egta
def test_modify_game():
    with api.EgtaOnlineApi() as egta:
        sim = next(s for s in egta.get_simulators()  # pragma: no branch
                   if next(iter(s['role_configuration'].values()), None))
        game = None
        try:
            game = sim.create_game('__unique_game__', 1)

            summ = game.get_summary()
            assert not summ['roles']
            assert not summ['profiles']

            role = next(iter(sim['role_configuration']))
            strat = sim['role_configuration'][role][0]
            game.add_role(role, 1)
            summ = game.get_summary()
            assert 1 == len(summ['roles'])
            assert summ['roles'][0]['name'] == role
            assert summ['roles'][0]['count'] == 1
            assert not summ['roles'][0]['strategies']

            game.add_dict({role: [strat]})
            summ = game.get_summary()
            assert 1 == len(summ['roles'])
            assert summ['roles'][0]['name'] == role
            assert summ['roles'][0]['count'] == 1
            assert summ['roles'][0]['strategies'] == [strat]

            game.remove_dict({role: [strat]})
            summ = game.get_summary()
            assert 1 == len(summ['roles'])
            assert summ['roles'][0]['name'] == role
            assert summ['roles'][0]['count'] == 1
            assert not summ['roles'][0]['strategies']

            game.remove_role(role)
            summ = game.get_summary()
            assert not summ['roles']
            assert not summ['profiles']

        finally:
            if game is not None:  # pragma: no branch
                game.destroy_game()
