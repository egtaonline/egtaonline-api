"""Python package to mock python interface to egta online api"""
# pylint: disable=too-many-lines
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
_LOCK = threading.Lock()


def _matcher(method, regex):
    """Sets up a regex matcher"""
    def wrapper(func):
        """Wrapper for matching function"""
        @functools.wraps(func)
        def wrapped(self, req):
            """Wrapped function to handle mock requests"""
            if req.method != method:
                return None
            match = re.match(
                'https://{}/{}$'.format(self.domain, regex), req.url)
            if match is None:
                return None
            keywords = match.groupdict().copy()
            if req.text is not None:
                keywords.update(_decode_data(req.text))
            named = {match.span(m) for m in match.groupdict()}
            unnamed = [m for i, m in enumerate(match.groups())
                       if match.span(i) not in named]
            try:
                with _LOCK:
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


class _ServerData(requests_mock.Mocker): # pylint: disable=too-many-instance-attributes
    """A Mock egta online server with data

    When entered this mocks out requests and instead handles them with internal
    data to replicate what the egta online server would be doing."""

    def __init__(self, domain, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.domain = domain
        self.loop = asyncio.get_event_loop()

        self._sims = []
        self._sims_by_name = {}
        self.scheds = []
        self.scheds_by_name = {}
        self.games = []
        self.games_by_name = {}
        self._sim_insts = {}
        self._symgrps_tup = {}
        self.profiles = []
        self.folders = []

        self._sim_future = None
        self.sim_queue = asyncio.PriorityQueue()

        self._custom_func = None
        self._custom_times = 0

        for _, method in inspect.getmembers(self, predicate=inspect.ismethod):
            if hasattr(method, 'is_matcher'):
                self.add_matcher(method)
        self.add_matcher(self._custom_matcher)

    async def __aenter__(self):
        super().__enter__()
        assert self._sim_future is None
        assert self.sim_queue.empty()
        self._sim_future = asyncio.ensure_future(self._run_simulations())
        return self

    async def __aexit__(self, typ, value, traceback):
        self._sim_future.cancel()
        try:
            await self._sim_future
        except asyncio.CancelledError:
            pass  # expected
        self._sim_future = None
        while not self.sim_queue.empty():
            self.sim_queue.get_nowait()
        return super().__exit__(typ, value, traceback)

    async def _run_simulations(self):
        """Thread to run simulations at specified time"""
        while True:
            wait_until, _, obs = await self.sim_queue.get()
            timeout = max(wait_until - time.time(), 0)
            await asyncio.sleep(timeout)
            obs.simulate()

    def get_sim_instance(self, sim_id, configuration):
        """Get the sim instance id for a sim and conf"""
        return self._sim_insts.setdefault(
            (sim_id, frozenset(configuration.items())),
            (len(self._sim_insts), {}))

    def _get_symgrp_id(self, symgrp):
        """Get symgroup id"""
        if symgrp in self._symgrps_tup:
            return self._symgrps_tup[symgrp]
        sym_id = len(self._symgrps_tup)
        self._symgrps_tup[symgrp] = sym_id
        return sym_id

    def assign_to_symgrps(self, assign):
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
        """Get simulator"""
        assert 0 <= sid < len(self._sims) and self._sims[sid] is not None, \
            "simulator with id '{:d}' doesn't exist".format(sid)
        return self._sims[sid]

    def _get_sched(self, sid):
        """Get scheduler"""
        assert 0 <= sid < len(self.scheds) and self.scheds[sid] is not None,\
            "simulator with id '{:d}' doesn't exist".format(sid)
        return self.scheds[sid]

    def _get_prof(self, pid):
        """Get profile"""
        assert 0 <= pid < len(self.profiles), \
            "profile with id '{:d}' doesn't exist".format(pid)
        return self.profiles[pid]

    def _get_folder(self, fid):
        """Get folder"""
        assert 0 <= fid < len(self.folders), \
            "folder with id '{:d}' doesn't exist".format(fid)
        return self.folders[fid]

    def _get_game(self, gid):
        """Get game"""
        assert 0 <= gid < len(self.games) and self.games[gid] is not None, \
            "game with id '{:d}' doesn't exist".format(gid)
        return self.games[gid]

    def create_simulator(self, name, version, email, conf, delay_dist): # pylint: disable=too-many-arguments
        """Create a simulator"""
        assert version not in self._sims_by_name.get(name, {}), \
            'name already exists'
        sim_id = len(self._sims)
        sim = _Simulator(self, sim_id, name, version, email, conf,
                         delay_dist)
        self._sims.append(sim)
        self._sims_by_name.setdefault(name, {})[version] = sim
        return sim_id

    def custom_response(self, func, times):
        """Return a custom response"""
        self._custom_func = func
        self._custom_times = times

    # -------------------------
    # Request matcher functions
    # -------------------------

    def _custom_matcher(self, _):
        """Custom matcher for arbitrary responses"""
        if self._custom_times > 0: # pylint: disable=no-else-return
            self._custom_times -= 1
            return _resp(self._custom_func())
        else:
            return None

    @_matcher('GET', '')
    def _session(self, auth_token): # pylint: disable=no-self-use
        """Start session"""
        assert isinstance(auth_token, str)
        return _resp()

    @_matcher('GET', 'api/v3/simulators')
    def _simulator_all(self):
        """Get simulation creation"""
        return _json_resp({'simulators': [
            sim.get_all() for sim in self._sims if sim is not None]})

    @_matcher('GET', r'api/v3/simulators/(\d+).json')
    def _simulator_get(self, sid):
        """Get simulator"""
        return _json_resp(self._get_sim(int(sid)).get_info())

    @_matcher('POST', r'api/v3/simulators/(\d+)/add_role.json')
    def _simulator_add_role(self, sid, role):
        """Add simulator role"""
        self._get_sim(int(sid)).add_role(role)
        return _resp()

    @_matcher('POST', r'api/v3/simulators/(\d+)/remove_role.json')
    def _simulator_remove_role(self, sid, role):
        """Remove simulator role"""
        self._get_sim(int(sid)).remove_role(role)
        return _resp()

    @_matcher('POST', r'api/v3/simulators/(\d+)/add_strategy.json')
    def _simulator_add_strategy(self, sid, role, strategy):
        """Add simulator strategy"""
        self._get_sim(int(sid)).add_strategy(role, strategy)
        return _resp()

    @_matcher('POST', r'api/v3/simulators/(\d+)/remove_strategy.json')
    def _simulator_remove_strategy(self, sid, role, strategy):
        """Remove scheduler strategy"""
        self._get_sim(int(sid)).remove_strategy(role, strategy)
        return _resp()

    @_matcher('POST', 'api/v3/generic_schedulers')
    def _scheduler_create(self, scheduler):
        """Create scheduler"""
        name = scheduler['name']
        assert name not in self.scheds_by_name, \
            'scheduler named {} already exists'.format(name)
        sim = self._get_sim(int(scheduler['simulator_id']))
        conf = scheduler.get('configuration', {})

        sched_id = len(self.scheds)
        sched = _Scheduler(
            sim, sched_id, name, int(scheduler['size']),
            int(scheduler['observations_per_simulation']),
            int(scheduler['time_per_observation']),
            int(scheduler['process_memory']),
            bool(int(scheduler['active'])), int(scheduler['nodes']),
            conf)
        self.scheds.append(sched)
        self.scheds_by_name[name] = sched
        return _json_resp(sched.get_info())

    @_matcher('GET', 'api/v3/generic_schedulers')
    def _scheduler_all(self):
        """Get scheduler creation"""
        return _json_resp({'generic_schedulers': [
            s.get_info() for s in self.scheds if s is not None]})

    @_matcher('GET', r'api/v3/schedulers/(\d+).json')
    def _scheduler_get(self, sid, granularity=None):
        """Get scheduler"""
        sched = self._get_sched(int(sid))
        return _json_resp(sched.get_requirements()
                          if granularity == 'with_requirements'
                          else sched.get_info())

    @_matcher('PUT', r'api/v3/generic_schedulers/(\d+).json')
    def _scheduler_update(self, sid, scheduler):
        """Scheduler update"""
        self._get_sched(int(sid)).update(**scheduler)
        return _resp()

    @_matcher('POST', r'api/v3/generic_schedulers/(\d+)/add_role.json')
    def _scheduler_add_role(self, sid, role, count):
        """Scheduler add role"""
        self._get_sched(int(sid)).add_role(role, int(count))
        return _resp()

    @_matcher('POST', r'api/v3/generic_schedulers/(\d+)/remove_role.json')
    def _scheduler_remove_role(self, sid, role):
        """Scheduler remove role"""
        self._get_sched(int(sid)).remove_role(role)
        return _resp()

    @_matcher('POST', r'api/v3/generic_schedulers/(\d+)/add_profile.json')
    def _scheduler_add_profile(self, sid, assignment, count):
        """Scheduler add profile"""
        return _json_resp(self._get_sched(
            int(sid)).add_profile(assignment, int(count)).get_new())

    @_matcher('POST', r'api/v3/generic_schedulers/(\d+)/remove_profile.json')
    def _scheduler_remove_profile(self, sid, profile_id):
        """Remove scheduler profile"""
        self._get_sched(int(sid)).remove_profile(int(profile_id))
        return _resp()

    @_matcher('DELETE', r'api/v3/generic_schedulers/(\d+).json')
    def _scheduler_destroy(self, sid):
        """Destroy scheduler"""
        self._get_sched(int(sid)).destroy()
        return _resp()

    @_matcher('GET', r'api/v3/profiles/(\d+).json')
    def _profile_get(self, pid, granularity='structure'):
        """Get profile"""
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
            raise AssertionError('should never get here') # pragma: no cover

    @_matcher('GET', 'simulations')
    def _simulation_all(
            self, direction='DESC', page='1', sort='job_id', search=''):
        """Get simulation creation"""
        assert isinstance(search, str)
        desc = direction == 'DESC'
        assert sort in _SIM_KEYS, 'unknown sort key'
        column = _SIM_KEYS[sort]
        if column in {'folder', 'profile', 'simulator'}:
            sims = sorted(self.folders, key=lambda f: getattr(f, column),
                          reverse=desc)
        elif desc:
            sims = self.folders[::-1]
        else:
            sims = self.folders

        page = int(page)
        sims = sims[25 * (page - 1): 25 * page]

        if not sims: # pylint: disable=no-else-return
            return _html_resp()
        else:
            return _html_resp(
                '<tbody>' + '\n'.join(f.get_all() for f in sims) + '</tbody>')

    @_matcher('GET', r'simulations/(\d+)')
    def _simulation_get(self, fid):
        """Get simulation"""
        return _html_resp(self._get_folder(int(fid)).get_info())

    @_matcher('POST', 'games')
    def _game_create(self, auth_token, game, selector):
        """Game create"""
        assert isinstance(auth_token, str)
        name = game['name']
        assert name not in self.games_by_name, \
            "game named '{}' already exists".format(name)
        sim = self._get_sim(int(selector['simulator_id']))
        conf = selector.get('configuration', {})

        game_id = len(self.games)
        game = _Game(sim, game_id, name, int(game['size']), conf)
        self.games.append(game)
        self.games_by_name[name] = game
        return _html_resp('<div id=game_{:d}></div>'.format(game_id))

    @_matcher('GET', 'api/v3/games')
    def _game_all(self):
        """Get game creation"""
        return _json_resp({'games': [
            game.get_all() for game in self.games if game is not None]})

    @_matcher('GET', r'games/(\d+).json')
    def _game_get(self, gid, granularity='structure'):
        """Get game"""
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
            raise AssertionError('should never get here') # pragma: no cover

    @_matcher('POST', r'api/v3/games/(\d+)/add_role.json')
    def _game_add_role(self, gid, role, count):
        """Add game role"""
        self._get_game(int(gid)).add_role(role, int(count))
        return _resp()

    @_matcher('POST', r'api/v3/games/(\d+)/remove_role.json')
    def _game_remove_role(self, gid, role):
        """Remove game role"""
        self._get_game(int(gid)).remove_role(role)
        return _resp()

    @_matcher('POST', r'api/v3/games/(\d+)/add_strategy.json')
    def _game_add_strategy(self, gid, role, strategy):
        """Add game strategy"""
        self._get_game(int(gid)).add_strategy(role, strategy)
        return _resp()

    @_matcher('POST', r'api/v3/games/(\d+)/remove_strategy.json')
    def _game_remove_strategy(self, gid, role, strategy):
        """Remove game strategy"""
        self._get_game(int(gid)).remove_strategy(role, strategy)
        return _resp()

    @_matcher('POST', r'games/(\d+)')
    def _game_destroy(self, gid, _method, auth_token):
        """Destroy game"""
        assert isinstance(auth_token, str)
        assert _method == 'delete', 'unknown method {}'.format(_method)
        self._get_game(int(gid)).destroy()
        return _resp()

    @_matcher('GET', r'uploads/simulator/source/(\d+)/([-\w]+).zip')
    def _zip_fetch(self, sim_id, sim_name):
        """Get zip"""
        sim = self._get_sim(int(sim_id))
        assert sim.name == sim_name
        return _resp('fake zip')


class _Server(object):
    """A Mock egta online server

    Supports creating simulators and throwing exceptions.
    """
    def __init__(self, domain, **kwargs):
        self._data = _ServerData(domain, **kwargs)

    async def __aenter__(self):
        await self._data.__aenter__()
        return self

    async def __aexit__(self, typ, value, traceback):
        await self._data.__aexit__(typ, value, traceback)

    def create_simulator( # pylint: disable=too-many-arguments
            self, name, version, email='egta@mailinator.com', conf=None,
            delay_dist=lambda: 0):
        """Create a simulator

        Parameters
        ----------
        delay_dist : () -> float
            Generator of how long simulations take to complete in seconds.
        """
        return self._data.create_simulator(
            name, version, email, conf or {}, delay_dist)

    def custom_response(self, func, times=1):
        """Return a custom response.

        The next `times` requests will return the custom response instead
        of a valid result. The function can raise exceptions or do
        anything else to mishandle the request.
        """
        return self._data.custom_response(func, times)


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
        while ind > 0:
            subres = subres.setdefault(key[:ind], {})
            key = key[ind + 1:-1]
            ind = key.find('[')
        subres[key] = val
    return result


_SIM_KEYS = {
    'state': 'state',
    'profiles.assignment': 'profile',
    'simulator_fullname': 'simulator',
    'id': 'folder',
    'job_id': 'job',
}


class _Simulator(object): # pylint: disable=too-many-instance-attributes
    """Simulator"""
    def __init__(self, serv, sid, name, version, email, conf, delay_dist): # pylint: disable=too-many-arguments
        self.server = serv
        self.id = sid # pylint: disable=invalid-name
        self.name = name
        self.version = version
        self.fullname = '{}-{}'.format(name, version)
        self.email = email

        self._conf = conf
        self.role_conf = {}
        current_time = _get_time_str()
        self.created_at = current_time
        self.updated_at = current_time
        self.delay_dist = delay_dist
        self._source = '/uploads/simulator/source/{:d}/{}.zip'.format(
            self.id, self.name)
        self.url = 'https://{}/simulators/{:d}'.format(
            self.server.domain, sid)

    @property
    def configuration(self):
        """Configuration"""
        return self._conf.copy()

    @property
    def role_configuration(self):
        """Role configuration"""
        return {role: strats.copy() for role, strats
                in self.role_conf.items()}

    @property
    def source(self):
        """Source"""
        return {'url': self._source}

    def add_role(self, role):
        """Add role"""
        self.role_conf.setdefault(role, [])
        self.updated_at = _get_time_str()

    def remove_role(self, role):
        """Remove role"""
        if self.role_conf.pop(role, None) is not None:
            self.updated_at = _get_time_str()

    def add_strategy(self, role, strat):
        """Add strategy"""
        strats = self.role_conf[role]
        strats.insert(bisect.bisect_left(strats, strat), strat)
        self.updated_at = _get_time_str()

    def remove_strategy(self, role, strategy):
        """Remove strategy"""
        try:
            self.role_conf[role].remove(strategy)
            self.updated_at = _get_time_str()
        except (KeyError, ValueError):
            pass  # don't care

    def get_all(self):
        """Get creation info"""
        return _dict(
            self,
            ['configuration', 'created_at', 'email', 'id', 'name',
             'role_configuration', 'source', 'updated_at', 'version'])

    def get_info(self):
        """Get info"""
        return _dict(
            self,
            ['configuration', 'created_at', 'email', 'id', 'name',
             'role_configuration', 'source', 'updated_at', 'url', 'version'])


class _Scheduler(object): # pylint: disable=too-many-instance-attributes
    """A scheduler"""
    def __init__( # pylint: disable=too-many-arguments
            self, sim, sid, name, size, obs_per_sim, time_per_obs,
            process_memory, active, nodes, conf):
        self.id = sid # pylint: disable=invalid-name
        self.name = name
        self.active = active
        self.nodes = nodes
        self.default_observation_requirement = 0
        self.observations_per_simulation = obs_per_sim
        self.process_memory = process_memory
        self.simulator_instance_id, self._assignments = (
            sim.server.get_sim_instance(sim.id, conf))
        self.size = size
        self.time_per_observation = time_per_obs
        current_time = _get_time_str()
        self.created_at = current_time
        self.updated_at = current_time
        self.simulator_id = sim.id
        self.url = 'https://{}/generic_schedulers/{:d}'.format(
            sim.server.domain, sid)
        self.type = 'GenericScheduler'

        self._destroyed = False
        self.sim = sim
        self.server = sim.server
        self._conf = conf
        self.role_conf = {}
        self._reqs = {}

    @property
    def configuration(self):
        """Configuration"""
        return [[key, str(value)] for key, value in self._conf.items()]

    @property
    def scheduling_requirements(self):
        """Scheduling requirements"""
        return [_dict(prof, ['current_count'], requirement=count,
                      profile_id=prof.id)
                for prof, count in self._reqs.items()]

    def update(self, name=None, **kwargs):
        """Update the parameters of a given scheduler"""
        # FIXME Technically this should allow updating the configuration and
        # hence the simulator instance id
        assert name is None, "don't handle renaming"
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
        """Add role"""
        assert role in self.sim.role_conf
        assert role not in self.role_conf
        assert sum(self.role_conf.values()) + count <= self.size
        self.role_conf[role] = count
        self.updated_at = _get_time_str()

    def remove_role(self, role):
        """Remove a role from the scheduler"""
        if self.role_conf.pop(role, None) is not None:
            self.updated_at = _get_time_str()

    def destroy(self):
        """Destroy scheduler"""
        self.server.scheds_by_name.pop(self.name)
        self.server.scheds[self.id] = None
        self._destroyed = True

    def get_info(self):
        """Get info"""
        return _dict(
            self,
            ['active', 'created_at', 'default_observation_requirement', 'id',
             'name', 'nodes', 'observations_per_simulation', 'process_memory',
             'simulator_instance_id', 'size', 'time_per_observation',
             'updated_at'])

    def get_requirements(self):
        """Get requirements"""
        return _dict(
            self,
            ['active', 'configuration', 'default_observation_requirement',
             'id', 'name', 'nodes', 'observations_per_simulation',
             'process_memory', 'scheduling_requirements', 'simulator_id',
             'size', 'time_per_observation', 'type', 'url'])

    def get_profile(self, assignment):
        """Get profile"""
        if assignment in self._assignments:
            return self._assignments[assignment]
        prof_id = len(self.server.profiles)
        prof = _Profile(self.sim, prof_id, assignment,
                        self.simulator_instance_id)
        for _, role, strat, _ in prof.symgrps:
            assert role in self.role_conf
            assert strat in self.sim.role_conf[role]
        assert prof.role_conf == self.role_conf
        self.server.profiles.append(prof)
        self._assignments[assignment] = prof
        return prof

    def add_profile(self, assignment, count):
        """Add a profile to the scheduler"""
        prof = self.get_profile(assignment)

        if prof not in self._reqs:
            # This is how egta online behaves, but it seems non ideal
            self._reqs[prof] = count
            self.updated_at = _get_time_str()
            if self.active:
                prof.update(count)

        return prof

    def remove_profile(self, pid):
        """Remove profile"""
        try:
            prof = self.server.profiles[pid]
            if self._reqs.pop(prof, None) is not None:
                self.updated_at = _get_time_str()
        except IndexError:
            pass  # don't care


class _Profile(object): # pylint: disable=too-many-instance-attributes
    """A profile"""
    def __init__(self, sim, pid, assignment, inst_id):
        self.id = pid # pylint: disable=invalid-name
        self.assignment = assignment
        self.simulator_instance_id = inst_id
        current_time = _get_time_str()
        self.created_at = current_time
        self.updated_at = current_time

        self.sim = sim
        self.server = sim.server
        self.symgrps = self.server.assign_to_symgrps(assignment)
        self.role_conf = collections.Counter()
        for _, role, _, count in self.symgrps:
            self.role_conf[role] += count
        self.size = sum(self.role_conf.values())
        self.obs = []
        self._scheduled = 0

    @property
    def observations_count(self):
        """Observations count"""
        return len(self.obs)

    @property
    def current_count(self):
        """Current count"""
        return self.observations_count

    @property
    def role_configuration(self):
        """Role configuration"""
        return self.role_conf.copy()

    @property
    def symmetry_groups(self):
        """Symmetry groups"""
        return [{'id': gid, 'role': role, 'strategy': strat, 'count': count}
                for gid, role, strat, count in self.symgrps]

    def update(self, count):
        """Update count requested"""
        if self._scheduled < count:
            self.updated_at = _get_time_str()
        for _ in range(count - self._scheduled):
            folder = len(self.server.folders)
            obs = _Observation(self, folder)
            self.server.folders.append(obs)
            sim_time = time.time() + self.sim.delay_dist()
            self.server.loop.call_soon_threadsafe(
                self.server.sim_queue.put_nowait, (sim_time, obs.id, obs))
            self._scheduled += 1

    def get_new(self):
        """Newly created data"""
        return _dict(
            self,
            ['assignment', 'created_at', 'id', 'observations_count',
             'role_configuration', 'simulator_instance_id', 'size',
             'updated_at', ])

    def get_structure(self):
        """Structure data"""
        role_conf = {r: str(c) for r, c in self.role_conf.items()}
        return _dict(
            self,
            ['assignment', 'created_at', 'id', 'observations_count',
             'simulator_instance_id', 'size', 'updated_at'],
            role_configuration=role_conf)

    def get_summary(self):
        """Summary data"""
        if self.obs:
            payoffs = {
                gid: (mean, stddev)
                for gid, mean, stddev
                in _mean_id(itertools.chain.from_iterable(
                    obs.pays for obs in self.obs))}
        else:
            payoffs = {gid: (None, None) for gid, _, _, _
                       in self.symgrps}

        symgrps = []
        for gid, role, strat, count in self.symgrps:
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
        """Observations data"""
        observations = [{
            'extended_features': {},
            'features': {},
            'symmetry_groups': [{
                'id': sid,
                'payoff': pay,
                'payoff_sd': None,
            } for sid, pay, _ in _mean_id(obs.pays)]
        } for obs in self.obs]
        return _dict(
            self,
            ['id', 'simulator_instance_id', 'symmetry_groups'],
            observations=observations)

    def get_full(self):
        """Full data"""
        observations = [{
            'extended_features': {},
            'features': {},
            'players': [{
                'e': {},
                'f': {},
                'p': pay,
                'sid': sid,
            } for sid, pay in obs.pays]
        } for obs in self.obs]
        return _dict(
            self,
            ['id', 'simulator_instance_id', 'symmetry_groups'],
            observations=observations)


class _Observation(object): # pylint: disable=too-many-instance-attributes
    """An observation"""
    def __init__(self, prof, oid):
        self.id = oid # pylint: disable=invalid-name
        self.folder = oid
        self.folder_number = oid
        self.job = 'Not specified'
        self.profile = prof.assignment
        self.simulator = prof.sim.fullname
        self.simulator_fullname = self.simulator
        self.simulator_instance_id = prof.simulator_instance_id
        self.size = prof.size
        self.error_message = ''

        self._prof = prof
        self.server = prof.server
        self.pays = tuple(itertools.chain.from_iterable(
            ((gid, random.random()) for _ in range(count))
            for gid, _, _, count in prof.symgrps))
        self._simulated = False

    @property
    def state(self):
        """State"""
        return 'complete' if self._simulated else 'running'

    def simulate(self):
        """Simulate the observation"""
        assert not self._simulated
        self._simulated = True
        self._prof.obs.append(self)

    def get_all(self):
        """Get standard data"""
        return (
            '<tr>' + ''.join(
                '<td>{}</td>'.format(d) for d
                in [self.state, self.profile, self.simulator, self.folder,
                    'n/a'])
            + '</tr>')

    def get_info(self):
        """Get info"""
        return (
            '<div class="show_for simulation">' +
            '\n'.join(
                '<p>{}: {}</p>'.format(
                    key, getattr(self, key.lower().replace(' ', '_')))
                for key in ['Simulator fullname', 'Profile', 'State', 'Size',
                            'Folder number', 'Job', 'Error message']) +
            '</div>')


class _Game(object): # pylint: disable=too-many-instance-attributes
    """A mock game"""
    def __init__(self, sim, gid, name, size, conf): # pylint: disable=too-many-arguments
        self.id = gid # pylint: disable=invalid-name
        self.name = name
        self.simulator_instance_id, self._assignments = (
            sim.server.get_sim_instance(sim.id, conf))
        self.size = size
        current_time = _get_time_str()
        self.created_at = current_time
        self.updated_at = current_time
        self.url = 'https://{}/games/{:d}'.format(sim.server.domain, gid)
        self.simulator_fullname = sim.fullname
        self.subgames = None

        self.sim = sim
        self.server = sim.server
        self._conf = conf
        self.role_conf = {}
        self._destroyed = False

    @property
    def configuration(self):
        """Configuration"""
        return [[k, str(v)] for k, v in self._conf.items()]

    @property
    def roles(self):
        """Roles as symgrps"""
        return [{'name': r, 'count': c, 'strategies': s, } for r, (s, c)
                in sorted(self.role_conf.items())]

    def add_role(self, role, count):
        """Adds a role to the game"""
        assert (sum(c for _, c in self.role_conf.values()) + count <=
                self.size)
        assert role not in self.role_conf, "can't add an existing role"
        assert role in self.sim.role_conf
        self.role_conf[role] = ([], count)
        self.updated_at = _get_time_str()

    def remove_role(self, role):
        """Removes a role from the game"""
        if self.role_conf.pop(role, None) is not None:
            self.updated_at = _get_time_str()

    def add_strategy(self, role, strat):
        """Adds a strategy to the game"""
        strats, _ = self.role_conf[role]
        assert strat in self.sim.role_conf[role]
        strats.insert(bisect.bisect_left(strats, strat), strat)
        self.updated_at = _get_time_str()

    def remove_strategy(self, role, strat):
        """Removes a strategy from the game"""
        try:
            self.role_conf[role][0].remove(strat)
            self.updated_at = _get_time_str()
        except ValueError:
            pass  # don't care

    def destroy(self):
        """Destroy the game"""
        self.server.games_by_name.pop(self.name)
        self.server.games[self.id] = None
        self._destroyed = True

    def get_data(self, func, keys):
        """Get generic data from the game"""
        strats = {r: set(s) for r, (s, _)
                  in self.role_conf.items()}
        counts = {r: c for r, (_, c) in self.role_conf.items()}
        profs = []
        for prof in self._assignments.values():
            # Assignments maps to all assignments in a sim_instance_id, so we
            # must filter by profiles that actually match
            if not prof.obs:
                continue  # no data
            counts_left = counts.copy()
            for _, role, strat, count in prof.symgrps:
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
        """Get all data from the game"""
        return _dict(
            self,
            ['created_at', 'id', 'name', 'simulator_instance_id', 'size',
             'subgames', 'updated_at'])

    def get_structure(self):
        """Get structure from the game"""
        return _dict(
            self,
            ['created_at', 'id', 'name', 'simulator_instance_id', 'size',
             'subgames', 'updated_at', 'url'])

    def get_summary(self):
        """Get summary data from the game"""
        return self.get_data(
            _Profile.get_summary,
            ['id', 'observations_count', 'symmetry_groups'])

    def get_observations(self):
        """Get observations from the game"""
        return self.get_data(
            _Profile.get_observations,
            ['id', 'observations', 'symmetry_groups'])

    def get_full(self):
        """Get full data from the game"""
        return self.get_data(
            _Profile.get_full,
            ['id', 'observations', 'symmetry_groups'])


def server(domain='egtaonline.eecs.umich.edu', **kwargs):
    """Create a mock server"""
    return _Server(domain, **kwargs)


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
    """Get a str for the current time"""
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z')


def _mean_id(iterator):
    """Get the mean for each id"""
    means = {}
    for sid, pay in iterator:
        dat = means.setdefault(sid, [0, 0.0, 0.0])
        old_mean = dat[1]
        dat[0] += 1
        dat[1] += (pay - dat[1]) / dat[0]
        dat[2] += (pay - old_mean) * (pay - dat[1])
    return ((sid, m, math.sqrt(s / (c - 1)) if c > 1 else None)
            for sid, (c, m, s) in means.items())
