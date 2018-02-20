"""Python package to handle python interface to egta online api"""
import collections
import inflection
import itertools
import json
import logging
import random
import requests
import string
import time
from os import path


from lxml import etree


_auth_file = '.egta_auth_token'
_search_path = [_auth_file, path.expanduser(path.join('~', _auth_file))]
_log = logging.getLogger(__name__)


def _load_auth_token(auth_token):
    if auth_token is not None:  # pragma: no cover
        return auth_token
    for file_name in _search_path:  # pragma: no branch
        if path.isfile(file_name):
            with open(file_name) as f:
                return f.read().strip()
    return '<no auth_token supplied or found in any of: {}>'.format(  # pragma: no cover # noqa
        ', '.join(_search_path))


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
        super().__init__(*args, **kwargs)
        self._api = api
        assert 'id' in self


class EgtaOnlineApi(object):
    """Class that allows access to an Egta Online server

    This can be used as context manager to automatically close the active
    session."""

    def __init__(self, auth_token=None, domain='egtaonline.eecs.umich.edu',
                 retry_on=(504,), num_tries=20, retry_delay=60,
                 retry_backoff=1.2):
        self.domain = domain
        self._auth_token = _load_auth_token(auth_token)
        self._retry_on = frozenset(retry_on)
        self._num_tries = num_tries
        self._retry_delay = 20
        self._retry_backoff = 1.2

        self._session = None

    def __enter__(self):
        self._session = requests.Session()
        # This authenticates us for the duration of the session
        resp = self._session.get('https://{domain}'.format(domain=self.domain),
                                 data={'auth_token': self._auth_token})
        resp.raise_for_status()
        assert '<a href="/users/sign_in">Sign in</a>' not in resp.text, \
            "Couldn't authenticate with auth_token: '{}'".format(
                self._auth_token)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._session is not None:  # pragma: no branch
            self._session.close()

    def _retry_request(self, verb, url, data):
        data = _encode_data(data)
        response = None
        timeout = self._retry_delay
        for i in range(self._num_tries):
            _log.debug('%s request to %s with data %s', verb, url, data)
            try:
                response = self._session.request(verb, url, data=data)
                if response.status_code not in self._retry_on:
                    _log.debug('response "%s"', response.text)
                    return response
                _log.debug('%s request to %s with data %s failed with status'
                           '%d, retrying in %.0f seconds', verb, url, data,
                           response.status_code, timeout)  # pragma: no cover
            except ConnectionError as ex:  # pragma: no cover
                _log.debug('%s request to %s with data %s failed with '
                           'exception %s %s, retrying in %.0f seconds', verb,
                           url, data, ex.__class__.__name__, ex, timeout)
            time.sleep(timeout)  # pragma: no cover
            timeout *= self._retry_backoff  # pragma: no cover
        # TODO catch session level errors and reinitialize it
        raise ConnectionError()  # pragma: no cover

    def _request(self, verb, api, data={}):
        """Convenience method for making requests"""
        url = 'https://{domain}/api/v3/{endpoint}'.format(
            domain=self.domain, endpoint=api)
        return self._retry_request(verb, url, data)

    def _non_api_request(self, verb, api, data={}):
        url = 'https://{domain}/{endpoint}'.format(
            domain=self.domain, endpoint=api)
        return self._retry_request(verb, url, data)

    def _json_non_api_request(self, verb, api, data={}, *, retries=10, sleep=1,
                              inc=1.2):
        """non api request for json"""
        assert retries > 0, "retries must be positive"
        exception = None
        for _ in range(retries):  # pragma: no branch
            resp = self._non_api_request(verb, api, data)
            resp.raise_for_status()
            try:
                return resp.json()
            except json.decoder.JSONDecodeError as ex:
                exception = ex
                time.sleep(sleep)
                sleep *= inc
        raise exception

    def _html_non_api_request(self, verb, api, data={}):
        """non api request for xml"""
        resp = self._non_api_request(verb, api, data)
        resp.raise_for_status()
        return etree.HTML(resp.text)

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

        sims = (s for s in self.get_simulators() if s['name'] == id_or_name)
        if version is not None:
            sims = (s for s in sims if s['version'] == version)
        try:
            sim = next(sims)
        except StopIteration:
            raise ValueError("No simulators found matching {} {}".format(
                id_or_name, version))
        try:
            next(sims)
            raise ValueError("Many simulations found with name {}".format(
                id_or_name))
        except StopIteration:
            return sim

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
        try:
            return next(s for s in self.get_generic_schedulers()
                        if s['name'] == id_or_name)
        except StopIteration:
            raise ValueError(
                "Generic scheduler {} does not exist".format(id_or_name))

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
            The maximum number of observations to take per simulation run. If a
            profile is added with fewer observations than this, they will all
            be scheduled at once, if more, then this number will be scheduler,
            and only after they complete successfully will more be added.
        nodes : int, optional
            The number of nodes required to run one of your simulations. If
            unsure, this should be 1.
        configuration : {str: str}, optional
            A dictionary representation that sets all the run-time parameters
            for this scheduler. Keys will default to the simulation default
            parameters, but new configurations parameters can be added."""
        conf = self.get_simulator(sim_id).get_info()['configuration']
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
            games = (g for g in self.get_games() if g['name'] == id_or_name)
            try:
                return next(games)
            except StopIteration:
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
        conf = self.get_simulator(sim_id).get_info()['configuration']
        conf.update(configuration)
        resp = self._html_non_api_request(
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
        game_id = int(resp.xpath('//div[starts-with(@id, "game_")]')[0]
                      .attrib['id'][5:])
        # We already have to make one round trip to create the game, might as
        # well return a reasonable amount of information, because we don't get
        # it from the non-api
        return Game(self, id=game_id).get_structure()

    def create_or_get_game(self, sim_id, players, strategies, configuration):
        """Create or get game with configuration

        From a configuration, this will either get a game that matches or
        create a new one. The structure is set up to minimize time spent
        querying egtaonline, while still providing reuse between
        invocations."""
        assert players.keys() == strategies.keys(), \
            "players and strategies must have same keys"
        sim_info = self.get_simulator(sim_id).get_info()
        roles = sim_info['role_configuration']
        assert players.keys() <= roles.keys(), \
            "roles must exist in simulator"
        for role, strats in strategies.items():
            assert set(strats) <= set(roles[role]), \
                "role {} didn't contain all of {}".format(role, strats)
        conf = sim_info['configuration']
        conf.update(configuration)

        prefix = 'eo_{:d}__{}'.format(sim_id, '__'.join(
            '{}_{:d}_{}'.format(r, c, '_'.join(s for s in strategies[r]))
            for r, c in players.items()))
        size = sum(players.values())

        failed_ids = set()
        for game in self.get_games():
            valid = (game['name'].startswith(prefix)
                     and game['size'] == size
                     and game['simulator_instance_id'] not in failed_ids)
            if not valid:
                continue
            summ = game.get_summary()
            if dict(summ['configuration']) != conf:
                failed_ids.add(game['simulator_instance_id'])
                continue
            if all(set(strategies[rinfo['name']]) == set(rinfo['strategies'])
                   and players[rinfo['name']] == rinfo['count']
                   for rinfo in summ['roles']):
                return game

        suffix = ''.join(random.choice(string.ascii_lowercase)
                         for _ in range(12))
        name = prefix + '__' + suffix
        game = self.create_game(sim_id, name, size, conf)
        for role, count in players.items():
            game.add_role(role, count)
            for strategy in strategies[role]:
                game.add_strategy(role, strategy)
        return game

    def create_temp_game(self, sim_id, size, configuration={}):
        """Create a temporary game and return it

        A temporary game will destroy itself when it leaves context, and
        randomly generates its name.
        """
        name = 'temp_game_' + ''.join(
            random.choice(string.ascii_lowercase) for _ in range(12))
        return TempGame(self, **self.create_game(
            sim_id, name, size, configuration))

    def get_profile(self, id):
        """Get a profile from its id

        `id`s can be found with a scheduler's `get_requirements`, when adding a
        profile to a scheduler, or from a game with sufficient granularity."""
        return Profile(self, id=id)

    def get_simulations(self, page_start=1, asc=False, column=None):
        """Get information about current simulations

        Parameters
        ----------
        page_start : int, optional
            The page of results to start at beginning at 1. Traditionally there
            are 25 results per page, but this is defined by the server.
        asc : bool, optional
            If results should be sorted ascending. By default, they are
            descending, showing the most recent jobs or solders.
        column : str, optional
            The column to sort on
        `page_start` must be at least 1. `column` should be one of 'job',
        'folder', 'profile', 'simulator', or 'state'."""
        column = _sims_mapping.get(column, column)
        data = {
            'direction': 'ASC' if asc else 'DESC'
        }
        if column is not None:
            data['sort'] = column
        for page in itertools.count(page_start):  # pragma: no branch
            data['page'] = page
            resp = self._html_non_api_request('get', 'simulations', data=data)
            # FIXME I could make this more robust, by getting //thead/tr and
            # iterating through the links. If i parse out sort=* from the urls,
            # I'll get the order of the columns, this can be used to get the
            # order explicitely and detect errors when they miss align.
            rows = resp.xpath('//tbody/tr')
            if not rows:
                break  # Empty page implies we're done
            for row in rows:
                res = (_sims_parse(''.join(e.itertext()))  # pragma: no branch
                       for e in row.getchildren())
                yield dict(zip(_sims_mapping, res))

    def get_simulation(self, folder):
        """Get a simulation from its folder number"""
        resp = self._html_non_api_request(
            'get',
            'simulations/{folder}'.format(folder=folder))
        info = resp.xpath('//div[@class="show_for simulation"]/p')
        parsed = (''.join(e.itertext()).split(':', 1) for e in info)
        return {key.lower().replace(' ', '_'): _sims_parse(val.strip())
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
            'get', 'simulators/{sim:d}.json'.format(sim=self['id']))
        resp.raise_for_status()
        result = resp.json()
        result['url'] = '/'.join(('https:/', self._api.domain, 'simulators',
                                  str(result['id'])))
        return Simulator(self._api, result)

    def add_role(self, role):
        """Adds a role to the simulator"""
        resp = self._api._request(
            'post',
            'simulators/{sim:d}/add_role.json'.format(sim=self['id']),
            data={'role': role})
        resp.raise_for_status()

    def remove_role(self, role):
        """Removes a role from the simulator"""
        resp = self._api._request(
            'post',
            'simulators/{sim:d}/remove_role.json'.format(sim=self['id']),
            data={'role': role})
        resp.raise_for_status()

    def _add_strategy(self, role, strategy):
        """Like `add_strategy` but without the duplication check"""
        resp = self._api._request(
            'post',
            'simulators/{sim:d}/add_strategy.json'.format(sim=self['id']),
            data={'role': role, 'strategy': strategy})
        resp.raise_for_status()

    def add_strategy(self, role, strategy):
        """Adds a strategy to the simulator

        Note: This performs an extra check to prevent adding an existing
        strategy to the simulator."""
        # We call get_info to make sure we're up to date, but there are still
        # race condition issues with this.
        if strategy not in self.get_info()['role_configuration'][role]:
            self._add_strategy(role, strategy)

    def add_dict(self, role_strat_dict):
        """Adds all of the roles and strategies in a dictionary

        The dictionary should be of the form {role: [strategies]}."""
        # We call get_info again to make sure we're up to date. There are
        # obviously race condition issues with this.
        existing = self.get_info()['role_configuration']
        for role, strategies in role_strat_dict.items():
            existing_strats = set(existing.get(role, ()))
            self.add_role(role)
            for strategy in set(strategies).difference(existing_strats):
                self._add_strategy(role, strategy)

    def remove_strategy(self, role, strategy):
        """Removes a strategy from the simulator"""
        resp = self._api._request(
            'post',
            'simulators/{sim:d}/remove_strategy.json'.format(sim=self['id']),
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
            self['id'], name, active, process_memory, size,
            time_per_observation, observations_per_simulation, nodes,
            configuration)

    def create_game(self, name, size, configuration={}):
        """Creates a game and returns it

        See the method in `Api` for details."""
        return self._api.create_game(self['id'], name, size, configuration)

    def create_temp_game(self, size, configuration={}):
        """Creates a temporary game and returns it

        See the method in `Api` for details."""
        return self._api.create_temp_game(
            self['id'], size, configuration)


class Scheduler(_Base):
    """Get information and modify EGTA Online Scheduler"""

    def get_info(self):
        """Get a scheduler information"""
        resp = self._api._request(
            'get',
            'schedulers/{sched_id}.json'.format(sched_id=self['id']))
        resp.raise_for_status()
        return Scheduler(self._api, resp.json())

    def get_requirements(self):
        resp = self._api._request(
            'get',
            'schedulers/{sched_id}.json'.format(sched_id=self['id']),
            {'granularity': 'with_requirements'})
        resp.raise_for_status()
        result = resp.json()
        # The or is necessary since egta returns null instead of an empty list
        # when a scheduler has not requirements
        reqs = result.get('scheduling_requirements', None) or ()
        result['scheduling_requirements'] = [
            Profile(self._api, prof, id=prof.pop('profile_id'))
            for prof in reqs]
        result['url'] = 'https://{}/{}s/{:d}'.format(
            self._api.domain, inflection.underscore(result['type']),
            result['id'])
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
            'generic_schedulers/{sid:d}.json'.format(sid=self['id']),
            data={'scheduler': kwargs})
        resp.raise_for_status()

    def activate(self):
        self.update(active=True)

    def deactivate(self):
        self.update(active=False)

    def add_role(self, role, count):
        """Add a role with specific count to the scheduler"""
        resp = self._api._request(
            'post',
            'generic_schedulers/{sid:d}/add_role.json'.format(sid=self['id']),
            data={'role': role, 'count': count})
        resp.raise_for_status()

    def add_dict(self, role_counts):
        for role, count in role_counts.items():
            self.add_role(role, count)

    def remove_role(self, role):
        """Remove a role from the scheduler"""
        resp = self._api._request(
            'post',
            'generic_schedulers/{sid:d}/remove_role.json'.format(
                sid=self['id']),
            data={'role': role})
        resp.raise_for_status()

    def destroy_scheduler(self):
        """Delete a generic scheduler"""
        resp = self._api._request(
            'delete',
            'generic_schedulers/{sid:d}.json'.format(sid=self['id']))
        resp.raise_for_status()

    def add_profile(self, assignment, count):
        """Add a profile to the scheduler

        Parameters
        ----------
        assignment : str or list
            This must be an assignment string (e.g. "role: count strategy, ...;
            ...") or a symmetry group list (e.g. `[{"role": role, "strategy":
            strategy, "count": count}, ...]`).
        count : int
            The number of observations of that profile to schedule.

        Notes
        -----
        If the profile already exists, this won't change the requested count.
        """
        if not isinstance(assignment, str):
            assignment = symgrps_to_assignment(assignment)
        resp = self._api._request(
            'post',
            'generic_schedulers/{sid:d}/add_profile.json'.format(
                sid=self['id']),
            data={
                'assignment': assignment,
                'count': count
            })
        resp.raise_for_status()
        return Profile(self._api, resp.json(), assignment=assignment)

    def update_profile(self, profile, count):
        """Update the requested count of a profile object

        Parameters
        ----------
        profile : int or str or dict
            If profile is an int, it's treated as an id. If it's a string, it's
            treated as an assignment, if it's a dictionary and has at least one
            of id, assignment or symmetry_groups, it uses those fields
            appropriately, otherwise it's treated as a symmetry group. This
            will set the count of profile appropriately even if the profile is
            already in the scheduler, but it's not as efficient as removing and
            adding it yourself unless profile contains the appropriate id.
        count : int
            The new number of observations to require for this scheduler.
        """
        if isinstance(profile, int):
            profile_id = profile
            assignment = self._api.get_profile(
                profile).get_info()['assignment']

        elif isinstance(profile, str):
            assignment = profile
            profile_id = self.add_profile(assignment, 0)['id']

        elif any(k in profile for k
                 in ['id', 'assignment', 'symmetry_groups']):
            assignment = (profile.get('assignment', None) or
                          profile.get('symmetry_groups', None) or
                          self._api.get_profile(
                              profile['id']).get_info()['assignment'])
            profile_id = (profile.get('id', None) or
                          self.add_profile(assignment, 0)['id'])

        else:
            assignment = profile
            profile_id = self.add_profile(assignment, 0)['id']

        self.remove_profile(profile_id)
        return self.add_profile(assignment, count)

    def remove_profile(self, profile):
        """Removes a profile from a scheduler

        Parameters
        ----------
        profile : int or dict
            If profile is an int it's treated as the profile id, otherwise the
            'id' key is taken from the dictionary.
        """
        if not isinstance(profile, int):
            profile = profile['id']
        resp = self._api._request(
            'post',
            'generic_schedulers/{sid:d}/remove_profile.json'.format(
                sid=self['id']),
            data={'profile_id': profile})
        resp.raise_for_status()

    def remove_all_profiles(self):
        """Removes all profiles from a scheduler"""
        # We fetch scheduling requirements in case the data in self if out of
        # date.
        for profile in self.get_requirements()['scheduling_requirements']:
            self.remove_profile(profile)

    def create_game(self, name=None):
        """Creates a game with the same parameters of the scheduler

        If name is unspecified, it will copy the name from the scheduler. This
        will fail if there's already a game with that name."""
        if {'configuration', 'name', 'simulator_id', 'size'}.difference(self):
            return self.get_requirements().create_game(name)
        return self._api.create_game(
            self['simulator_id'], self['name'] if name is None else name,
            self['size'], dict(self['configuration']))

    def create_temp_game(self):
        """Creates a temporary game with the same parameters"""
        if {'configuration', 'simulator_id', 'size'}.difference(self):
            return self.get_requirements().create_temp_game()
        return self._api.create_temp_game(
            self['simulator_id'], self['size'],
            dict(self['configuration']))


class Profile(_Base):
    """Class for manipulating profiles"""

    def get_info(self, granularity='structure'):
        """Gets information about the profile

        Parameters
        ----------
        granularity : str, optional
            String representing the granularity of data to fetch. This is
            identical to game level granularity.  It can be one of 'structure',
            'summary', 'observations', 'full'.  See the corresponding
            get_`granularity` methods.
        """
        resp = self._api._request(
            'get',
            'profiles/{pid:d}.json'.format(pid=self['id']),
            {'granularity': granularity})
        resp.raise_for_status()
        return Profile(self._api, resp.json())

    def get_structure(self):
        """Get profile information but no payoff data"""
        return self.get_info('structure')

    def get_summary(self):
        """Return payoff data for each symmetry group"""
        return self.get_info('summary')

    def get_observations(self):
        """Return payoff data for each observation symmetry group"""
        return self.get_info('observations')

    def get_full_data(self):
        """Return payoff data for each player observation"""
        return self.get_info('full')


class Game(_Base):
    """Get information and manipulate EGTA Online Games"""

    def get_info(self, granularity='structure'):
        """Gets game information and data

        Parameters
        ----------
        granularity : str, optional
            Get data at one of the following granularities: structure, summary,
            observations, full. See the corresponding get_`granularity` methods
            for detailed descriptions of each granularity.
        """
        try:
            # This call breaks convention because the api is broken, so we use
            # a different api.
            result = self._api._json_non_api_request(
                'get',
                'games/{gid:d}.json'.format(gid=self['id']),
                data={'granularity': granularity})
            if granularity == 'structure':
                result = json.loads(result)
            else:
                result['profiles'] = [
                    Profile(self._api, p) for p
                    in result['profiles'] or ()]
            result['url'] = '/'.join(('https:/', self._api.domain, 'games',
                                      str(result['id'])))
            return Game(self._api, result)
        except requests.exceptions.HTTPError as ex:
            if not (str(ex).startswith('500 Server Error:') and
                    granularity in {'observations', 'full'}):
                raise ex
            result = self.get_summary()
            profs = []
            for prof in result['profiles']:
                gran = prof.get_info(granularity)
                gran.pop('simulator_instance_id')
                for obs in gran['observations']:
                    obs['extended_features'] = {}
                    obs['features'] = {}
                    if granularity == 'full':
                        for p in obs['players']:
                            p['e'] = {}
                            p['f'] = {}
                profs.append(gran)
            result['profiles'] = profs
            return result

    def get_structure(self):
        """Get game information without payoff data"""
        return self.get_info('structure')

    def get_summary(self):
        """Get payoff data for each profile by symmetry group"""
        return self.get_info('summary')

    def get_observations(self):
        """Get payoff data for each symmetry groups observation"""
        return self.get_info('observations')

    def get_full_data(self):
        """Get payoff data for each players observation"""
        return self.get_info('full')

    def add_role(self, role, count):
        """Adds a role to the game"""
        resp = self._api._request(
            'post',
            'games/{game:d}/add_role.json'.format(game=self['id']),
            data={'role': role, 'count': count})
        resp.raise_for_status()

    def remove_role(self, role):
        """Removes a role from the game"""
        resp = self._api._request(
            'post',
            'games/{game:d}/remove_role.json'.format(game=self['id']),
            data={'role': role})
        resp.raise_for_status()

    def add_strategy(self, role, strategy):
        """Adds a strategy to the game"""
        resp = self._api._request(
            'post',
            'games/{game:d}/add_strategy.json'.format(game=self['id']),
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
            'games/{game:d}/remove_strategy.json'.format(game=self['id']),
            data={'role': role, 'strategy': strategy})
        resp.raise_for_status()

    def remove_dict(self, role_strat_dict):
        """Removes all of the strategies in a dictionary

        The dictionary should be of the form {role: [strategies]}. Empty roles
        are not removed."""
        for role, strategies in role_strat_dict.items():
            for strategy in set(strategies):
                self.remove_strategy(role, strategy)

    def get_simulator(self):
        """Get the simulator for this game"""
        if 'simulator_fullname' not in self:
            return self.get_summary().get_simulator()
        name = self['simulator_fullname']
        return self._api.get_simulator(*name.split('-', 1))

    def destroy_game(self):
        """Delete a game"""
        resp = self._api._non_api_request(
            'post',
            'games/{game:d}'.format(game=self['id']),
            data={
                'auth_token': self._api._auth_token,  # Necessary
                '_method': 'delete',
            })
        resp.raise_for_status()


class TempGame(Game):
    """A game that destroys itself when leaving context"""

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.destroy_game()


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


_sims_mapping = collections.OrderedDict((
    ('state', 'state'),
    ('profile', 'profiles.assignment'),
    ('simulator', 'simulator_fullname'),
    ('folder', 'id'),
    ('job', 'job_id'),
))


def _sims_parse(res):
    """Converts N/A to `nan` and otherwise tries to parse integers"""
    try:
        return int(res)
    except ValueError:
        if res.lower() == 'n/a':
            return float('nan')  # pragma: no cover
        else:
            return res
