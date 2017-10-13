import collections

import pytest
from egtaonline import api, mockapi


def describe_structure(obj, illegal=(), nums=False):
    """Compute an object that represents the recursive structure"""
    if isinstance(obj, dict):
        return frozenset((k, describe_structure(v, illegal, nums))
                         for k, v in obj.items()
                         if k not in illegal)
    elif isinstance(obj, list):  # FIXME Iterable
        counts = collections.Counter(
            describe_structure(o, illegal, nums) for o in obj)
        return frozenset(counts.items())
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
    assert ({k: v for k, v in actual.items()
             if k not in _illegal_keys and k not in illegal} ==
            {k: v for k, v in expected.items()
             if k not in _illegal_keys and k not in illegal})


def get_existing_objects(egta):
    illegal_sched_ids = set()
    while True:
        try:
            true_sched = next(s for s in egta.get_generic_schedulers()
                              if s['id'] not in illegal_sched_ids)
        except StopIteration:  # pragma: no cover
            raise ValueError("No set of all objects")

        try:
            true_sim = egta.get_simulator(
                true_sched.get_requirements()['simulator_id'])
            true_game = next(g for g in egta.get_games()
                             if (g['simulator_instance_id'] ==
                                 true_sched['simulator_instance_id']))
            assert true_sched.get_requirements().get(
                'scheduling_requirements', ())
            return true_sim.get_info(), true_sched, true_game
        except (StopIteration, AssertionError):  # pragma: no cover
            illegal_sched_ids.add(true_sched['id'])


@pytest.mark.egta
def test_parity():
    with api.EgtaOnlineApi() as egta, mockapi.EgtaOnlineApi() as mockegta:
        true_sim, true_sched, true_game = get_existing_objects(egta)

        for _ in range(true_sim['id']):
            mockegta.create_simulator()
        mock_sim = mockegta.create_simulator(
            true_sim['name'], true_sim['version'], true_sim['email'],
            true_sim['configuration'])
        mock_sim.add_dict(true_sim['role_configuration'])

        assert_dicts_types(true_sim, mock_sim.get_info())
        assert_dicts_equal(true_sim, mock_sim.get_info())

        for i in range(true_sched['id']):
            mock_sim.create_generic_scheduler(str(i), False, 0, 0, 0, 0)
        reqs = true_sched.get_requirements()
        mock_sched = mock_sim.create_generic_scheduler(
            true_sched['name'], true_sched['active'],
            true_sched['process_memory'], true_sched['size'],
            true_sched['time_per_observation'],
            true_sched['observations_per_simulation'], true_sched['nodes'],
            dict(reqs['configuration']))

        assert_dicts_types(true_sched, mock_sched.get_info())
        assert_dicts_equal(true_sched, mock_sched.get_info())

        sched2 = mock_sim.create_generic_scheduler(
            'temp', True, 0, true_sched['size'], 0, 0)

        prof = reqs['scheduling_requirements'][0]
        for role, count in prof.get_info()['role_configuration'].items():
            mock_sched.add_role(role, int(count))
            sched2.add_role(role, int(count))

        mock_sched.activate()
        for prof in reqs['scheduling_requirements']:
            info = prof.get_info()
            sched2.add_profile(info['assignment'], prof['current_count'])
            mp = mock_sched.add_profile(
                info['assignment'], prof['requirement'])

            assert_dicts_types(info, mp.get_info())
            assert_dicts_equal(info, mp.get_info(), {'id'})

            assert_dicts_types(prof.get_summary(), mp.get_summary(), (), True)
            assert_dicts_types(prof.get_observations(), mp.get_observations(),
                               {'extended_features', 'features'},
                               True)
            assert_dicts_types(prof.get_full_data(), mp.get_full_data(),
                               {'extended_features', 'features', 'e', 'f'},
                               True)

        for i in range(true_game['id']):
            mock_sim.create_game(str(i), 0)
        mock_game = mock_sim.create_game(
            true_game['name'], true_game['size']).get_info()
        info = true_game.get_info()
        assert_dicts_types(info, mock_game.get_info())
        assert_dicts_equal(info, mock_game.get_info())

        summ = true_game.get_summary()
        for grp in summ['roles']:
            role = grp['name']
            mock_game.add_role(role, grp['count'])
            for strat in grp['strategies']:
                mock_game.add_strategy(role, strat)
        # Schedule next profiles
        for prof in summ['profiles']:
            sched2.add_profile(
                prof['symmetry_groups'], prof['observations_count'])

        assert_dicts_types(summ, mock_game.get_summary(), (), True)
        # TODO Assert full_data and observations


@pytest.mark.egta
def test_equality():
    with api.EgtaOnlineApi() as egta:
        summ = next(g for g in (g.get_summary() for g in egta.get_games())
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
