import async_generator
import asyncio
import collections

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


async def sched_complete(sched, sleep=0.001):
    while (await sched.get_info())['active'] and not all(
            p['requirement'] <= p['current_count'] for p
            in (await sched.get_requirements())['scheduling_requirements']):
        await asyncio.sleep(sleep)


async def get_existing_objects(egta):
    scheds = egta.get_generic_schedulers()
    try:
        async for sched in scheds:
            try:
                reqs = await sched.get_requirements()
                assert reqs.get('scheduling_requirements', ())
                sim = await egta.get_simulator(reqs['simulator_id'])
                games = egta.get_games()
                try:
                    async for game in games:
                        if (game['simulator_instance_id'] ==
                                sched['simulator_instance_id']):
                            return sim, sched, game
                finally:
                    await games.aclose()
            except AssertionError:  # pragma: no cover
                continue
        assert False, "no valie games"  # pragma: no cover
    finally:
        await scheds.aclose()


@pytest.fixture
@async_generator.async_generator
async def egta():
    async with api.api() as egta:
        await async_generator.yield_(egta)


@pytest.mark.asyncio
@pytest.mark.egta
async def test_parity():
    # Get egta data
    async with api.api() as egta:
        true_sim, true_sched, true_game = await get_existing_objects(egta)
        reqs = await true_sched.get_requirements()

        async def get_prof_info(prof):
            return (await prof.get_structure(),
                    await prof.get_summary(),
                    await prof.get_observations(),
                    await prof.get_full_data())

        prof_info = await asyncio.gather(*[
            get_prof_info(prof)
            for prof in reqs['scheduling_requirements']])
        game_info = await true_game.get_structure()
        game_summ = await true_game.get_summary()

    # Replicate in mock
    async with mockserver.server() as server, api.api() as egta:
        for i in range(true_sim['id']):
            server.create_simulator('sim', str(i))
        mock_sim = await egta.get_simulator(server.create_simulator(
            true_sim['name'], true_sim['version'], true_sim['email'],
            true_sim['configuration']))
        await mock_sim.add_strategies(true_sim['role_configuration'])

        info = await mock_sim.get_info()
        assert_dicts_types(true_sim, info)
        assert_dicts_equal(true_sim, info)

        await asyncio.gather(*[
            mock_sim.create_generic_scheduler(str(i), False, 0, 0, 0, 0)
            for i in range(true_sched['id'])])
        mock_sched = await mock_sim.create_generic_scheduler(
            true_sched['name'], true_sched['active'],
            true_sched['process_memory'], true_sched['size'],
            true_sched['time_per_observation'],
            true_sched['observations_per_simulation'], true_sched['nodes'],
            dict(reqs['configuration']))

        info = await mock_sched.get_info()
        assert_dicts_types(true_sched, info)
        assert_dicts_equal(true_sched, info)

        game_sched = await mock_sim.create_generic_scheduler(
            'temp', True, 0, true_sched['size'], 0, 0, 1,
            dict(reqs['configuration']))

        counts = prof_info[0][0]['role_configuration']
        await mock_sched.add_roles(counts)
        await game_sched.add_roles(counts)

        await mock_sched.activate()
        for prof, (info, summ, obs, full) in zip(
                reqs['scheduling_requirements'], prof_info):
            sp = await game_sched.add_profile(
                info['assignment'], prof['current_count'])
            mp = await mock_sched.add_profile(
                info['assignment'], prof['requirement'])
            await sched_complete(game_sched)
            await sched_complete(mock_sched)
            assert ((await sp.get_structure())['observations_count'] ==
                    prof['current_count'])
            assert sp['id'] == mp['id']

            info = await mp.get_structure()
            assert_dicts_types(info, info)
            assert_dicts_equal(info, info, {'id'})

            assert_dicts_types(summ, (await mp.get_summary()), (), True)
            assert_dicts_types(obs, (await mp.get_observations()),
                               {'extended_features', 'features'},
                               True)
            assert_dicts_types(full, (await mp.get_full_data()),
                               {'extended_features', 'features', 'e', 'f'},
                               True)

        await asyncio.gather(*[
            mock_sim.create_game(str(i), 0) for i in range(true_game['id'])])
        mock_game = await mock_sim.create_game(
            true_game['name'], true_game['size'],
            dict(game_summ['configuration']))
        info = await mock_game.get_structure()
        assert_dicts_types(game_info, info)
        assert_dicts_equal(game_info, info)

        symgrps = [
            (grp['name'], grp['count'], grp['strategies'])
            for grp in game_summ['roles']]
        await mock_game.add_symgroups(symgrps)

        # Schedule next profiles
        await asyncio.gather(*[
            game_sched.add_profile(
                prof['symmetry_groups'], prof['observations_count'])
            for prof in game_summ['profiles']])
        await sched_complete(game_sched)

        assert_dicts_types(
            game_summ, (await mock_game.get_summary()), (), True)
        # TODO Assert full_data and observations


async def agather(gen):
    res = []
    async for e in gen:
        res.append(e)
    return res


@pytest.mark.asyncio
@pytest.mark.egta
async def test_gets(egta):
    with pytest.raises(AssertionError):
        await egta.get_simulator_fullname('this name is impossible I hope')

    async def test_sim_name(sim):
        assert sim['id'] == (await egta.get_simulator_fullname(
            '{}-{}'.format(sim['name'], sim['version'])))['id']

    await asyncio.gather(*[
        test_sim_name(sim) for sim
        in await agather(egta.get_simulators())])

    scheds = egta.get_generic_schedulers()
    sched = await scheds.__anext__()
    await scheds.aclose()
    assert (await egta.get_scheduler(sched['id']))['id'] == sched['id']
    assert ((await egta.get_scheduler_name(sched['name']))['id'] ==
            sched['id'])
    with pytest.raises(AssertionError):
        await egta.get_scheduler_name('this name is impossible I hope')

    games = egta.get_games()
    game = await games.__anext__()
    await games.aclose()
    assert (await egta.get_game(game['id']))['id'] == game['id']
    assert (await egta.get_game_name(game['name']))['id'] == game['id']
    with pytest.raises(AssertionError):
        await egta.get_game_name('this name is impossible I hope')

    folds = egta.get_simulations()
    fold = await folds.__anext__()
    await folds.aclose()
    assert (await egta.get_simulation(
        fold['folder']))['folder_number'] == fold['folder']
    for sort in ['job', 'folder', 'profile', 'simulator', 'state']:
        assert 'folder' in await egta.get_simulations(
            column=sort).__anext__()
    with pytest.raises(StopAsyncIteration):
        await egta.get_simulations(page_start=10**9).__anext__()

    scheds = egta.get_generic_schedulers()
    async for s in scheds:
        sched = await s.get_requirements()
        if sched['scheduling_requirements']:
            break
    await scheds.aclose()
    prof = sched['scheduling_requirements'][0]
    assert (await egta.get_profile(prof['id']))['id'] == prof['id']


@pytest.mark.asyncio
@pytest.mark.egta
async def test_modify_simulator(egta):
    # XXX This is very "dangerous" because we're just finding and modifying a
    # random simulator. However, adding and removing a random role shouldn't
    # really affect anything, so this should be fine
    role = '__unique_role__'
    strat1 = '__unique_strategy_1__'
    strat2 = '__unique_strategy_2__'
    # Old sims seem frozen so > 100
    sims = egta.get_simulators()
    async for sim in sims:
        if sim['id'] > 100:
            break
    await sims.aclose()
    try:
        await sim.add_strategies({role: [strat1]})
        assert ((await sim.get_info())['role_configuration'][role] ==
                [strat1])

        await sim.add_strategy(role, strat2)
        expected = [strat1, strat2]
        assert ((await sim.get_info())['role_configuration'][role] ==
                expected)

        await sim.remove_strategies({role: [strat1]})
        assert ((await sim.get_info())['role_configuration'][role] ==
                [strat2])
    finally:
        await sim.remove_role(role)

    assert role not in (await sim.get_info())['role_configuration']


@pytest.mark.asyncio
@pytest.mark.egta
async def test_modify_scheduler(egta):
    sims = egta.get_simulators()
    async for sim in sims:
        if next(iter(sim['role_configuration'].values()), None):
            break
    await sims.aclose()
    sched = game = None
    try:
        sched = await sim.create_generic_scheduler(
            '__unique_scheduler__', False, 0, 1, 0, 0)
        await sched.activate()
        await sched.deactivate()

        role = next(iter(sim['role_configuration']))
        strat = sim['role_configuration'][role][0]
        symgrps = [{'role': role, 'strategy': strat, 'count': 1}]
        assignment = api.symgrps_to_assignment(symgrps)

        await sched.add_role(role, 1)

        reqs = (await sched.get_requirements())['scheduling_requirements']
        assert not reqs

        prof = await sched.add_profile(symgrps, 1)
        reqs = (await sched.get_requirements())['scheduling_requirements']
        assert len(reqs) == 1
        assert reqs[0]['requirement'] == 1

        await sched.remove_profile(prof['id'])
        assert (await sched.add_profile(assignment, 2))['id'] == prof['id']
        reqs = (await sched.get_requirements())['scheduling_requirements']
        assert len(reqs) == 1
        assert reqs[0]['requirement'] == 2

        await sched.remove_profile(prof['id'])
        reqs = (await sched.get_requirements())['scheduling_requirements']
        assert not reqs

        assert (await sched.add_profile(symgrps, 3))['id'] == prof['id']
        reqs = (await sched.get_requirements())['scheduling_requirements']
        assert len(reqs) == 1
        assert reqs[0]['requirement'] == 3

        await sched.remove_all_profiles()
        reqs = (await sched.get_requirements())['scheduling_requirements']
        assert not reqs

        await sched.remove_role(role)

        game = await sched.create_game()

    finally:
        if sched is not None:  # pragma: no branch
            await sched.destroy_scheduler()
        if game is not None:  # pragma: no branch
            await game.destroy_game()


@pytest.mark.asyncio
@pytest.mark.egta
async def test_modify_game(egta):
    sims = egta.get_simulators()
    async for sim in sims:
        if next(iter(sim['role_configuration'].values()), None):
            break
    await sims.aclose()
    game = None
    try:
        game = await sim.create_game('__unique_game__', 1)

        summ = await game.get_summary()
        assert not summ['roles']
        assert not summ['profiles']

        role = next(iter(sim['role_configuration']))
        strat = sim['role_configuration'][role][0]
        await game.add_role(role, 1)
        summ = await game.get_summary()
        assert 1 == len(summ['roles'])
        assert summ['roles'][0]['name'] == role
        assert summ['roles'][0]['count'] == 1
        assert not summ['roles'][0]['strategies']

        await game.add_strategies({role: [strat]})
        summ = await game.get_summary()
        assert 1 == len(summ['roles'])
        assert summ['roles'][0]['name'] == role
        assert summ['roles'][0]['count'] == 1
        assert summ['roles'][0]['strategies'] == [strat]

        await game.remove_strategies({role: [strat]})
        summ = await game.get_summary()
        assert 1 == len(summ['roles'])
        assert summ['roles'][0]['name'] == role
        assert summ['roles'][0]['count'] == 1
        assert not summ['roles'][0]['strategies']

        await game.remove_role(role)
        summ = await game.get_summary()
        assert not summ['roles']
        assert not summ['profiles']

    finally:
        if game is not None:  # pragma: no branch
            await game.destroy_game()
