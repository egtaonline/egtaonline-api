"""Python package to mock python interface to egta online api"""
import bisect
import itertools
import math
import random
import threading
import time


class _Base(dict):
    """A base api object"""

    def __init__(self, api, *args, **kwargs):
        assert api is not None and id is not None
        self._api = api
        super().__init__(*args, **kwargs)

    def __getattr__(self, name):
        return self[name]


# FIXME A better way to organize this would be to have backend objects, and
# then have _Base just store a reference to them. When returning new
# information, you just pass the fields that base copies, or something along
# those lines, instead of using complicated dictionaries. You then have
# constructors of _Base which take a list of properties and store those in a
# dict.
# XXX The current thread locking is more aggressive than it needs to be, and
# may be faster and more easily accomplished with the use of thread safe
# dictionaries.

class EgtaOnlineApi(object):
    """Class that mocks access to an Egta Online server"""

    def __init__(self, *_, domain='egtaonline.eecs.umich.edu', **__):
        self._domain = domain
        self._open = False

        self._sims = []
        self._sims_by_name = {}
        self._sims_lock = threading.Lock()

        self._scheds = []
        self._scheds_by_name = {}
        self._scheds_lock = threading.Lock()

        self._games = []
        self._games_by_name = {}
        self._games_lock = threading.Lock()

        self._sim_insts = []
        self._sim_insts_by_key = {}
        self._sim_insts_lock = threading.Lock()

        self._symgrps_tup = {}
        self._symgrps_lock = threading.Lock()

        self._profiles = []
        self._profiles_lock = threading.Lock()

        self._folders = []
        self._folders_lock = threading.Lock()

    def close(self):
        assert self._open

    def __enter__(self):
        assert not self._open
        self._open = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _get_sim_instance_id(self, sim_id, configuration):
        """Get the sim instance id for a sim and conf"""
        key = (sim_id, tuple(sorted(configuration.items())))
        if key in self._sim_insts_by_key:
            return self._sim_insts_by_key[key]
        else:
            with self._sim_insts_lock:
                inst_id = len(self._sim_insts)
                self._sim_insts.append((threading.Lock(), {}))
                self._sim_insts_by_key[key] = inst_id
                return inst_id

    def _get_symgrp_id(self, symgrp):
        if symgrp in self._symgrps_tup:
            return self._symgrps_tup[symgrp]
        else:
            with self._symgrps_lock:
                sym_id = len(self._symgrps_tup)
                self._symgrps_tup[symgrp] = sym_id
                return sym_id

    def _get_folder(self):
        with self._folders_lock:
            fold = {'folder': len(self._folders)}
            self._folders.append(fold)
            return fold

    def _assign_to_symgrps(self, assign):
        """Turn an assignment string into a role_conf and a size"""
        symgroups = []
        for rolestrat in assign.split('; '):
            role, strats = rolestrat.split(': ', 1)
            for stratstr in strats.split(', '):
                scount, strat = stratstr.split(' ', 1)
                count = int(scount)
                symgroups.append({
                    'id': self._get_symgrp_id((role, strat, count)),
                    'role': role,
                    'strategy': strat,
                    'count': count,
                })
        return symgroups

    def create_simulator(self, name=None, version='',
                         email='egta@mailinator.com', conf={}):
        """Create a simulator"""
        assert self._open, "connection not opened"
        sim_time = _get_time_str()
        with self._sims_lock:
            sim_id = len(self._sims)
            name = name or 'sim_{:d}'.format(sim_id)
            assert version not in self._sims_by_name.get(name, {}), \
                "name already exists"
            simulator = {
                'configuration': conf,
                'created_at': sim_time,
                'email': email,
                'id': sim_id,
                'name': name,
                'lock': threading.RLock(),
                'role_configuration': {},
                'updated_at': sim_time,
                'version': version,
            }
            self._sims.append(simulator)
            self._sims_by_name.setdefault(name, {})[version] = simulator
            return Simulator(self, id=sim_id)

    def create_generic_scheduler(
            self, sim_id, name, active, process_memory, size,
            time_per_observation, observations_per_simulation, nodes=1,
            configuration={}):
        """Creates a generic scheduler and returns it"""
        assert self._open, "connection not opened"
        assert name not in self._scheds_by_name, \
            "name already exists"
        conf = self.get_simulator(sim_id).get_info()['configuration']
        conf.update(configuration)
        sched_time = _get_time_str()
        with self._scheds_lock:
            sched_id = len(self._scheds)
            scheduler = {
                'active': bool(active),
                'configuration': conf,
                'created_at': sched_time,
                'default_observation_requirement': 0,
                'id': sched_id,
                'lock': threading.RLock(),
                'name': name,
                'nodes': nodes,
                'observations_per_simulation': observations_per_simulation,
                'process_memory': process_memory,
                'role_configuration': {},
                'scheduling_requirements': {},
                'simulator_id': sim_id,
                'simulator_instance_id': self._get_sim_instance_id(
                    sim_id, conf),
                'size': size,
                'time_per_observation': time_per_observation,
                'updated_at': sched_time,
            }
            self._scheds.append(scheduler)
            self._scheds_by_name[name] = scheduler
            return Scheduler(self, {
                'active': scheduler['active'],
                'created_at': scheduler['created_at'],
                'default_observation_requirement': (
                    scheduler['default_observation_requirement']),
                'id': scheduler['id'],
                'name': scheduler['name'],
                'nodes': scheduler['nodes'],
                'observations_per_simulation': (
                    scheduler['observations_per_simulation']),
                'process_memory': scheduler['process_memory'],
                'simulator_instance_id': scheduler['simulator_instance_id'],
                'size': scheduler['size'],
                'time_per_observation': scheduler['time_per_observation'],
                'updated_at': scheduler['updated_at'],
            })

    def create_game(self, sim_id, name, size, configuration={}):
        """Creates a game and returns it"""
        assert self._open, "connection not opened"
        assert name not in self._games_by_name, \
            "name already exists"
        conf = self.get_simulator(sim_id).get_info().configuration
        conf.update(configuration)
        game_time = _get_time_str()
        with self._games_lock:
            game_id = len(self._games)
            game = {
                'configuration': [[k, str(v)] for k, v in conf.items()],
                'created_at': game_time,
                'id': game_id,
                'lock': threading.RLock(),
                'name': name,
                'role_configuration': {},
                'simulator_id': sim_id,
                'simulator_instance_id': self._get_sim_instance_id(
                    sim_id, conf),
                'size': size,
                'updated_at': game_time,
            }
            self._games.append(game)
            self._games_by_name[name] = game
            return Game(self, id=game_id)

    def _get_sim(self, sim_data):
        return Simulator(self, {
            'configuration': sim_data['configuration'],
            'created_at': sim_data['created_at'],
            'email': sim_data['email'],
            'id': sim_data['id'],
            'name': sim_data['name'],
            'role_configuration': sim_data['role_configuration'],
            'soruce': {
                'url': '/uploads/simulator/source/{:d}/{}-{}.zip'.format(
                    sim_data['id'], sim_data['name'], sim_data['version'])},
            'updated_at': sim_data['updated_at'],
            'version': sim_data['version'],
        })

    def get_simulators(self):
        """Get a generator of all simulators"""
        assert self._open, "connection not opened"
        return (self._get_sim(s) for s in self._sims if s is not None)

    def get_simulator(self, id_or_name, version=None):
        """Get a simulator"""
        assert self._open, "connection not opened"
        if isinstance(id_or_name, int):
            return Simulator(self, id=id_or_name)

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

    def _get_sched(self, sched_data):
        return Scheduler(self, {
            'active': sched_data['active'],
            'created_at': sched_data['created_at'],
            'default_observation_requirement': (
                sched_data['default_observation_requirement']),
            'id': sched_data['id'],
            'name': sched_data['name'],
            'nodes': sched_data['nodes'],
            'observations_per_simulation': (
                sched_data['observations_per_simulation']),
            'process_memory': sched_data['process_memory'],
            'simulator_instance_id': sched_data['simulator_instance_id'],
            'size': sched_data['size'],
            'time_per_observation': sched_data['time_per_observation'],
            'updated_at': sched_data['updated_at'],
        })

    def get_generic_schedulers(self):
        """Get a generator of all generic schedulers"""
        assert self._open, "connection not opened"
        return (self._get_sched(s) for s in self._scheds if s is not None)

    def get_scheduler(self, id_or_name):
        """Get a scheduler with an or name"""
        assert self._open, "connection not opened"
        if isinstance(id_or_name, int):
            return Scheduler(self, id=id_or_name)
        else:
            return self._get_sched(self._scheds_by_name[id_or_name])

    def _get_game(self, game_data):
        return Game(self, {
            'created_at': game_data['created_at'],
            'id': game_data['id'],
            'name': game_data['name'],
            'simulator_instance_id': game_data['simulator_instance_id'],
            'size': game_data['size'],
            'subgames': None,
            'updated_at': game_data['updated_at'],
        })

    def get_games(self):
        """Get a generator of all games"""
        assert self._open, "connection not opened"
        return (self._get_game(g) for g in self._games if g is not None)

    def get_game(self, id_or_name):
        """Get a game"""
        assert self._open, "connection not opened"
        if isinstance(id_or_name, int):
            return Game(self, id=id_or_name)
        else:
            return self._get_game(self._games_by_name[id_or_name])

    def get_profile(self, id):
        """Get a profile from its id"""
        assert self._open, "connection not opened"
        return Profile(self, id=id)

    def get_simulations(self, page_start=1, asc=False, column='job'):
        """Get information about current simulations"""
        assert self._open, "connection not opened"

        if column in {'folder', 'profile', 'simulator'}:
            sims = sorted(self._folders, key=lambda f: f[column],
                          reverse=not asc)
        elif asc:
            sims = self._folders
        else:
            sims = self._folders[::-1]
        return ({
            'folder': f['folder'],
            'job': float('nan'),
            'profile': f['profile'],
            'simulator': f['simulator'],
            'state': 'complete',
        } for f in itertools.islice(
            sims, 25 * (page_start - 1), None))

    def get_simulation(self, folder):
        """Get a simulation from its folder number"""
        assert self._open, "connection not opened"
        info = self._folders[folder]
        return {
            'error_message': '',
            'folder_number': info['folder'],
            'job': 'Not specified',
            'profile': info['profile'],
            'simulator_fullname': info['simulator'],
            'size': info['size'],
            'state': 'complete',
        }


class Simulator(_Base):
    """Get information about and modify EGTA Online Simulators"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _get_info(self):
        assert self._api._open, "connection not opened"
        try:
            return self._api._sims[self['id']]
        except IndexError:
            raise ValueError("simulator with id {:d} doesn't exist".format(
                self['id']))

    def get_info(self):
        """Return information about this simulator"""
        info = self._get_info()
        return Simulator(self._api, {
            'configuration': info['configuration'],
            'created_at': info['created_at'],
            'email': info['email'],
            'id': info['id'],
            'name': info['name'],
            'role_configuration': {r: s.copy() for r, s
                                   in info['role_configuration'].items()},
            'source': {
                'url': '/uploads/simulator/source/{:d}/{}-{}.zip'.format(
                    info['id'], info['name'], info['version'])},
            'updated_at': info['updated_at'],
            'url': 'https://{}/simulators/{:d}'.format(
                self._api._domain, info['id']),
            'version': info['version'],
        })

    def add_role(self, role):
        """Adds a role to the simulator"""
        info = self._get_info()
        with info['lock']:
            info['role_configuration'].setdefault(role, [])
            info['updated_at'] = _get_time_str()

    def remove_role(self, role):
        """Removes a role from the simulator"""
        info = self._get_info()
        with info['lock']:
            info['role_configuration'].pop(role)
            info['updated_at'] = _get_time_str()

    def add_strategy(self, role, strat):
        """Adds a strategy to the simulator"""
        info = self._get_info()
        with info['lock']:
            strats = info['role_configuration'][role]
            index = bisect.bisect_left(strats, strat)
            if index >= len(strats) or strats[index] != strat:
                strats.insert(index, strat)
            info['updated_at'] = _get_time_str()

    def add_dict(self, role_strat_dict):
        """Adds all of the roles and strategies in a dictionary"""
        with self._get_info()['lock']:
            for role, strats in role_strat_dict.items():
                self.add_role(role)
                for strat in set(strats):
                    self.add_strategy(role, strat)

    def remove_strategy(self, role, strategy):
        """Removes a strategy from the simulator"""
        info = self._get_info()
        with info['lock']:
            try:
                info['role_configuration'][role].remove(strategy)
                info['updated_at'] = _get_time_str()
            except ValueError:
                pass  # don't care

    def remove_dict(self, role_strat_dict):
        """Removes all of the strategies in a dictionary"""
        with self._get_info()['lock']:
            for role, strategies in role_strat_dict.items():
                for strategy in set(strategies):
                    self.remove_strategy(role, strategy)

    def create_generic_scheduler(
            self, name, active, process_memory, size, time_per_observation,
            observations_per_simulation, nodes=1, configuration={}):
        """Creates a generic scheduler and returns it"""
        return self._api.create_generic_scheduler(
            self['id'], name, active, process_memory, size,
            time_per_observation, observations_per_simulation, nodes,
            configuration)

    def create_game(self, name, size, configuration={}):
        """Creates a game and returns it"""
        return self._api.create_game(self['id'], name, size, configuration)


class Scheduler(_Base):
    """Get information and modify EGTA Online Scheduler"""

    def _get_info(self):
        assert self._api._open, "connection not opened"
        try:
            res = self._api._scheds[self['id']]
            if res is None:
                raise ValueError("scheduler with id {:d} doesn't exist".format(
                    self['id']))
            return res
        except IndexError:
            raise ValueError("scheduler with id {:d} doesn't exist".format(
                self['id']))

    def get_info(self):
        """Get a scheduler information"""
        info = self._get_info()
        return Scheduler(self._api, {
            'active': info['active'],
            'created_at': info['created_at'],
            'default_observation_requirement': (
                info['default_observation_requirement']),
            'id': info['id'],
            'name': info['name'],
            'nodes': info['nodes'],
            'observations_per_simulation': (
                info['observations_per_simulation']),
            'process_memory': info['process_memory'],
            'simulator_instance_id': info['simulator_instance_id'],
            'size': info['size'],
            'time_per_observation': info['time_per_observation'],
            'updated_at': info['updated_at'],
        })

    def get_requirements(self):
        info = self._get_info()
        return Scheduler(self._api, {
            'active': info['active'],
            'configuration': [[key, str(value)] for key, value
                              in info['configuration'].items()],
            'default_observation_requirement': (
                info['default_observation_requirement']),
            'id': info['id'],
            'name': info['name'],
            'nodes': info['nodes'],
            'observations_per_simulation': (
                info['observations_per_simulation']),
            'process_memory': info['process_memory'],
            'scheduling_requirements': [
                Profile(
                    self._api,
                    current_count=len(
                        self._api._profiles[pid]['observations']),
                    id=pid, requirement=req)
                for pid, req in info['scheduling_requirements'].items()],
            'simulator_id': info['simulator_id'],
            'size': info['size'],
            'time_per_observation': info['time_per_observation'],
            'type': 'GenericScheduler',
            'url': 'https://{}/generic_schedulers/{:d}'.format(
                self._api._domain, info['id']),
        })

    def _update_profile(self, prof, count):
        if len(prof['observations']) < count:
            # We need to schedule profiles
            info = self._get_info()
            sim = self._api._sims[info['simulator_id']]
            full_name = '{}-{}'.format(sim['name'], sim['version'])
            with prof['lock']:
                base = {
                    'profile': prof['assignment'],
                    'simulator': full_name,
                    'simulator_instance_id': info['simulator_instance_id'],
                    'size': prof['size'],
                }
                for _ in range(count - len(prof['observations'])):
                    payoffs = list(itertools.chain.from_iterable(
                        ((g['id'], random.random()) for _ in range(g['count']))
                        for g in prof['symmetry_groups']))
                    simul = self._api._get_folder()
                    simul.update(base)
                    simul['payoffs'] = payoffs
                    prof['observations'].append(simul)
                prof['updated_at'] = _get_time_str()

    def update(self, **kwargs):
        """Update the parameters of a given scheduler"""
        if 'active' in kwargs:
            kwargs['active'] = bool(kwargs['active'])
        info = self._get_info()
        # If activating, schedule all profiles
        if not info['active'] and kwargs['active']:
            for pid, count in info['scheduling_requirements'].items():
                self._update_profile(self._api._profiles[pid], count)
        with info['lock']:
            info.update(kwargs)
            info['updated_at'] = _get_time_str()

    def activate(self):
        self.update(active=True)

    def deactivate(self):
        self.update(active=False)

    def add_role(self, role, count):
        """Add a role with specific count to the scheduler"""
        info = self._get_info()
        with info['lock']:
            roles = info['role_configuration']
            simr = self._api._sims[info['simulator_id']]['role_configuration']
            assert sum(roles.values()) + count <= info['size']
            assert role not in roles
            assert role in simr
            roles[role] = count
            info['updated_at'] = _get_time_str()

    def remove_role(self, role):
        """Remove a role from the scheduler"""
        info = self._get_info()
        with info['lock']:
            if info['role_configuration'].pop(role, None) is not None:
                info['updated_at'] = _get_time_str()

    def destroy_scheduler(self):
        """Delete a generic scheduler"""
        info = self._get_info()
        with info['lock'], self._api._scheds_lock:
            self._api._scheds_by_name.pop(info['name'])
            self._api._scheds[self['id']] = None

    def _get_profile(self, assignment):
        """Get a profile"""
        info = self._get_info()
        inst_id = info['simulator_instance_id']
        lock, assignments = self._api._sim_insts[inst_id]
        with lock:
            if assignment in assignments:
                return assignments[assignment]
            else:
                symgrps = self._api._assign_to_symgrps(assignment)
                sim = self._api._sims[info['simulator_id']]
                sim_roles = sim['role_configuration']
                role_conf = {}
                for g in symgrps:
                    assert g['role'] in sim_roles
                    assert g['strategy'] in sim_roles[g['role']]
                    # FIXME use collections.Counter
                    role_conf[g['role']] = (
                        role_conf.get(g['role'], 0) + g['count'])
                assert role_conf == info['role_configuration']
                size = sum(role_conf.values())
                prof_time = _get_time_str()
                with self._api._profiles_lock:
                    prof_id = len(self._api._profiles)
                    prof = {
                        'assignment': assignment,
                        'created_at': prof_time,
                        'id': prof_id,
                        'lock': threading.Lock(),
                        'observations': [],
                        'role_configuration': role_conf,
                        'simulator_instance_id': inst_id,
                        'size': size,
                        'symmetry_groups': symgrps,
                        'updated_at': prof_time,
                    }
                    self._api._profiles.append(prof)
                    assignments[assignment] = prof
                    return prof

    def add_profile(self, assignment, count):
        """Add a profile to the scheduler"""
        if not isinstance(assignment, str):
            assignment = symgrps_to_assignment(assignment)
        info = self._get_info()
        prof = self._get_profile(assignment)

        if prof['id'] in info['scheduling_requirements']:
            # XXX This is how egta online behaves, but it seems non ideal
            return Profile(self._api, {
                'assignment': prof['assignment'],
                'created_at': prof['created_at'],
                'id': prof['id'],
                'observations_count': len(prof['observations']),
                'role_configuration': prof['role_configuration'].copy(),
                'simulator_instance_id': prof['simulator_instance_id'],
                'size': prof['size'],
                'updated_at': prof['updated_at'],
            })

        with info['lock']:
            info['scheduling_requirements'][prof['id']] = count
            info['updated_at'] = _get_time_str()
        if info['active']:
            self._update_profile(prof, count)

        return Profile(self._api, {
            'assignment': prof['assignment'],
            'created_at': prof['created_at'],
            'id': prof['id'],
            'observations_count': len(prof['observations']),
            'role_configuration': prof['role_configuration'].copy(),
            'simulator_instance_id': prof['simulator_instance_id'],
            'size': prof['size'],
            'updated_at': prof['updated_at'],
        })

    def update_profile(self, profile, count):
        """Update the requested count of a profile object"""
        if isinstance(profile, int):
            profile_id = profile
            assignment = (self._api.get_profile(profile)
                          .get_info()['assignment'])

        elif isinstance(profile, str):
            assignment = profile
            profile_id = self.add_profile(assignment, 0)['id']

        elif any(k in profile for k
                 in ['id', 'assignment', 'symmetry_groups']):
            assignment = (profile.get('assignment', None) or
                          profile.get('symmetry_groups', None) or
                          self._api.get_profile(profile)
                          .get_info()['assignment'])
            profile_id = (profile.get('id', None) or
                          self.add_profile(assignment, 0)['id'])

        else:
            assignment = profile
            profile_id = self.add_profile(assignment, 0)['id']

        self.remove_profile(profile_id)
        return self.add_profile(assignment, count)

    def remove_profile(self, profile):
        """Removes a profile from a scheduler"""
        if not isinstance(profile, int):
            profile = profile['id']
        info = self._get_info()
        with info['lock']:
            if info['scheduling_requirements'].pop(profile, None) is not None:
                info['updated_at'] = _get_time_str()

    def remove_all_profiles(self):
        """Removes all profiles from a scheduler"""
        info = self._get_info()
        with info['lock']:
            if info['scheduling_requirements']:
                info['updated_at'] = _get_time_str()
            info['scheduling_requirements'].clear()

    def create_game(self, name=None):
        """Creates a game with the same parameters of the scheduler

        If name is unspecified, it will copy the name from the scheduler. This
        will fail if there's already a game with that name."""
        info = self._get_info()
        return self._api.create_game(
            info['simulator_id'], info['name'] if name is None else name,
            info['size'], info['configuration'])


class Profile(_Base):
    """Class for manipulating profiles"""

    def _get_info(self):
        assert self._api._open, "connection not opened"
        try:
            return self._api._profiles[self['id']]
        except IndexError:
            raise ValueError("profile with id {:d} doesn't exist".format(
                self['id']))

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
        info = self._get_info()
        return Profile(self._api, {
            'assignment': info['assignment'],
            'created_at': info['created_at'],
            'id': info['id'],
            'observations_count': len(info['observations']),
            'role_configuration': {r: str(c) for r, c
                                   in info['role_configuration'].items()},
            'simulator_instance_id': info['simulator_instance_id'],
            'size': info['size'],
            'updated_at': info['updated_at'],
        })

    def get_summary(self):
        info = self._get_info()
        payoffs = {
            sid: (mean, stddev)
            for sid, mean, stddev
            in _mean_id(itertools.chain.from_iterable(
                obs['payoffs'] for obs in info['observations']))}
        symgrps = [g.copy() for g in info['symmetry_groups']]
        for grp in symgrps:
            mean, stddev = payoffs[grp['id']]
            grp['payoff'] = mean
            grp['payoff_sd'] = stddev
        return Profile(self._api, {
            'id': info['id'],
            'simulator_instance_id': info['simulator_instance_id'],
            'symmetry_groups': symgrps,
            'observations_count': len(info['observations']),
        })

    def get_observations(self):
        info = self._get_info()
        observations = [{
            'extended_features': {},
            'features': {},
            'symmetry_groups': [{
                'id': sid,
                'payoff': pay,
                'payoff_sd': None,
            } for sid, pay, _ in _mean_id(obs['payoffs'])]
        } for obs in info['observations']]
        return Profile(self._api, {
            'id': self['id'],
            'simulator_instance_id': info['simulator_instance_id'],
            'symmetry_groups': info['symmetry_groups'],
            'observations': observations,
        })

    def get_full_data(self):
        info = self._get_info()
        observations = [{
            'extended_features': {},
            'features': {},
            'players': [{
                'e': {},
                'f': {},
                'p': pay,
                'sid': sid,
            } for sid, pay in obs['payoffs']]
        } for obs in info['observations']]
        return Profile(self._api, {
            'id': info['id'],
            'simulator_instance_id': info['simulator_instance_id'],
            'symmetry_groups': info['symmetry_groups'],
            'observations': observations,
        })


class Game(_Base):
    """Get information and manipulate EGTA Online Games"""

    def _get_info(self):
        assert self._api._open, "connection not opened"
        try:
            res = self._api._games[self['id']]
            if res is None:
                raise ValueError("game with id {:d} doesn't exist".format(
                    self['id']))
            return res
        except IndexError:
            raise ValueError("game with id {:d} doesn't exist".format(
                self['id']))

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
        info = self._get_info()
        return Game(self._api, {
            'created_at': info['created_at'],
            'id': info['id'],
            'name': info['name'],
            'simulator_instance_id': info['simulator_instance_id'],
            'size': info['size'],
            'subgames': None,
            'updated_at': info['updated_at'],
            'url': 'https://{}/games/{:d}'.format(
                self._api._domain, info['id']),
        })

    def _get_data(self, func, keys):
        info = self._get_info()
        inst_id = info['simulator_instance_id']
        sim = self._api._sims[info['simulator_id']]
        roles = [{
            'name': r,
            'count': c,
            'strategies': s,
        } for r, (c, s) in sorted(info['role_configuration'].items())]

        strats = {r: set(s) for r, (_, s)
                  in info['role_configuration'].items()}
        counts = {r: c for r, (c, _) in info['role_configuration'].items()}
        profs = []
        for prof in self._api._sim_insts[inst_id][1].values():
            counts_left = counts.copy()
            for grp in prof['symmetry_groups']:
                if grp['strategy'] not in strats.get(grp['role'], ()):
                    continue
                counts_left[grp['role']] -= grp['count']
            if all(c == 0 for c in counts_left.values()):
                prof = func(Profile(self._api, prof))
                profs.append(Profile(self._api, {k: prof[k] for k in keys}))

        return Game(self._api, {
            'id': info['id'],
            'configuration': info['configuration'],
            'profiles': profs,
            'roles': roles,
            'simulator_fullname': '{}-{}'.format(sim['name'], sim['version']),
            'name': info['name'],
            'url': 'https://{}/games/{:d}'.format(
                self._api._domain, info['id']),
        })

    def get_summary(self):
        return self._get_data(Profile.get_summary,
                              ['id', 'observations_count', 'symmetry_groups'])

    def get_observations(self):
        return self._get_data(Profile.get_observations,
                              ['id', 'observations', 'symmetry_groups'])

    def get_full_data(self):
        return self._get_data(Profile.get_full_data,
                              ['id', 'observations', 'symmetry_groups'])

    def add_role(self, role, count):
        """Adds a role to the game"""
        info = self._get_info()
        with info['lock']:
            roles = info['role_configuration']
            simr = self._api._sims[info['simulator_id']]['role_configuration']
            assert sum(c for c, _ in roles.values()) + count <= info['size']
            assert role not in roles, "can't add an existing role"
            assert role in simr
            roles[role] = (count, [])
            info['updated_at'] = _get_time_str()

    def remove_role(self, role):
        """Removes a role from the game"""
        info = self._get_info()
        with info['lock']:
            if info['role_configuration'].pop(role, None) is not None:
                info['updated_at'] = _get_time_str()

    def add_strategy(self, role, strat):
        """Adds a strategy to the game"""
        info = self._get_info()
        with info['lock']:
            _, strats = info['role_configuration'][role]
            roles = self._api._sims[info['simulator_id']]['role_configuration']
            assert strat in roles[role]
            index = bisect.bisect_left(strats, strat)
            if index >= len(strats) or strats[index] != strat:
                strats.insert(index, strat)
            info['updated_at'] = _get_time_str()

    def add_dict(self, role_strat_dict):
        """Attempts to add all of the strategies in a dictionary"""
        with self._get_info()['lock']:
            for role, strategies in role_strat_dict.items():
                for strategy in strategies:
                    self.add_strategy(role, strategy)

    def remove_strategy(self, role, strat):
        """Removes a strategy from the game"""
        info = self._get_info()
        with info['lock']:
            try:
                info['role_configuration'][role][1].remove(strat)
                info['updated_at'] = _get_time_str()
            except ValueError:
                pass  # don't care

    def remove_dict(self, role_strat_dict):
        """Removes all of the strategies in a dictionary"""
        with self._get_info()['lock']:
            for role, strategies in role_strat_dict.items():
                for strategy in set(strategies):
                    self.remove_strategy(role, strategy)

    def destroy_game(self):
        """Delete a game"""
        info = self._get_info()
        with info['lock'], self._api._games_lock:
            self._api._games_by_name.pop(info['name'])
            self._api._games[self['id']] = None


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
