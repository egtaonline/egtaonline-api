"""Python package to mock python interface to egta online api"""
import asyncio
import bisect
import collections
import functools
import inspect
import io
import itertools
import json
import math
import random
import re
import threading
import time
import urllib

import requests
import requests_mock


# The mock server isn't intended to be performant, and isn't multiprocessed, so
# it can over aggressively thread lock without causing any real issues.
_lock = threading.Lock()


def _matcher(method, regex):
    """Sets up a regex matcher"""
    def wrapper(func):
        @functools.wraps(func)
        def wrapped(self, req):
            if req.method != method:
                return
            match = re.match(
                'https://{}/{}$'.format(self.domain, regex), req.url)
            if match is None:
                return
            keywords = match.groupdict().copy()
            if req.text is not None:
                keywords.update(_decode_data(req.text))
            named = {match.span(m) for m in match.groupdict()}
            unnamed = [m for i, m in enumerate(match.groups())
                       if match.span(i) not in named]
            try:
                with _lock:
                    return func(self, *unnamed, **keywords)
            except AssertionError as ex:
                resp = requests.Response()
                resp.status_code = 500
                resp.reason = str(ex)
                resp.url = req.url
                return resp
        wrapped.is_matcher = None
        return wrapped
    return wrapper


class Server(requests_mock.Mocker):
    """A Mock egta online server

    Supports creating simulators and throwing exceptions.
    """

    def __init__(self, domain='egtaonline.eecs.umich.edu', *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.domain = domain

        self._sims = []
        self._sims_by_name = {}
        self._scheds = []
        self._scheds_by_name = {}
        self._games = []
        self._games_by_name = {}
        self._sim_insts = {}
        self._symgrps_tup = {}
        self._profiles = []
        self._folders = []

        self._sim_future = None
        self._sim_queue = asyncio.PriorityQueue()

        self._custom_func = None
        self._custom_times = 0

        for _, method in inspect.getmembers(self, predicate=inspect.ismethod):
            if hasattr(method, 'is_matcher'):
                self.add_matcher(method)
        self.add_matcher(self._custom_matcher)

    async def __aenter__(self):
        super().__enter__()
        assert self._sim_future is None
        assert self._sim_queue.empty()
        self._sim_future = asyncio.ensure_future(self._run_simulations())
        return self

    async def __aexit__(self, *args):
        self._sim_future.cancel()
        try:
            await self._sim_future
        except asyncio.CancelledError:
            pass  # expected
        self._sim_future = None
        while not self._sim_queue.empty():
            self._sim_queue.get_nowait()
        return super().__exit__(*args)

    async def _run_simulations(self):
        """Thread to run simulations at specified time"""
        while True:
            wait_until, _, obs = await self._sim_queue.get()
            timeout = max(wait_until - time.time(), 0)
            await asyncio.sleep(timeout)
            obs._simulate()

    def _get_sim_instance(self, sim_id, configuration):
        """Get the sim instance id for a sim and conf"""
        return self._sim_insts.setdefault(
            (sim_id, frozenset(configuration.items())),
            (len(self._sim_insts), {}))

    def _get_symgrp_id(self, symgrp):
        if symgrp in self._symgrps_tup:
            return self._symgrps_tup[symgrp]
        else:
            sym_id = len(self._symgrps_tup)
            self._symgrps_tup[symgrp] = sym_id
            return sym_id

    def _assign_to_symgrps(self, assign):
        """Turn an assignment string into a role_conf and a size"""
        symgroups = []
        for rolestrat in assign.split('; '):
            role, strats = rolestrat.split(': ', 1)
            for stratstr in strats.split(', '):
                count, strat = stratstr.split(' ', 1)
                rsc = (role, strat, int(count))
                symgroups.append((self._get_symgrp_id(rsc),) + rsc)
        return symgroups

    def _get_sim(self, sid):
        assert 0 <= sid < len(self._sims) and self._sims[sid] is not None, \
            "simulator with id '{:d}' doesn't exist".format(sid)
        return self._sims[sid]

    def _get_sched(self, sid):
        assert 0 <= sid < len(self._scheds) and self._scheds[sid] is not None,\
            "simulator with id '{:d}' doesn't exist".format(sid)
        return self._scheds[sid]

    def _get_prof(self, pid):
        assert 0 <= pid < len(self._profiles), \
            "profile with id '{:d}' doesn't exist".format(pid)
        return self._profiles[pid]

    def _get_folder(self, fid):
        assert 0 <= fid < len(self._folders), \
            "folder with id '{:d}' doesn't exist".format(fid)
        return self._folders[fid]

    def _get_game(self, gid):
        assert 0 <= gid < len(self._games) and self._games[gid] is not None, \
            "game with id '{:d}' doesn't exist".format(gid)
        return self._games[gid]

    def create_simulator(self, name, version, email='egta@mailinator.com',
                         conf={}, delay_dist=lambda: 0):
        """Create a simulator

        Parameters
        ----------
        delay_dist : () -> float
            Generator of how long simulations take to complete in seconds.
        """
        assert version not in self._sims_by_name.get(name, {}), \
            "name already exists"
        sim_id = len(self._sims)
        sim = _Simulator(self, sim_id, name, version, email, conf,
                         delay_dist)
        self._sims.append(sim)
        self._sims_by_name.setdefault(name, {})[version] = sim
        return sim_id

    def custom_response(self, func, times=1):
        """Return a custom response.

        The next `times` requests will return the custom response instead
        of a valid result. The function can raise exceptions or do
        anything else to mishandle the request.
        """
        self._custom_func = func
        self._custom_times = times

    # -------------------------
    # Request matcher functions
    # -------------------------

    def _custom_matcher(self, _):
        if self._custom_times > 0:
            self._custom_times -= 1
            return _resp(self._custom_func())

    @_matcher('GET', '')
    def _session(self, auth_token):
        return _resp()

    @_matcher('GET', 'api/v3/simulators')
    def _simulator_all(self):
        return _json_resp({"simulators": [
            sim.get_all() for sim in self._sims if sim is not None]})

    @_matcher('GET', 'api/v3/simulators/(\d+).json')
    def _simulator_get(self, sid):
        return _json_resp(self._get_sim(int(sid)).get_info())

    @_matcher('POST', 'api/v3/simulators/(\d+)/add_role.json')
    def _simulator_add_role(self, sid, role):
        self._get_sim(int(sid)).add_role(role)
        return _resp()

    @_matcher('POST', 'api/v3/simulators/(\d+)/remove_role.json')
    def _simulator_remove_role(self, sid, role):
        self._get_sim(int(sid)).remove_role(role)
        return _resp()

    @_matcher('POST', 'api/v3/simulators/(\d+)/add_strategy.json')
    def _simulator_add_strategy(self, sid, role, strategy):
        self._get_sim(int(sid)).add_strategy(role, strategy)
        return _resp()

    @_matcher('POST', 'api/v3/simulators/(\d+)/remove_strategy.json')
    def _simulator_remove_strategy(self, sid, role, strategy):
        self._get_sim(int(sid)).remove_strategy(role, strategy)
        return _resp()

    @_matcher('POST', 'api/v3/generic_schedulers')
    def _scheduler_create(self, scheduler):
        name = scheduler['name']
        assert name not in self._scheds_by_name, \
            "scheduler named {} already exists".format(name)
        sim = self._get_sim(int(scheduler['simulator_id']))
        conf = sim.configuration
        conf.update(scheduler.get('configuration', {}))

        sched_id = len(self._scheds)
        sched = _Scheduler(
            sim, sched_id, name, int(scheduler['size']),
            int(scheduler['observations_per_simulation']),
            int(scheduler['time_per_observation']),
            int(scheduler['process_memory']),
            bool(int(scheduler['active'])), int(scheduler['nodes']),
            conf)
        self._scheds.append(sched)
        self._scheds_by_name[name] = sched
        return _json_resp(sched.get_info())

    @_matcher('GET', 'api/v3/generic_schedulers')
    def _scheduler_all(self):
        return _json_resp({'generic_schedulers': [
            s.get_info() for s in self._scheds if s is not None]})

    @_matcher('GET', 'api/v3/schedulers/(\d+).json')
    def _scheduler_get(self, sid, granularity=None):
        sched = self._get_sched(int(sid))
        return _json_resp(sched.get_requirements()
                          if granularity == 'with_requirements'
                          else sched.get_info())

    @_matcher('PUT', 'api/v3/generic_schedulers/(\d+).json')
    def _scheduler_update(self, sid, scheduler):
        self._get_sched(int(sid)).update(**scheduler)
        return _resp()

    @_matcher('POST', 'api/v3/generic_schedulers/(\d+)/add_role.json')
    def _scheduler_add_role(self, sid, role, count):
        self._get_sched(int(sid)).add_role(role, int(count))
        return _resp()

    @_matcher('POST', 'api/v3/generic_schedulers/(\d+)/remove_role.json')
    def _scheduler_remove_role(self, sid, role):
        self._get_sched(int(sid)).remove_role(role)
        return _resp()

    @_matcher('POST', 'api/v3/generic_schedulers/(\d+)/add_profile.json')
    def _scheduler_add_profile(self, sid, assignment, count):
        return _json_resp(self._get_sched(
            int(sid)).add_profile(assignment, int(count)).get_new())

    @_matcher('POST', 'api/v3/generic_schedulers/(\d+)/remove_profile.json')
    def _scheduler_remove_profile(self, sid, profile_id):
        self._get_sched(int(sid)).remove_profile(int(profile_id))
        return _resp()

    @_matcher('DELETE', 'api/v3/generic_schedulers/(\d+).json')
    def _scheduler_destroy(self, sid):
        self._get_sched(int(sid)).destroy()
        return _resp()

    @_matcher('GET', 'api/v3/profiles/(\d+).json')
    def _profile_get(self, pid, granularity='structure'):
        prof = self._get_prof(int(pid))
        if granularity == 'structure':
            return _json_resp(prof.get_structure())
        elif granularity == 'summary':
            return _json_resp(prof.get_summary())
        elif granularity == 'observations':
            return _json_resp(prof.get_observations())
        elif granularity == 'full':
            return _json_resp(prof.get_full())
        else:
            assert False  # pragma: no cover

    @_matcher('GET', 'simulations')
    def _simulation_all(self, direction='DESC', page='1', sort='job_id'):
        desc = direction == 'DESC'
        assert sort in _sim_keys, "unknown sort key"
        column = _sim_keys[sort]
        if column in {'folder', 'profile', 'simulator'}:
            sims = sorted(self._folders, key=lambda f: getattr(f, column),
                          reverse=desc)
        elif desc:
            sims = self._folders[::-1]
        else:
            sims = self._folders

        page = int(page)
        sims = sims[25 * (page - 1): 25 * page]

        if not sims:
            return _html_resp()
        else:
            return _html_resp(
                '<tbody>' + '\n'.join(f.get_all() for f in sims) + '</tbody>')

    @_matcher('GET', 'simulations/(\d+)')
    def _simulation_get(self, fid):
        return _html_resp(self._get_folder(int(fid)).get_info())

    @_matcher('POST', 'games')
    def _game_create(self, auth_token, game, selector):
        name = game['name']
        assert name not in self._games_by_name, \
            "game named '{}' already exists".format(name)
        sim = self._get_sim(int(selector['simulator_id']))
        conf = sim.configuration
        conf.update(selector.get('configuration', {}))

        game_id = len(self._games)
        game = _Game(sim, game_id, name, int(game['size']), conf)
        self._games.append(game)
        self._games_by_name[name] = game
        return _html_resp('<div id=game_{:d}></div>'.format(game_id))

    @_matcher('GET', 'api/v3/games')
    def _game_all(self):
        return _json_resp({'games': [
            game.get_all() for game in self._games if game is not None]})

    @_matcher('GET', 'games/(\d+).json')
    def _game_get(self, gid, granularity='structure'):
        game = self._get_game(int(gid))
        if granularity == 'structure':
            # This extra dump is a quirk of the api
            return _json_resp(json.dumps(game.get_structure()))
        elif granularity == 'summary':
            return _json_resp(game.get_summary())
        elif granularity == 'observations':
            return _json_resp(game.get_observations())
        elif granularity == 'full':
            return _json_resp(game.get_full())
        else:
            assert False  # pragma: no cover

    @_matcher('POST', 'api/v3/games/(\d+)/add_role.json')
    def _game_add_role(self, gid, role, count):
        self._get_game(int(gid)).add_role(role, int(count))
        return _resp()

    @_matcher('POST', 'api/v3/games/(\d+)/remove_role.json')
    def _game_remove_role(self, gid, role):
        self._get_game(int(gid)).remove_role(role)
        return _resp()

    @_matcher('POST', 'api/v3/games/(\d+)/add_strategy.json')
    def _game_add_strategy(self, gid, role, strategy):
        self._get_game(int(gid)).add_strategy(role, strategy)
        return _resp()

    @_matcher('POST', 'api/v3/games/(\d+)/remove_strategy.json')
    def _game_remove_strategy(self, gid, role, strategy):
        self._get_game(int(gid)).remove_strategy(role, strategy)
        return _resp()

    @_matcher('POST', 'games/(\d+)')
    def _game_destroy(self, gid, _method, auth_token):
        assert _method == 'delete', "unknown method {}".format(_method)
        self._get_game(int(gid)).destroy()
        return _resp()

    @_matcher('GET', 'uploads/simulator/source/(\d+)/([-\w]+).zip')
    def _zip_fetch(self, sim_id, sim_fullname):
        sim = self._get_sim(int(sim_id))
        assert sim.fullname == sim_fullname
        return _resp('fake zip')


def _dict(item, keys, **extra):
    """Convert item to dict"""
    return dict(((k, getattr(item, k)) for k in keys), **extra)


def _resp(text=''):
    """Construct a response with plain text"""
    resp = requests.Response()
    resp.status_code = 200
    resp.encoding = 'utf8'
    resp.raw = io.BytesIO(text.encode('utf8'))
    return resp


def _json_resp(json_data):
    """Construct a response with various data types"""
    return _resp(json.dumps(json_data))


def _html_resp(body=''):
    """Construct a response with various data types"""
    return _resp('<html><head></head><body>{}</body></html>'.format(body))


def _decode_data(text):
    """Decode put request body"""
    result = {}
    for key_val in text.split('&'):
        key, val = map(urllib.parse.unquote_plus, key_val.split('='))
        subres = result
        ind = key.find('[')
        while 0 < ind:
            subres = subres.setdefault(key[:ind], {})
            key = key[ind + 1:-1]
            ind = key.find('[')
        subres[key] = val
    return result


_sim_keys = {
    'state': 'state',
    'profiles.assignment': 'profile',
    'simulator_fullname': 'simulator',
    'id': 'folder',
    'job_id': 'job',
}


class _Simulator(object):
    def __init__(self, server, sid, name, version, email, conf, delay_dist):
        self._server = server
        self.id = sid
        self.name = name
        self.version = version
        self.fullname = '{}-{}'.format(name, version)
        self.email = email

        self._conf = conf
        self._role_conf = {}
        current_time = _get_time_str()
        self.created_at = current_time
        self.updated_at = current_time
        self._delay_dist = delay_dist
        self._source = '/uploads/simulator/source/{:d}/{}.zip'.format(
            self.id, self.fullname)
        self.url = 'https://{}/simulators/{:d}'.format(
            self._server.domain, sid)

    @property
    def configuration(self):
        return self._conf.copy()

    @property
    def role_configuration(self):
        return {role: strats.copy() for role, strats
                in self._role_conf.items()}

    @property
    def source(self):
        return {'url': self._source}

    def add_role(self, role):
        self._role_conf.setdefault(role, [])
        self.updated_at = _get_time_str()

    def remove_role(self, role):
        if self._role_conf.pop(role, None) is not None:
            self.updated_at = _get_time_str()

    def add_strategy(self, role, strat):
        strats = self._role_conf[role]
        strats.insert(bisect.bisect_left(strats, strat), strat)
        self.updated_at = _get_time_str()

    def remove_strategy(self, role, strategy):
        try:
            self._role_conf[role].remove(strategy)
            self.updated_at = _get_time_str()
        except (KeyError, ValueError):
            pass  # don't care

    def get_all(self):
        return _dict(
            self,
            ['configuration', 'created_at', 'email', 'id', 'name',
             'role_configuration', 'source', 'updated_at', 'version'])

    def get_info(self):
        return _dict(
            self,
            ['configuration', 'created_at', 'email', 'id', 'name',
             'role_configuration', 'source', 'updated_at', 'url', 'version'])


class _Scheduler(object):
    def __init__(self, sim, sid, name, size, obs_per_sim, time_per_obs,
                 process_memory, active, nodes, conf):
        self.id = sid
        self.name = name
        self.active = active
        self.nodes = nodes
        self.default_observation_requirement = 0
        self.observations_per_simulation = obs_per_sim
        self.process_memory = process_memory
        self.simulator_instance_id, self._assignments = (
            sim._server._get_sim_instance(sim.id, conf))
        self.size = size
        self.time_per_observation = time_per_obs
        current_time = _get_time_str()
        self.created_at = current_time
        self.updated_at = current_time
        self.simulator_id = sim.id
        self.url = 'https://{}/generic_schedulers/{:d}'.format(
            sim._server.domain, sid)
        self.type = 'GenericScheduler'

        self._destroyed = False
        self._sim = sim
        self._server = sim._server
        self._conf = conf
        self._role_conf = {}
        self._reqs = {}

    @property
    def configuration(self):
        return [[key, str(value)] for key, value in self._conf.items()]

    @property
    def scheduling_requirements(self):
        return [_dict(prof, ['current_count'], requirement=count,
                      profile_id=prof.id)
                for prof, count in self._reqs.items()]

    def update(self, name=None, **kwargs):
        """Update the parameters of a given scheduler"""
        # FIXME Technically this should allow updating the configuration and
        # hence the simulator instance id
        kwargs = {k: int(v) for k, v in kwargs.items()}
        if 'active' in kwargs:
            kwargs['active'] = bool(kwargs['active'])
        if not self.active and kwargs['active']:
            for prof, count in self._reqs.items():
                prof.update(count)
        # FIXME Only for valid keys
        for key, val in kwargs.items():
            setattr(self, key, val)
        self.updated_at = _get_time_str()

    def add_role(self, role, count):
        assert role in self._sim._role_conf
        assert role not in self._role_conf
        assert sum(self._role_conf.values()) + count <= self.size
        self._role_conf[role] = count
        self.updated_at = _get_time_str()

    def remove_role(self, role):
        """Remove a role from the scheduler"""
        if self._role_conf.pop(role, None) is not None:
            self.updated_at = _get_time_str()

    def destroy(self):
        self._server._scheds_by_name.pop(self.name)
        self._server._scheds[self.id] = None
        self._destroyed = True

    def get_info(self):
        return _dict(
            self,
            ['active', 'created_at', 'default_observation_requirement', 'id',
             'name', 'nodes', 'observations_per_simulation', 'process_memory',
             'simulator_instance_id', 'size', 'time_per_observation',
             'updated_at'])

    def get_requirements(self):
        return _dict(
            self,
            ['active', 'configuration', 'default_observation_requirement',
             'id', 'name', 'nodes', 'observations_per_simulation',
             'process_memory', 'scheduling_requirements', 'simulator_id',
             'size', 'time_per_observation', 'type', 'url'])

    def get_profile(self, assignment):
        if assignment in self._assignments:
            return self._assignments[assignment]
        else:
            prof_id = len(self._server._profiles)
            prof = _Profile(self._sim, prof_id, assignment,
                            self.simulator_instance_id)
            for _, role, strat, _ in prof._symgrps:
                assert role in self._role_conf
                assert strat in self._sim._role_conf[role]
            assert prof._role_conf == self._role_conf
            self._server._profiles.append(prof)
            self._assignments[assignment] = prof
            return prof

    def add_profile(self, assignment, count):
        """Add a profile to the scheduler"""
        prof = self.get_profile(assignment)

        if prof not in self._reqs:
            # XXX This is how egta online behaves, but it seems non ideal
            self._reqs[prof] = count
            self.updated_at = _get_time_str()
            if self.active:
                prof.update(count)

        return prof

    def remove_profile(self, pid):
        try:
            prof = self._server._profiles[pid]
            if self._reqs.pop(prof, None) is not None:
                self.updated_at = _get_time_str()
        except IndexError:
            pass  # don't care


class _Profile(object):
    def __init__(self, sim, pid, assignment, inst_id):
        self.id = pid
        self.assignment = assignment
        self.simulator_instance_id = inst_id
        current_time = _get_time_str()
        self.created_at = current_time
        self.updated_at = current_time

        self._sim = sim
        self._server = sim._server
        self._symgrps = self._server._assign_to_symgrps(assignment)
        self._role_conf = collections.Counter()
        for _, role, _, count in self._symgrps:
            self._role_conf[role] += count
        self.size = sum(self._role_conf.values())
        self._obs = []
        self._scheduled = 0

    @property
    def observations_count(self):
        return len(self._obs)

    @property
    def current_count(self):
        return self.observations_count

    @property
    def role_configuration(self):
        return self._role_conf.copy()

    @property
    def symmetry_groups(self):
        return [{'id': gid, 'role': role, 'strategy': strat, 'count': count}
                for gid, role, strat, count in self._symgrps]

    def update(self, count):
        if self._scheduled < count:
            self.updated_at = _get_time_str()
        for _ in range(count - self._scheduled):
            folder = len(self._server._folders)
            obs = _Observation(self, folder)
            self._server._folders.append(obs)
            sim_time = time.time() + self._sim._delay_dist()
            self._server._sim_queue.put_nowait((sim_time, obs.id, obs))
            self._scheduled += 1

    def get_new(self):
        return _dict(
            self,
            ['assignment', 'created_at', 'id', 'observations_count',
             'role_configuration', 'simulator_instance_id', 'size',
             'updated_at', ])

    def get_structure(self):
        role_conf = {r: str(c) for r, c in self._role_conf.items()}
        return _dict(
            self,
            ['assignment', 'created_at', 'id', 'observations_count',
             'simulator_instance_id', 'size', 'updated_at'],
            role_configuration=role_conf)

    def get_summary(self):
        if self._obs:
            payoffs = {
                gid: (mean, stddev)
                for gid, mean, stddev
                in _mean_id(itertools.chain.from_iterable(
                    obs._pays for obs in self._obs))}
        else:
            payoffs = {gid: (None, None) for gid, _, _, _
                       in self._symgrps}

        symgrps = []
        for gid, role, strat, count in self._symgrps:
            pay, pay_sd = payoffs[gid]
            symgrps.append({
                'id': gid,
                'role': role,
                'strategy': strat,
                'count': count,
                'payoff': pay,
                'payoff_sd': pay_sd,
            })
        return _dict(
            self,
            ['id', 'simulator_instance_id', 'observations_count'],
            symmetry_groups=symgrps)

    def get_observations(self):
        observations = [{
            'extended_features': {},
            'features': {},
            'symmetry_groups': [{
                'id': sid,
                'payoff': pay,
                'payoff_sd': None,
            } for sid, pay, _ in _mean_id(obs._pays)]
        } for obs in self._obs]
        return _dict(
            self,
            ['id', 'simulator_instance_id', 'symmetry_groups'],
            observations=observations)

    def get_full(self):
        observations = [{
            'extended_features': {},
            'features': {},
            'players': [{
                'e': {},
                'f': {},
                'p': pay,
                'sid': sid,
            } for sid, pay in obs._pays]
        } for obs in self._obs]
        return _dict(
            self,
            ['id', 'simulator_instance_id', 'symmetry_groups'],
            observations=observations)


class _Observation(object):
    def __init__(self, prof, oid):
        self.id = oid
        self.folder = oid
        self.folder_number = oid
        self.job = 'Not specified'
        self.profile = prof.assignment
        self.simulator = prof._sim.fullname
        self.simulator_fullname = self.simulator
        self.simulator_instance_id = prof.simulator_instance_id
        self.size = prof.size
        self.error_message = ''

        self._prof = prof
        self._server = prof._server
        self._pays = tuple(itertools.chain.from_iterable(
            ((gid, random.random()) for _ in range(count))
            for gid, _, _, count in prof._symgrps))
        self._simulated = False

    @property
    def state(self):
        return 'complete' if self._simulated else 'running'

    def _simulate(self):
        assert not self._simulated
        self._simulated = True
        self._prof._obs.append(self)

    def get_all(self):
        return (
            '<tr>' + ''.join(
                '<td>{}</td>'.format(d) for d
                in [self.state, self.profile, self.simulator, self.folder,
                    'n/a'])
            + '</tr>')

    def get_info(self):
        return (
            '<div class="show_for simulation">' +
            '\n'.join(
                '<p>{}: {}</p>'.format(
                    key, getattr(self, key.lower().replace(' ', '_')))
                for key in ['Simulator fullname', 'Profile', 'State', 'Size',
                            'Folder number', 'Job', 'Error message']) +
            '</div>')


class _Game(object):
    def __init__(self, sim, gid, name, size, conf):
        self.id = gid
        self.name = name
        self.simulator_instance_id, self._assignments = (
            sim._server._get_sim_instance(sim.id, conf))
        self.size = size
        current_time = _get_time_str()
        self.created_at = current_time
        self.updated_at = current_time
        self.url = 'https://{}/games/{:d}'.format(sim._server.domain, gid)
        self.simulator_fullname = sim.fullname
        self.subgames = None

        self._sim = sim
        self._server = sim._server
        self._conf = conf
        self._role_conf = {}
        self._destroyed = False

    @property
    def configuration(self):
        return [[k, str(v)] for k, v in self._conf.items()]

    @property
    def roles(self):
        return [{'name': r, 'count': c, 'strategies': s, } for r, (s, c)
                in sorted(self._role_conf.items())]

    def add_role(self, role, count):
        """Adds a role to the game"""
        assert (sum(c for _, c in self._role_conf.values()) + count <=
                self.size)
        assert role not in self._role_conf, "can't add an existing role"
        assert role in self._sim._role_conf
        self._role_conf[role] = ([], count)
        self.updated_at = _get_time_str()

    def remove_role(self, role):
        """Removes a role from the game"""
        if self._role_conf.pop(role, None) is not None:
            self.updated_at = _get_time_str()

    def add_strategy(self, role, strat):
        """Adds a strategy to the game"""
        strats, _ = self._role_conf[role]
        assert strat in self._sim._role_conf[role]
        strats.insert(bisect.bisect_left(strats, strat), strat)
        self.updated_at = _get_time_str()

    def remove_strategy(self, role, strat):
        """Removes a strategy from the game"""
        try:
            self._role_conf[role][0].remove(strat)
            self.updated_at = _get_time_str()
        except ValueError:
            pass  # don't care

    def destroy(self):
        self._server._games_by_name.pop(self.name)
        self._server._games[self.id] = None
        self._destroyed = True

    def get_data(self, func, keys):
        strats = {r: set(s) for r, (s, _)
                  in self._role_conf.items()}
        counts = {r: c for r, (_, c) in self._role_conf.items()}
        profs = []
        for prof in self._assignments.values():
            # Assignments maps to all assignments in a sim_instance_id, so we
            # must filter by profiles that actually match
            if not prof._obs:
                continue  # no data
            counts_left = counts.copy()
            for _, role, strat, count in prof._symgrps:
                if strat not in strats.get(role, ()):
                    continue  # invalid profile
                counts_left[role] -= count
            if all(c == 0 for c in counts_left.values()):
                jprof = func(prof)
                for k in set(jprof.keys()).difference(keys):
                    jprof.pop(k)
                profs.append(jprof)

        return _dict(
            self,
            ['id', 'configuration', 'roles', 'simulator_fullname', 'name',
             'url'],
            profiles=profs)

    def get_all(self):
        return _dict(
            self,
            ['created_at', 'id', 'name', 'simulator_instance_id', 'size',
             'subgames', 'updated_at'])

    def get_structure(self):
        return _dict(
            self,
            ['created_at', 'id', 'name', 'simulator_instance_id', 'size',
             'subgames', 'updated_at', 'url'])

    def get_summary(self):
        return self.get_data(
            _Profile.get_summary,
            ['id', 'observations_count', 'symmetry_groups'])

    def get_observations(self):
        return self.get_data(
            _Profile.get_observations,
            ['id', 'observations', 'symmetry_groups'])

    def get_full(self):
        return self.get_data(
            _Profile.get_full,
            ['id', 'observations', 'symmetry_groups'])


def server(domain='egtaonline.eecs.umich.edu', *args, **kwargs):
    return Server(domain, *args, **kwargs)


def symgrps_to_assignment(symmetry_groups):
    """Converts a symmetry groups structure to an assignemnt string"""
    roles = {}
    for symgrp in symmetry_groups:
        role, strat, count = symgrp['role'], symgrp[
            'strategy'], symgrp['count']
        roles.setdefault(role, []).append((strat, count))
    return '; '.join(
        '{}: {}'.format(role, ', '.join('{:d} {}'.format(count, strat)
                                        for strat, count in sorted(strats)
                                        if count > 0))
        for role, strats in sorted(roles.items()))


def _get_time_str():
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z')


def _mean_id(iterator):
    means = {}
    for sid, pay in iterator:
        dat = means.setdefault(sid, [0, 0.0, 0.0])
        old_mean = dat[1]
        dat[0] += 1
        dat[1] += (pay - dat[1]) / dat[0]
        dat[2] += (pay - old_mean) * (pay - dat[1])
    return ((sid, m, math.sqrt(s / (c - 1)) if c > 1 else None)
            for sid, (c, m, s) in means.items())
