"""Python package to handle python interface to egta online api"""
import collections
import inflection
import itertools
import json
import logging
import requests
import sys
import time
from os import path


from lxml import etree


_AUTH_FILE = '.egta_auth_token'
_SEARCH_PATH = [_AUTH_FILE, path.expanduser(path.join('~', _AUTH_FILE))]


def _load_auth_token(auth_token):
    if auth_token is not None:
        return auth_token
    for file_name in _SEARCH_PATH:
        if path.isfile(file_name):
            with open(file_name) as f:
                return f.read().strip()
    raise ValueError("No auth token file found at any of: {}".format(
        ', '.join(_SEARCH_PATH)))


def _encode_data(data):
    """Takes data in nested dictionary form, and converts it for egta

    All dictionary keys must be strings. This call is non destructive.
    """
    encoded = {}
    for k, val in data.items():
        if isinstance(val, dict):
            for inner_key, inner_val in _encode_data(val).items():
                encoded['{0}[{1}]'.format(k, inner_key)] = inner_val
        else:
            encoded[k] = val
    return encoded


class _Base(dict):
    """A base api object"""

    def __init__(self, api, *args, **kwargs):
        assert api is not None and id is not None
        self._api = api
        super().__init__(*args, **kwargs)

    def __getattr__(self, name):
        return self[name]


class EgtaOnlineApi(object):
    """Class that allows access to an Egta Online server

    This can be used as context manager to automatically close the active
    session."""

    def __init__(self, auth_token=None, domain='egtaonline.eecs.umich.edu',
                 logLevel=0, retry_on=(504,), num_tries=20, retry_delay=60,
                 retry_backoff=1.2):
        self.domain = domain
        self._auth_token = _load_auth_token(auth_token)
        self._retry_on = frozenset(retry_on)
        self._num_tries = num_tries
        self._retry_delay = 20
        self._retry_backoff = 1.2

        self._session = requests.Session()
        self._log = logging.getLogger(self.__class__.__name__)
        self._log.setLevel(40 - logLevel * 10)
        self._log.addHandler(logging.StreamHandler(sys.stderr))

        # This authenticates us for the duration of the session
        resp = self._session.get('https://{domain}'.format(domain=self.domain),
                                 data={'auth_token': self._auth_token})
        assert '<a href="/users/sign_in">Sign in</a>' not in resp.text, \
            "Couldn't authenticate with auth_token: {}".format(
                self._auth_token)

    def close(self):
        """Closes the active session"""
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._session.close()

    def _retry_request(self, verb, url, data):
        data = _encode_data(data)
        response = None
        timeout = self._retry_delay
        for i in range(self._num_tries):
            self._log.info('%s request to %s with data %s', verb, url, data)
            try:
                response = self._session.request(verb, url, data=data)
                if response.status_code not in self._retry_on:
                    self._log.info('response "%s"', response.text)
                    return response
                self._log.info('%s request to %s with data %s failed with '
                               'status %d, retrying in %.0f seconds', verb,
                               url, data, response.status_code, timeout)
            except ConnectionError as ex:
                self._log.info('%s request to %s with data %s failed with '
                               'exception %s %s, retrying in %.0f seconds',
                               verb, url, data, ex.__class__.__name__, ex,
                               timeout)
            time.sleep(timeout)
            timeout *= self._retry_backoff
        return response

    def _request(self, verb, api, data={}):
        """Convenience method for making requests"""
        url = 'https://{domain}/api/v3/{endpoint}'.format(
            domain=self.domain, endpoint=api)
        return self._retry_request(verb, url, data)

    def _non_api_request(self, verb, api, data={}):
        url = 'https://{domain}/{endpoint}'.format(
            domain=self.domain, endpoint=api)
        return self._retry_request(verb, url, data)

    def get_simulators(self):
        """Get a generator of all simulators"""
        resp = self._request('get', 'simulators')
        resp.raise_for_status()
        return (Simulator(self, s) for s in resp.json()['simulators'])

    def get_simulator(self, id_or_name, version=None):
        """Get a simulator

        If `id_or_name` is an int, i.e. an id, then this will return a
        simulator with that id, otherwise it will look for a simulator named
        `id_or_name` with an optional version. An exception is thrown is a
        simulator with that name/version doesn't exit, or only name is
        specified and there are multiple versions.
        """
        if isinstance(id_or_name, int):
            return Simulator(self, id=id_or_name)

        elif version is not None:
            sims = [sim for sim in self.get_simulators()
                    if sim.name == id_or_name
                    and sim.version == version]
            if sims:
                return sims[0]
            else:
                raise ValueError("Simulator {} version {} does not exist"
                                 .format(id_or_name, version))
        else:
            sims = [sim for sim in self.get_simulators()
                    if sim.name == id_or_name]
            if len(sims) > 1:
                raise ValueError(
                    "Simulator {} has multiple versions: {}"
                    .format(id_or_name, ', '.join(s.version for s in sims)))
            elif sims:
                return sims[0]
            else:
                raise ValueError("Simulator {} does not exist"
                                 .format(id_or_name))

    def get_generic_schedulers(self):
        """Get a generator of all generic schedulers"""
        resp = self._request('get', 'generic_schedulers')
        resp.raise_for_status()
        return (Scheduler(self, s) for s in resp.json()['generic_schedulers'])

    def get_scheduler(self, id_or_name):
        """Get a scheduler with an or name

        If `id_or_name` is an int, i.e. an id, then this will return a
        scheduler with that id, otherwise it will look for a generic scheduler
        with that name. An exception is raised if no generic scheduler exists
        with that name.
        """
        if isinstance(id_or_name, int):
            return Scheduler(self, id=id_or_name)
        else:
            scheds = [sched for sched in self.get_generic_schedulers()
                      if sched.name == id_or_name]
            if scheds:
                return scheds[0]
            else:
                raise ValueError("Generic scheduler {} does not exist"
                                 .format(id_or_name))

    def create_generic_scheduler(
            self, sim_id, name, active, process_memory, size,
            time_per_observation, observations_per_simulation, nodes=1,
            configuration={}):
        """Creates a generic scheduler and returns it

        Parameters
        ----------
        sim_id : int
            The simulator id for this scheduler.
        name : str
            The name for the scheduler.
        active : boolean
            True or false, specifying whether the scheduler is initially
            active.
        process_memory : int
            The amount of memory in MB that your simulations need.
        size : int
            The number of players for the scheduler.
        time_per_observation : int
            The time you require to take a single observation in seconds.
        observations_per_simulation : int
            The number of observations to take per simulation run.
        nodes : int, optional
            The number of nodes required to run one of your simulations. If
            unsure, this should be 1.
        configuration : {str: str}, optional
            A dictionary representation that sets all the run-time parameters
            for this scheduler. Keys will default to the simulation default
            parameters, but new configurations parameters can be added."""
        conf = self.get_simulator(sim_id).get_info().configuration
        conf.update(configuration)
        resp = self._request(
            'post',
            'generic_schedulers',
            data={'scheduler': {
                'simulator_id': sim_id,
                'name': name,
                'active': int(active),
                'process_memory': process_memory,
                'size': size,
                'time_per_observation': time_per_observation,
                'observations_per_simulation': observations_per_simulation,
                'nodes': nodes,
                'default_observation_requirement': 0,
                'configuration': conf,
            }})
        resp.raise_for_status()
        return Scheduler(self, resp.json())

    def get_games(self):
        """Get a generator of all games"""
        resp = self._request('get', 'games')
        resp.raise_for_status()
        return (Game(self, g) for g in resp.json()['games'])

    def get_game(self, id_or_name):
        """Get a game

        if `id_or_name` is an int e.g. an id, a game with that id is returned,
        otherwise this searches for a game named `id_or_name` and throws an
        exception if none is found."""
        if isinstance(id_or_name, int):
            return Game(self, id=id_or_name)
        else:
            games = [g for g in self.get_games() if g.name == id_or_name]
            if games:
                return games[0]
            else:
                raise ValueError("Game {} does not exist".format(id_or_name))

    def create_game(self, sim_id, name, size, configuration={}):
        """Creates a game and returns it

        Parameters
        ----------
        sim_id : int
            The simulator id for this game.
        name : str
            The name for the game.
        size : int
            The number of players in this game.
        configuration : {str: str}, optional
            A dictionary representation that sets all the run-time parameters
            for this scheduler. Keys will default to the simulation default
            parameters, but new configurations parameters can be added."""
        conf = self.get_simulator(sim_id).get_info().configuration
        conf.update(configuration)
        resp = self._non_api_request(
            'post',
            'games',
            data={
                'auth_token': self._auth_token,  # Necessary for some reason
                'game': {
                    'name': name,
                    'size': size,
                },
                'selector': {
                    'simulator_id': sim_id,
                    'configuration': conf,
                },
            })
        resp.raise_for_status()
        game_id = int(etree.HTML(resp.text)
                      .xpath('//div[starts-with(@id, "game_")]')[0]
                      .attrib['id'][5:])
        return Game(self, id=game_id)

    def get_profile(self, id):
        """Get a profile from its id

        `id`s can be found with a scheduler's `get_requirements`, when adding a
        profile to a scheduler, or from a game with sufficient granularity."""
        return Profile(self, id=id)

    _mapping = collections.OrderedDict((
        ('state', 'state'),
        ('profile', 'profiles.assignment'),
        ('simulator', 'simulator_fullname'),
        ('folder', 'id'),
        ('job', 'job_id'),
    ))

    @staticmethod
    def _parse(res):
        """Converts N/A to `nan` and otherwise tries to parse integers"""
        try:
            return int(res)
        except ValueError:
            if res.lower() == 'n/a':
                return float('nan')
            else:
                return res

    def get_simulations(self, page_start=1, asc=False, column='job_id'):
        """Get information about current simulations

        `page_start` must be at least 1. `column` should be
        one of 'job', 'folder', 'profile', or 'state'."""
        column = self._mapping.get(column, column)
        data = {
            'direction': 'ASC' if asc else 'DESC'
        }
        if column is not None:
            data['sort'] = column
        for page in itertools.count(page_start):
            data['page'] = page
            resp = self._non_api_request('get', 'simulations', data=data)
            resp.raise_for_status()
            rows = etree.HTML(resp.text).xpath('//tbody/tr')
            if not rows:
                break  # Empty page implies we're done
            for row in rows:
                res = (self._parse(''.join(e.itertext()))
                       for e in row.getchildren())
                yield dict(zip(self._mapping, res))

    def get_simulation(self, folder):
        """Get a simulation from its folder number"""
        resp = self._non_api_request(
            'get',
            'simulations/{folder}'.format(folder=folder))
        resp.raise_for_status()
        info = etree.HTML(resp.text).xpath(
            '//div[@class="show_for simulation"]/p')
        parsed = (''.join(e.itertext()).split(':', 1) for e in info)
        return {key.lower().replace(' ', '_'): self._parse(val.strip())
                for key, val in parsed}


class Simulator(_Base):
    """Get information about and modify EGTA Online Simulators"""

    def get_info(self):
        """Return information about this simulator

        If the id is unknown this will search all simulators for one with the
        same name and optionally version. If version is unspecified, but only
        one simulator with that name exists, this lookup should still succeed.
        This returns a new simulator object, but will update the id of the
        current simulator if it was undefined."""
        resp = self._api._request(
            'get', 'simulators/{sim:d}.json'.format(sim=self.id))
        resp.raise_for_status()
        result = resp.json()
        result['url'] = '/'.join(('https:/', self._api.domain, 'simulators',
                                  str(result['id'])))
        return Simulator(self._api, result)

    def add_role(self, role):
        """Adds a role to the simulator"""
        resp = self._api._request(
            'post',
            'simulators/{sim:d}/add_role.json'.format(sim=self.id),
            data={'role': role})
        resp.raise_for_status()

    def remove_role(self, role):
        """Removes a role from the simulator"""
        resp = self._api._request(
            'post',
            'simulators/{sim:d}/remove_role.json'.format(sim=self.id),
            data={'role': role})
        resp.raise_for_status()

    def _add_strategy(self, role, strategy):
        """Like `add_strategy` but without the duplication check"""
        resp = self._api._request(
            'post',
            'simulators/{sim:d}/add_strategy.json'.format(sim=self.id),
            data={'role': role, 'strategy': strategy})
        resp.raise_for_status()

    def add_strategy(self, role, strategy):
        """Adds a strategy to the simulator

        If `check_for_dups` is set to false, this won't prevent adding
        duplicate strategies to a role."""
        if strategy not in self.get_info().role_configuration[role]:
            self._add_strategy(role, strategy)

    def add_dict(self, role_strat_dict):
        """Adds all of the roles and strategies in a dictionary

        The dictionary should be of the form {role: [strategies]}."""
        existing = self.get_info().role_configuration
        for role, strategies in role_strat_dict.items():
            existing_strats = set(existing.get(role, ()))
            self.add_role(role)
            for strategy in set(strategies).difference(existing_strats):
                self._add_strategy(role, strategy)

    def remove_strategy(self, role, strategy):
        """Removes a strategy from the simulator"""
        resp = self._api._request(
            'post',
            'simulators/{sim:d}/remove_strategy.json'.format(sim=self.id),
            data={'role': role, 'strategy': strategy})
        resp.raise_for_status()

    def remove_dict(self, role_strat_dict):
        """Removes all of the strategies in a dictionary

        The dictionary should be of the form {role: [strategies]}. Empty roles
        are not removed."""
        for role, strategies in role_strat_dict.items():
            for strategy in set(strategies):
                self.remove_strategy(role, strategy)

    def create_generic_scheduler(
            self, name, active, process_memory, size, time_per_observation,
            observations_per_simulation, nodes=1, configuration={}):
        """Creates a generic scheduler and returns it

        See the method in `Api` for details."""
        return self._api.create_generic_scheduler(
            self.id, name, active, process_memory, size, time_per_observation,
            observations_per_simulation, nodes, configuration)

    def create_game(self, name, size, configuration={}):
        """Creates a game and returns it

        See the method in `Api` for details."""
        return self._api.create_game(self.id, name, size, configuration)


class Scheduler(_Base):
    """Get information and modify EGTA Online Scheduler"""

    def get_info(self):
        """Get a scheduler information"""
        resp = self._api._request(
            'get',
            'schedulers/{sched_id}.json'.format(sched_id=self.id))
        resp.raise_for_status()
        return Scheduler(self._api, resp.json())

    def get_requirements(self):
        resp = self._api._request(
            'get',
            'schedulers/{sched_id}.json'.format(sched_id=self.id),
            {'granularity': 'with_requirements'})
        resp.raise_for_status()
        result = resp.json()
        reqs = result.get('scheduling_requirements', None) or ()
        result['scheduling_requirements'] = [
            Profile(self._api, prof, id=prof.pop('profile_id'))
            for prof in reqs]
        result['url'] = '/'.join(('https:/', self._api.domain,
                                  inflection.underscore(result['type']) + 's',
                                  str(result['id'])))
        return Scheduler(self._api, result)

    def update(self, **kwargs):
        """Update the parameters of a given scheduler

        kwargs are any of the mandatory arguments for create_generic_scheduler,
        except for configuration, that cannont be updated for whatever
        reason."""
        if 'active' in kwargs:
            kwargs['active'] = int(kwargs['active'])
        resp = self._api._request(
            'put',
            'generic_schedulers/{sid:d}.json'.format(sid=self.id),
            data={'scheduler': kwargs})
        resp.raise_for_status()

    def activate(self):
        self.update(active=1)

    def deactivate(self):
        self.update(active=0)

    def add_role(self, role, count):
        """Add a role with specific count to the scheduler"""
        resp = self._api._request(
            'post',
            'generic_schedulers/{sid:d}/add_role.json'.format(sid=self.id),
            data={'role': role, 'count': count})
        resp.raise_for_status()

    def remove_role(self, role):
        """Remove a role from the scheduler"""
        resp = self._api._request(
            'post',
            'generic_schedulers/{sid:d}/remove_role.json'.format(sid=self.id),
            data={'role': role})
        resp.raise_for_status()

    def destroy_scheduler(self):
        """Delete a generic scheduler"""
        resp = self._api._request(
            'delete',
            'generic_schedulers/{sid:d}.json'.format(sid=self.id))
        resp.raise_for_status()

    def add_profile(self, assignment, count):
        """Add a profile to the scheduler

        assignment can be an assignment string or a symmetry group dictionary.
        If the profile already exists, this won't change the requested
        count."""
        if not isinstance(assignment, str):
            assignment = symgrps_to_assignment(assignment)
        resp = self._api._request(
            'post',
            'generic_schedulers/{sid:d}/add_profile.json'.format(
                sid=self.id),
            data={
                'assignment': assignment,
                'count': count
            })
        resp.raise_for_status()
        return Profile(self._api, resp.json(), assignment=assignment)

    def update_profile(self, profile, count):
        """Update the requested count of a profile object

        If profile is an int, it's treated as an id. If it's a string, it's
        treated as an assignment, if it's a profile object or dictionary and
        has at least one of id, assignment or symmetry_groups, it uses those
        fields appropriately, otherwise it's treated as a symmetry group. This
        should still work if the profile doesn't exist, but it's not as
        efficient as using add_profile."""
        if isinstance(profile, int):
            profile_id = profile
            assignment = (self._api.get_profile(profile).get_info()
                          .symmetry_groups)

        elif isinstance(profile, str):
            assignment = profile
            profile_id = self.add_profile(assignment, 0).id

        elif any(k in profile for k
                 in ['id', 'assignment', 'symmetry_groups']):
            assignment = (profile.get('assignment', None) or
                          profile.get('symmetry_groups', None) or
                          self._api.get_profile(profile).get_info()
                          .symmetry_groups)
            profile_id = (profile.get('id', None) or
                          self.add_profile(assignment, 0).id)

        else:
            assignment = profile
            profile_id = self.add_profile(assignment, 0).id

        self.remove_profile(profile_id)
        return self.add_profile(assignment, count)

    def remove_profile(self, profile):
        """Removes a profile from a scheduler

        `profile` can be an int or a profile object."""
        if not isinstance(profile, int):
            profile = profile['id']
        resp = self._api._request(
            'post',
            'generic_schedulers/{sid:d}/remove_profile.json'.format(
                sid=self.id),
            data={'profile_id': profile})
        resp.raise_for_status()

    def remove_all_profiles(self):
        """Removes all profiles from a scheduler"""
        for profile in self.get_requirements().scheduling_requirements:
            self.remove_profile(profile)

    def create_game(self, name=None):
        """Creates a game with the same parameters of the scheduler

        If name is unspecified, it will copy the name from the scheduler. This
        will fail if there's already a game with that name."""
        reqs = self
        if any(k not in reqs for k
               in ['configuration', 'name', 'simulator_id', 'size']):
            reqs = self.get_requirements()
        return self._api.create_game(reqs.simulator_id,
                                     reqs.name if name is None else name,
                                     reqs.size, dict(reqs.configuration))


class Profile(_Base):
    """Class for manipulating profiles"""

    def get_info(self, granularity='structure'):
        """Gets information about the profile

        `granularity` is the same as in a game, and can be one of:

        structure    - returns the profile information but no payoff data.
                       (default)
        summary      - returns the one payoff for each symmetry group.
        observations - returns payoff data for each symmetry group and each
                       observation.
        full         - returns all data about profile.
        """
        resp = self._api._request(
            'get',
            'profiles/{pid:d}.json'.format(pid=self.id),
            {'granularity': granularity})
        resp.raise_for_status()
        return Profile(self._api, resp.json())


class Game(_Base):
    """Get information and manipulate EGTA Online Games"""

    def get_info(self, granularity='structure'):
        """Gets game information from EGTA Online

        granularity can be one of:

        structure    - returns the game information but no profile information.
                       (default)
        summary      - returns the game information and profiles with
                       aggregated payoffs.
        observations - returns the game information and profiles with data
                       aggregated at the observation level.
        full         - returns the game information and profiles with complete
                       observation information
        """
        # This call breaks convention because the api is broken, so we use
        # a different api.
        resp = self._api._non_api_request(
            'get',
            'games/{gid:d}.json'.format(gid=self.id),
            data={'granularity': granularity})
        resp.raise_for_status()
        if granularity == 'structure':
            result = json.loads(resp.json())
        else:
            result = resp.json()
            result['profiles'] = [
                Profile(self._api, p) for p
                in result['profiles'] or ()]
        result['url'] = '/'.join(('https:/', self._api.domain, 'games',
                                  str(result['id'])))
        return Game(self._api, result)

    def get_structure(self):
        return self.get_info('structure')

    def get_summary(self):
        return self.get_info('summary')

    def get_observations(self):
        return self.get_info('observations')

    def get_full_data(self):
        return self.get_info('full')

    def add_role(self, role, count):
        """Adds a role to the game"""
        resp = self._api._request(
            'post',
            'games/{game:d}/add_role.json'.format(game=self.id),
            data={'role': role, 'count': count})
        resp.raise_for_status()

    def remove_role(self, role):
        """Removes a role from the game"""
        resp = self._api._request(
            'post',
            'games/{game:d}/remove_role.json'.format(game=self.id),
            data={'role': role})
        resp.raise_for_status()

    def add_strategy(self, role, strategy):
        """Adds a strategy to the game"""
        resp = self._api._request(
            'post',
            'games/{game:d}/add_strategy.json'.format(game=self.id),
            data={'role': role, 'strategy': strategy})
        resp.raise_for_status()

    def add_dict(self, role_strat_dict):
        """Attempts to add all of the strategies in a dictionary

        The dictionary should be of the form {role: [strategies]}."""
        for role, strategies in role_strat_dict.items():
            for strategy in strategies:
                self.add_strategy(role, strategy)

    def remove_strategy(self, role, strategy):
        """Removes a strategy from the game"""
        resp = self._api._request(
            'post',
            'games/{game:d}/remove_strategy.json'.format(game=self.id),
            data={'role': role, 'strategy': strategy})
        resp.raise_for_status()

    def remove_dict(self, role_strat_dict):
        """Removes all of the strategies in a dictionary

        The dictionary should be of the form {role: [strategies]}. Empty roles
        are not removed."""
        for role, strategies in role_strat_dict.items():
            for strategy in set(strategies):
                self.remove_strategy(role, strategy)

    def destroy_game(self):
        """Delete a game"""
        resp = self._api._non_api_request(
            'post',
            'games/{game:d}'.format(game=self.id),
            data={
                'auth_token': self._api._auth_token,  # Necessary
                '_method': 'delete',
            })
        resp.raise_for_status()


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
