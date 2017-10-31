"""Python package to mock python interface to egta online api"""
import bisect
import collections
import itertools
import math
import queue
import random
import threading
import time


# XXX The current thread locking is more aggressive than it needs to be, and
# may be faster and more easily accomplished with the use of thread safe
# dictionaries.


class _Base(dict):
    def __init__(self, backend, keys):
        super().__init__((k, getattr(backend, k)) for k in keys)
        self._backend = backend
        backend._valid()


class EgtaOnlineApi(object):
    """Class that mocks access to an Egta Online server"""

    def __init__(self, *_, domain='egtaonline.eecs.umich.edu', **__):
        self.domain = domain
        self._is_open = False

        self._sims = []
        self._sims_by_name = {}
        self._sims_lock = threading.Lock()

        self._scheds = []
        self._scheds_by_name = {}
        self._scheds_lock = threading.Lock()

        self._games = []
        self._games_by_name = {}
        self._games_lock = threading.Lock()

        self._sim_insts = {}
        self._sim_insts_lock = threading.Lock()

        self._symgrps_tup = {}
        self._symgrps_lock = threading.Lock()

        self._profiles = []
        self._profiles_lock = threading.Lock()

        self._folders = []
        self._folders_lock = threading.Lock()

        self._sim_thread = None
        self._sim_interrupt = threading.Lock()
        self._sim_queue = queue.PriorityQueue()

    def _check_open(self):
        assert self._is_open, "connection closed"

    def __enter__(self):
        assert not self._is_open
        assert self._sim_thread is None
        assert self._sim_queue.empty()
        self._is_open = True
        assert self._sim_interrupt.acquire(False)
        self._sim_thread = threading.Thread(target=self._run_simulations)
        self._sim_thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._sim_interrupt.release()
        self._sim_queue.put((0, 0, None))
        self._sim_thread.join(1)
        assert not self._sim_thread.is_alive(), "thread didn't die"
        self._sim_thread = None
        while not self._sim_queue.empty():
            self._sim_queue.get()
        self._is_open = False

    def _run_simulations(self):
        """Thread to run simulations at specified time"""
        while True:
            wait_until, _, obs = self._sim_queue.get()
            timeout = max(wait_until - time.time(), 0)
            if self._sim_interrupt.acquire(timeout=timeout):
                self._sim_interrupt.release()
                return  # we were told to die
            obs._simulate()

    def _get_sim_instance(self, sim_id, configuration):
        """Get the sim instance id for a sim and conf"""
        return self._sim_insts.setdefault(
            (sim_id, frozenset(configuration.items())),
            (len(self._sim_insts), threading.Lock(), {}))

    def _get_symgrp_id(self, symgrp):
        if symgrp in self._symgrps_tup:
            return self._symgrps_tup[symgrp]
        else:
            with self._symgrps_lock:
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

    def create_simulator(self, name, version, email='egta@mailinator.com',
                         conf={}, delay_dist=lambda: 0):
        """Create a simulator

        This doesn't exist in the standard api.
        """
        assert version not in self._sims_by_name.get(name, {}), \
            "name already exists"
        with self._sims_lock:
            sim_id = len(self._sims)
            sim = _Simulator(self, sim_id, name, version, email, conf,
                             delay_dist)
            self._sims.append(sim)
            self._sims_by_name.setdefault(name, {})[version] = sim
        return Simulator(sim, ['id'])

    def create_generic_scheduler(
            self, sim_id, name, active, process_memory, size,
            time_per_observation, observations_per_simulation, nodes=1,
            configuration={}):
        """Creates a generic scheduler and returns it"""
        assert name not in self._scheds_by_name, \
            "name already exists"
        sim = self._sims[sim_id]
        conf = sim.configuration
        conf.update(configuration)
        with self._scheds_lock:
            sched_id = len(self._scheds)
            sim = self._sims[sim_id]
            sched = _Scheduler(
                sim, sched_id, name, size, observations_per_simulation,
                time_per_observation, process_memory, bool(active), nodes,
                conf)
            self._scheds.append(sched)
            self._scheds_by_name[name] = sched
            return Scheduler(sched,
                             ['active', 'created_at',
                              'default_observation_requirement', 'id', 'name',
                              'nodes', 'observations_per_simulation',
                              'process_memory', 'simulator_instance_id',
                              'size', 'time_per_observation', 'updated_at'])

    def create_game(self, sim_id, name, size, configuration={}):
        """Creates a game and returns it"""
        assert name not in self._games_by_name, \
            "name already exists"
        sim = self._sims[sim_id]
        conf = sim.configuration
        conf.update(configuration)
        with self._games_lock:
            game_id = len(self._games)
            game = _Game(sim, game_id, name, size, configuration)
            self._games.append(game)
            self._games_by_name[name] = game
            return Game(game, ['id'])

    def _get_sim(self, sim):
        return Simulator(sim,
                         ['configuration', 'created_at', 'email', 'id', 'name',
                          'role_configuration', 'source', 'updated_at',
                          'version'])

    def get_simulators(self):
        """Get a generator of all simulators"""
        self._check_open()
        return (self._get_sim(s) for s in self._sims if s is not None)

    def get_simulator(self, id_or_name, version=None):
        """Get a simulator"""
        self._check_open()
        if isinstance(id_or_name, int):
            if 0 <= id_or_name < len(self._sims):
                return Simulator(self._sims[id_or_name], ['id'])
            else:
                return _ErrorObj(id_or_name)

        sim_dict = self._sims_by_name.get(id_or_name, {})
        if version is not None:
            return self._get_sim(sim_dict[version])
        else:
            sims = iter(sim_dict.values())
            try:
                sim = next(sims)
            except StopIteration:
                raise ValueError(
                    "Simulator {} does not exist".format(id_or_name))
            try:
                next(sims)
                raise ValueError(
                    "Simulator {} has multiple versions: {}"
                    .format(id_or_name, ', '.join(s.version for s in sims)))
            except StopIteration:
                return self._get_sim(sim)

    def _get_sched(self, sched):
        return Scheduler(sched,
                         ['active', 'created_at',
                          'default_observation_requirement', 'id', 'name',
                          'nodes', 'observations_per_simulation',
                          'process_memory', 'simulator_instance_id', 'size',
                          'time_per_observation', 'updated_at'])

    def get_generic_schedulers(self):
        """Get a generator of all generic schedulers"""
        self._check_open()
        return (self._get_sched(s) for s in self._scheds if s is not None)

    def get_scheduler(self, id_or_name):
        """Get a scheduler with an or name"""
        self._check_open()
        if isinstance(id_or_name, int):
            if (0 <= id_or_name < len(self._scheds) and
                    self._scheds[id_or_name] is not None):
                return Scheduler(self._scheds[id_or_name], ['id'])
            else:
                return _ErrorObj(id_or_name)
        else:
            return self._get_sched(self._scheds_by_name[id_or_name])

    def _get_game(self, game):
        return Game(game,
                    ['created_at', 'id', 'name', 'simulator_instance_id',
                     'size', 'subgames', 'updated_at'])

    def get_games(self):
        """Get a generator of all games"""
        self._check_open()
        return (self._get_game(g) for g in self._games if g is not None)

    def get_game(self, id_or_name):
        """Get a game"""
        self._check_open()
        if isinstance(id_or_name, int):
            if (0 <= id_or_name < len(self._games) and
                    self._games[id_or_name] is not None):
                return Game(self._games[id_or_name], ['id'])
            else:
                return _ErrorObj(id_or_name)
        else:
            return self._get_game(self._games_by_name[id_or_name])

    def get_profile(self, pid):
        """Get a profile from its id"""
        if 0 <= pid < len(self._profiles):
            return Profile(self._profiles[pid], ['id'])
        else:
            return _ErrorObj(pid)

    def get_simulations(self, page_start=1, asc=False, column='job'):
        """Get information about current simulations"""
        self._check_open()

        if column in {'folder', 'profile', 'simulator'}:
            sims = sorted(self._folders, key=lambda f: getattr(f, column),
                          reverse=not asc)
        elif asc:
            sims = self._folders
        else:
            sims = self._folders[::-1]
        return (Observation(f, ['folder', 'profile', 'simulator', 'state',
                                'job'])
                for f in itertools.islice(sims, 25 * (page_start - 1), None))

    def get_simulation(self, folder):
        """Get a simulation from its folder number"""
        self._check_open()
        info = self._folders[folder]
        return Observation(info,
                           ['error_message', 'folder_number', 'profile',
                            'simulator_fullname', 'size', 'state'],
                           job='Not specified')


class _Simulator(object):
    def __init__(self, api, sid, name, version, email, conf, delay_dist):
        self._api = api
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
        self._lock = threading.Lock()
        self._delay_dist = delay_dist
        self._source = '/uploads/simulator/source/{:d}/{}.zip'.format(
            self.id, self.fullname)
        self.url = 'https://{}/simulators/{:d}'.format(self._api.domain, sid)

    def _valid(self):
        self._api._check_open()

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

    def _add_role(self, role):
        with self._lock:
            self._valid()
            self._role_conf.setdefault(role, [])
            self.updated_at = _get_time_str()

    def _remove_role(self, role):
        with self._lock:
            self._valid()
            if self._role_conf.pop(role, None) is not None:
                self.updated_at = _get_time_str()

    def _add_strategy(self, role, strat):
        with self._lock:
            self._valid()
            strats = self._role_conf[role]
            index = bisect.bisect_left(strats, strat)
            if index >= len(strats) or strats[index] != strat:
                strats.insert(index, strat)
            self.updated_at = _get_time_str()

    def _remove_strategy(self, role, strategy):
        with self._lock:
            self._valid()
            try:
                self._role_conf[role].remove(strategy)
                self.updated_at = _get_time_str()
            except ValueError:
                pass  # don't care


class Simulator(_Base):
    """Get information about and modify EGTA Online Simulators"""

    def get_info(self):
        """Return information about this simulator"""
        return Simulator(self._backend,
                         ['configuration', 'created_at', 'email', 'id', 'name',
                          'role_configuration', 'source', 'updated_at', 'url',
                          'version'])

    def add_role(self, role):
        """Adds a role to the simulator"""
        self._backend._add_role(role)

    def remove_role(self, role):
        """Removes a role from the simulator"""
        self._backend._remove_role(role)

    def add_strategy(self, role, strat):
        """Adds a strategy to the simulator"""
        self._backend._add_strategy(role, strat)

    def remove_strategy(self, role, strategy):
        """Removes a strategy from the simulator"""
        self._backend._remove_strategy(role, strategy)

    def add_dict(self, role_strat_dict):
        """Adds all of the roles and strategies in a dictionary"""
        for role, strats in role_strat_dict.items():
            self.add_role(role)
            for strat in strats:
                self.add_strategy(role, strat)

    def remove_dict(self, role_strat_dict):
        """Removes all of the strategies in a dictionary"""
        for role, strategies in role_strat_dict.items():
            for strategy in strategies:
                self.remove_strategy(role, strategy)

    def create_generic_scheduler(
            self, name, active, process_memory, size, time_per_observation,
            observations_per_simulation, nodes=1, configuration={}):
        """Creates a generic scheduler and returns it"""
        return self._backend._api.create_generic_scheduler(
            self._backend.id, name, active, process_memory, size,
            time_per_observation, observations_per_simulation, nodes,
            configuration)

    def create_game(self, name, size, configuration={}):
        """Creates a game and returns it"""
        return self._backend._api.create_game(
            self._backend.id, name, size, configuration)


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
        self.simulator_instance_id, self._assign_lock, self._assignments = (
            sim._api._get_sim_instance(sim.id, conf))
        self.size = size
        self.time_per_observation = time_per_obs
        current_time = _get_time_str()
        self.created_at = current_time
        self.updated_at = current_time
        self.simulator_id = sim.id
        self.url = 'https://{}/generic_schedulers/{:d}'.format(
            sim._api.domain, sid)
        self.type = 'GenericScheduler'

        self._destroyed = False
        self._sim = sim
        self._api = sim._api
        self._conf = conf
        self._role_conf = {}
        self._reqs = {}
        self._lock = threading.Lock()

    def _valid(self):
        self._api._check_open()
        assert not self._destroyed, "scheduler doesn't exist"

    @property
    def configuration(self):
        return [[key, str(value)] for key, value in self._conf.items()]

    @property
    def scheduling_requirements(self):
        return [Profile(prof, ['id', 'current_count'], requirement=count)
                for prof, count in self._reqs.items()]

    def _update(self, **kwargs):
        """Update the parameters of a given scheduler"""
        if 'active' in kwargs:
            kwargs['active'] = bool(kwargs['active'])
        with self._lock:
            self._valid()
            if not self.active and kwargs['active']:
                for prof, count in self._reqs.items():
                    prof._update(count)
            # FIXME Only for valid keys
            for key, val in kwargs.items():
                setattr(self, key, val)
            self.updated_at = _get_time_str()

    def _add_role(self, role, count):
        with self._lock, self._sim._lock:
            self._valid()
            assert role in self._sim._role_conf
            assert role not in self._role_conf
            assert sum(self._role_conf.values()) + count <= self.size
            self._role_conf[role] = count
            self.updated_at = _get_time_str()

    def _remove_role(self, role):
        """Remove a role from the scheduler"""
        with self._lock:
            self._valid()
            if self._role_conf.pop(role, None) is not None:
                self.updated_at = _get_time_str()

    def _destroy(self):
        with self._lock, self._api._scheds_lock:
            self._api._scheds_by_name.pop(self.name)
            self._api._scheds[self.id] = None
            self._destroyed = True

    def _get_profile(self, assignment):
        with self._assign_lock:
            if assignment in self._assignments:
                return self._assignments[assignment]
            else:
                with self._api._profiles_lock:
                    prof_id = len(self._api._profiles)
                    prof = _Profile(self._sim, prof_id, assignment,
                                    self.simulator_instance_id)
                    for _, role, strat, _ in prof._symgrps:
                        assert role in self._role_conf
                        assert strat in self._sim._role_conf[role]
                    assert prof._role_conf == self._role_conf
                    self._api._profiles.append(prof)
                    self._assignments[assignment] = prof
                    return prof

    def _add_profile(self, assignment, count):
        """Add a profile to the scheduler"""
        with self._lock:
            self._valid()
        if not isinstance(assignment, str):
            assignment = symgrps_to_assignment(assignment)
        prof = self._get_profile(assignment)

        if prof not in self._reqs:
            # XXX This is how egta online behaves, but it seems non ideal
            with self._lock:
                self._reqs[prof] = count
                self.updated_at = _get_time_str()
            if self.active:
                prof._update(count)

        return Profile(prof,
                       ['assignment', 'created_at', 'id', 'observations_count',
                        'role_configuration', 'simulator_instance_id', 'size',
                        'updated_at', ])

    def _update_profile(self, profile, count):
        with self._lock:
            self._valid()
        if isinstance(profile, int):
            prof = self._api._profiles[profile]
        elif isinstance(profile, str):
            prof = self._get_profile(profile)
        elif 'id' in profile:
            prof = self._api._profiles[profile['id']]
        elif 'assignment' in profile:
            prof = self._get_profile(profile['assignment'])
        elif 'symmetry_groups' in profile:
            prof = self._get_profile(symgrps_to_assignment(
                profile['symmetry_groups']))
        else:
            prof = self._get_profile(symgrps_to_assignment(profile))

        with self._lock:
            self._reqs[prof] = count
        if self.active:
            prof._update(count)
        return Profile(prof,
                       ['assignment', 'created_at', 'id', 'observations_count',
                        'role_configuration', 'simulator_instance_id', 'size',
                        'updated_at', ])

    def _remove_profile_id(self, pid):
        with self._lock:
            self._valid()
            try:
                prof = self._api._profiles[pid]
                if self._reqs.pop(prof, None) is not None:
                    self.updated_at = _get_time_str()
            except IndexError:
                pass  # don't care

    def _remove_all_profiles(self):
        with self._lock:
            self._valid()
            if self._reqs:
                self.updated_at = _get_time_str()
            self._reqs.clear()


class Scheduler(_Base):
    """Get information and modify EGTA Online Scheduler"""

    def get_info(self):
        """Get a scheduler information"""
        return Scheduler(self._backend,
                         ['active', 'created_at',
                          'default_observation_requirement', 'id', 'name',
                          'nodes', 'observations_per_simulation',
                          'process_memory', 'simulator_instance_id', 'size',
                          'time_per_observation', 'updated_at'])

    def get_requirements(self):
        return Scheduler(self._backend,
                         ['active', 'configuration',
                          'default_observation_requirement', 'id', 'name',
                          'nodes', 'observations_per_simulation',
                          'process_memory', 'scheduling_requirements',
                          'simulator_id', 'size', 'time_per_observation',
                          'type', 'url'])

    def update(self, **kwargs):
        """Update the parameters of a given scheduler"""
        self._backend._update(**kwargs)

    def activate(self):
        self.update(active=True)

    def deactivate(self):
        self.update(active=False)

    def add_role(self, role, count):
        """Add a role with specific count to the scheduler"""
        self._backend._add_role(role, count)

    def remove_role(self, role):
        """Remove a role from the scheduler"""
        self._backend._remove_role(role)

    def destroy_scheduler(self):
        """Delete a generic scheduler"""
        self._backend._destroy()

    def add_profile(self, assignment, count):
        """Add a profile to the scheduler"""
        return self._backend._add_profile(assignment, count)

    def update_profile(self, profile, count):
        """Update the requested count of a profile object"""
        return self._backend._update_profile(profile, count)

    def remove_profile(self, profile):
        """Removes a profile from a scheduler"""
        if not isinstance(profile, int):
            profile = profile['id']
        self._backend._remove_profile_id(profile)

    def remove_all_profiles(self):
        """Removes all profiles from a scheduler"""
        self._backend._remove_all_profiles()

    def create_game(self, name=None):
        """Creates a game with the same parameters of the scheduler

        If name is unspecified, it will copy the name from the scheduler. This
        will fail if there's already a game with that name."""
        return self._backend._api.create_game(
            self._backend._sim.id,
            self._backend.name if name is None else name,
            self._backend.size, self._backend._conf)


class _Profile(object):
    def __init__(self, sim, pid, assignment, inst_id):
        self.id = pid
        self.assignment = assignment
        self.simulator_instance_id = inst_id
        current_time = _get_time_str()
        self.created_at = current_time
        self.updated_at = current_time

        self._sim = sim
        self._api = sim._api
        self._symgrps = self._api._assign_to_symgrps(assignment)
        self._role_conf = collections.Counter()
        for _, role, _, count in self._symgrps:
            self._role_conf[role] += count
        self.size = sum(self._role_conf.values())
        self._obs = []
        self._scheduled = 0
        self._lock = threading.Lock()

    def _valid(self):
        self._api._check_open()

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

    def _update(self, count):
        with self._lock:
            if self._scheduled < count:
                self.updated_at = _get_time_str()
            for _ in range(count - self._scheduled):
                with self._api._folders_lock:
                    folder = len(self._api._folders)
                    obs = _Observation(self, folder)
                    self._api._folders.append(obs)
                    sim_time = time.time() + self._sim._delay_dist()
                    self._api._sim_queue.put((sim_time, obs.id, obs))
                self._scheduled += 1


class Profile(_Base):
    """Class for manipulating profiles"""

    def __init__(self, backend, keys, **extra):
        super().__init__(backend, keys)
        self.update(extra)

    def get_info(self, granularity='structure'):
        """Gets information about the profile"""
        if granularity == 'structure':
            return self.get_structure()
        elif granularity == 'summary':
            return self.get_summary()
        elif granularity == 'observations':
            return self.get_observations()
        elif granularity == 'full':
            return self.get_full_data()
        else:
            raise ValueError(
                "{} is not a valid granularity".format(granularity))

    def get_structure(self):
        role_conf = {r: str(c) for r, c in self._backend._role_conf.items()}
        return Profile(self._backend,
                       ['assignment', 'created_at', 'id', 'observations_count',
                        'simulator_instance_id', 'size', 'updated_at'],
                       role_configuration=role_conf)

    def get_summary(self):
        if self._backend._obs:
            payoffs = {
                gid: (mean, stddev)
                for gid, mean, stddev
                in _mean_id(itertools.chain.from_iterable(
                    obs._pays for obs in self._backend._obs))}
        else:
            payoffs = {gid: (None, None) for gid, _, _, _
                       in self._backend._symgrps}

        symgrps = []
        for gid, role, strat, count in self._backend._symgrps:
            pay, pay_sd = payoffs[gid]
            symgrps.append({
                'id': gid,
                'role': role,
                'strategy': strat,
                'count': count,
                'payoff': pay,
                'payoff_sd': pay_sd,
            })
        return Profile(self._backend,
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
        } for obs in self._backend._obs]
        return Profile(self._backend,
                       ['id', 'simulator_instance_id', 'symmetry_groups'],
                       observations=observations)

    def get_full_data(self):
        observations = [{
            'extended_features': {},
            'features': {},
            'players': [{
                'e': {},
                'f': {},
                'p': pay,
                'sid': sid,
            } for sid, pay in obs._pays]
        } for obs in self._backend._obs]
        return Profile(self._backend,
                       ['id', 'simulator_instance_id', 'symmetry_groups'],
                       observations=observations)


class _Observation(object):
    def __init__(self, prof, oid):
        self.id = oid
        self.folder = oid
        self.folder_number = oid
        self.job = float('nan')
        self.profile = prof.assignment
        self.simulator = prof._sim.fullname
        self.simulator_fullname = self.simulator
        self.simulator_instance_id = prof.simulator_instance_id
        self.size = prof.size
        self.error_message = ''

        self._prof = prof
        self._api = prof._api
        self._pays = tuple(itertools.chain.from_iterable(
            ((gid, random.random()) for _ in range(count))
            for gid, _, _, count in prof._symgrps))
        self._simulated = False

    def _valid(self):
        self._api._check_open()

    @property
    def state(self):
        return 'complete' if self._simulated else 'running'

    def _simulate(self):
        assert not self._simulated
        self._simulated = True
        with self._prof._lock:
            self._prof._obs.append(self)


class Observation(_Base):
    def __init__(self, backend, keys, **extra):
        super().__init__(backend, keys)
        self.update(extra)


class _Game(object):
    def __init__(self, sim, gid, name, size, conf):
        self.id = gid
        self.name = name
        self.simulator_instance_id, _, self._assignments = (
            sim._api._get_sim_instance(sim.id, conf))
        self.size = size
        current_time = _get_time_str()
        self.created_at = current_time
        self.updated_at = current_time
        self.url = 'https://{}/games/{:d}'.format(sim._api.domain, gid)
        self.simulator_fullname = sim.fullname
        self.subgames = None

        self._sim = sim
        self._api = sim._api
        self._conf = conf
        self._role_conf = {}
        self._destroyed = False
        self._lock = threading.Lock()

    def _valid(self):
        self._api._check_open()
        assert not self._destroyed, "game doesn't exist"

    @property
    def configuration(self):
        return [[k, str(v)] for k, v in self._conf.items()]

    @property
    def roles(self):
        return [{'name': r, 'count': c, 'strategies': s, } for r, (s, c)
                in sorted(self._role_conf.items())]

    def _add_role(self, role, count):
        """Adds a role to the game"""
        with self._lock:
            self._valid()
            assert (sum(c for _, c in self._role_conf.values()) + count <=
                    self.size)
            assert role not in self._role_conf, "can't add an existing role"
            assert role in self._sim._role_conf
            self._role_conf[role] = ([], count)
            self.updated_at = _get_time_str()

    def _remove_role(self, role):
        """Removes a role from the game"""
        with self._lock:
            self._valid()
            if self._role_conf.pop(role, None) is not None:
                self.updated_at = _get_time_str()

    def _add_strategy(self, role, strat):
        """Adds a strategy to the game"""
        with self._lock:
            self._valid()
            strats, _ = self._role_conf[role]
            assert strat in self._sim._role_conf[role]
            index = bisect.bisect_left(strats, strat)
            if index >= len(strats) or strats[index] != strat:
                strats.insert(index, strat)
                self.updated_at = _get_time_str()

    def _remove_strategy(self, role, strat):
        """Removes a strategy from the game"""
        with self._lock:
            self._valid()
            try:
                self._role_conf[role][0].remove(strat)
                self.updated_at = _get_time_str()
            except ValueError:
                pass  # don't care

    def _destroy(self):
        with self._lock, self._api._games_lock:
            self._api._games_by_name.pop(self.name)
            self._api._games[self.id] = None
            self._destroyed = True

    def _get_data(self, func, keys):
        strats = {r: set(s) for r, (s, _)
                  in self._role_conf.items()}
        counts = {r: c for r, (_, c) in self._role_conf.items()}
        profs = []
        for prof in self._assignments.values():
            if not prof._obs:
                continue  # no data
            counts_left = counts.copy()
            for _, role, strat, count in prof._symgrps:
                if strat not in strats.get(role, ()):
                    continue  # invalid profile
                counts_left[role] -= count
            if all(c == 0 for c in counts_left.values()):
                jprof = func(Profile(prof, ()))
                for k in set(jprof.keys()).difference(keys):
                    jprof.pop(k)
                profs.append(jprof)

        return Game(self,
                    ['id', 'configuration', 'roles', 'simulator_fullname',
                     'name', 'url'],
                    profiles=profs)


class Game(_Base):
    """Get information and manipulate EGTA Online Games"""

    def __init__(self, backend, keys, **extra):
        super().__init__(backend, keys)
        self.update(extra)

    def get_info(self, granularity='structure'):
        """Gets game information from EGTA Online"""
        if granularity == 'structure':
            return self.get_structure()
        elif granularity == 'summary':
            return self.get_summary()
        elif granularity == 'observations':
            return self.get_observations()
        elif granularity == 'full':
            return self.get_full_data()
        else:
            raise ValueError(
                "{} is not a valid granularity".format(granularity))

    def get_structure(self):
        return Game(self._backend,
                    ['created_at', 'id', 'name', 'simulator_instance_id',
                     'size', 'subgames', 'updated_at', 'url'])

    def get_summary(self):
        return self._backend._get_data(
            Profile.get_summary,
            ['id', 'observations_count', 'symmetry_groups'])

    def get_observations(self):
        return self._backend._get_data(
            Profile.get_observations,
            ['id', 'observations', 'symmetry_groups'])

    def get_full_data(self):
        return self._backend._get_data(
            Profile.get_full_data,
            ['id', 'observations', 'symmetry_groups'])

    def add_role(self, role, count):
        """Adds a role to the game"""
        self._backend._add_role(role, count)

    def remove_role(self, role):
        """Removes a role from the game"""
        self._backend._remove_role(role)

    def add_strategy(self, role, strat):
        """Adds a strategy to the game"""
        self._backend._add_strategy(role, strat)

    def remove_strategy(self, role, strat):
        """Removes a strategy from the game"""
        self._backend._remove_strategy(role, strat)

    def add_dict(self, role_strat_dict):
        """Attempts to add all of the strategies in a dictionary"""
        for role, strategies in role_strat_dict.items():
            for strategy in strategies:
                self.add_strategy(role, strategy)

    def remove_dict(self, role_strat_dict):
        """Removes all of the strategies in a dictionary"""
        for role, strategies in role_strat_dict.items():
            for strategy in set(strategies):
                self.remove_strategy(role, strategy)

    def destroy_game(self):
        """Delete a game"""
        self._backend._destroy()


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


class _ErrorObj(dict):
    def __init__(self, oid):
        super().__init__(id=oid)

    def __raise_error(self, *args, **kwargs):
        raise ValueError("object doesn't exist")

    def __getattr__(self, _):
        return self.__raise_error


class ExceptionEgtaOnlineApi(EgtaOnlineApi):
    def __init__(self, exception, except_in, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._exception = exception
        self._except_in = except_in
        self._lock = threading.Lock()

    def _check_open(self):
        with self._lock:
            self._except_in -= 1
            if self._except_in <= 0:
                raise self._exception
        super()._check_open()
