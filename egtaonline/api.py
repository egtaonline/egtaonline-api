"""Python package to handle python interface to egta online api"""
# pylint: disable=too-many-lines
import asyncio
import base64
import collections
import copy
import functools
import hashlib
import itertools
import json
import logging
from os import path

import inflection
import jsonschema
import requests
from lxml import etree


_AUTH_FILE = '.egta_auth_token'
_SEARCH_PATH = [_AUTH_FILE, path.expanduser(path.join('~', _AUTH_FILE))]

# TODO Add simulation object
# TODO Add a json schema for every object, and have every method validate
# FIXME Change asserts to ValueErrors


def _load_auth_token(auth_token):
    """Load an authorization token"""
    if auth_token is not None:  # pragma: no cover
        return auth_token
    for file_name in _SEARCH_PATH:  # pragma: no branch
        if path.isfile(file_name):
            with open(file_name) as fil:
                return fil.read().strip()
    return '<no auth_token supplied or found in any of: {}>'.format(  # pragma: no cover pylint: disable=line-too-long
        ', '.join(_SEARCH_PATH))


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

    def __init__(self, session, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sess = session
        assert 'id' in self


class _EgtaOnlineSession(object): # pylint: disable=too-many-instance-attributes
    """Object that holds the egta online session

    This object is private to hide private request methods."""
    def __init__( # pylint: disable=too-many-arguments
            self, auth_token, domain, retry_on, num_tries, retry_delay,
            retry_backoff, executor):
        self.domain = domain
        self.auth_token = _load_auth_token(auth_token)

        self._retry_on = frozenset(retry_on)
        self._num_tries = num_tries
        self._retry_delay = retry_delay
        self._retry_backoff = retry_backoff
        self._executor = executor
        self._loop = asyncio.get_event_loop()
        self._session = None

    async def aopen(self):
        """Open the requester"""
        assert self._session is None
        self._session = requests.Session()
        # This authenticates us for the duration of the session
        resp = self._session.get(
            'https://{domain}'.format(domain=self.domain),
            data={'auth_token': self.auth_token})
        resp.raise_for_status()
        assert '<a href="/users/sign_in">Sign in</a>' not in resp.text, \
            "Couldn't authenticate with auth_token: '{}'".format(
                self.auth_token)

    async def aclose(self):
        """Close the requester"""
        if self._session is not None:  # pragma: no branch
            self._session.close()
            self._session = None

    async def retry_request(self, verb, url, data):
        """Make a request, retying if it fails"""
        data = _encode_data(data)
        response = None
        timeout = self._retry_delay
        for _ in range(self._num_tries):
            logging.debug('%s request to %s with data %s', verb, url, data)
            try:
                response = await self._loop.run_in_executor(
                    self._executor, functools.partial(
                        self._session.request, verb, url, data=data))
                if response.status_code not in self._retry_on:
                    response.raise_for_status()
                    logging.debug('response "%s"', response.text)
                    return response
                logging.debug(
                    '%s request to %s with data %s failed with status'
                    '%d, retrying in %.0f seconds', verb, url, data,
                    response.status_code, timeout)  # pragma: no cover
            except ConnectionError as ex:  # pragma: no cover
                logging.debug(
                    '%s request to %s with data %s failed with '
                    'exception %s %s, retrying in %.0f seconds', verb,
                    url, data, ex.__class__.__name__, ex, timeout)
            logging.debug(  # pragma: no cover
                'sleeping %d due to connection error', timeout)
            await asyncio.sleep(timeout)  # pragma: no cover
            timeout *= self._retry_backoff  # pragma: no cover
        # TODO catch session level errors and reinitialize it
        raise ConnectionError()  # pragma: no cover

    async def request(self, verb, endpoint, data=None):
        """Convenience method for making requests"""
        url = 'https://{domain}/api/v3/{endpoint}'.format(
            domain=self.domain, endpoint=endpoint)
        return await self.retry_request(verb, url, data or {})

    async def json_validate_request(self, schema, verb, endpoint, data=None):
        """Convenience method for making validated json requests"""
        sleep = self._retry_delay
        exception = ValueError("shouldn't ever call this")
        for _ in range(self._num_tries):  # pragma: no branch
            resp = await self.request(verb, endpoint, data)
            try:
                jresp = resp.json()
                jsonschema.validate(jresp, schema)
                return jresp
            except (json.decoder.JSONDecodeError,
                    jsonschema.ValidationError) as ex:
                exception = ex
                logging.debug(
                    'sleeping %d due to invalid json', sleep)
                await asyncio.sleep(sleep)
                sleep *= self._retry_backoff
        raise exception

    async def non_api_request(self, verb, endpoint, data=None):
        """Make a standard request instead of hitting the api"""
        url = 'https://{domain}/{endpoint}'.format(
            domain=self.domain, endpoint=endpoint)
        return await self.retry_request(verb, url, data or {})

    async def json_non_api_request(
            self, schema, verb, endpoint, data=None):
        """non api request for json"""
        sleep = self._retry_delay
        exception = ValueError("shouldn't ever call this")
        for _ in range(self._num_tries):  # pragma: no branch
            resp = await self.non_api_request(verb, endpoint, data)
            try:
                jresp = resp.json()
                jsonschema.validate(jresp, schema)
                return jresp
            except (json.decoder.JSONDecodeError,
                    jsonschema.ValidationError) as ex:
                exception = ex
                logging.debug(
                    'sleeping %d due to invalid json', sleep)
                await asyncio.sleep(sleep)
                sleep *= self._retry_backoff
        raise exception

    async def html_non_api_request(self, verb, endpoint, data=None):
        """non api request for xml"""
        resp = await self.non_api_request(verb, endpoint, data)
        return etree.HTML(resp.text)

    # The following methods are used by several "objects" and so they are in
    # session object for easy access

    async def get_simulators(self):
        """Get a generator of all simulators"""
        resp = await self.request('get', 'simulators')
        return [_Simulator(self, s) for s in resp.json()['simulators']]

    async def get_simulator_fullname(self, fullname):
        """Get a simulator with its full name"""
        for sim in await self.get_simulators():
            if '{}-{}'.format(sim['name'], sim['version']) == fullname:
                return sim
        assert False, 'No simulator found for full name {}'.format(
            fullname)

    async def create_generic_scheduler( # pylint: disable=too-many-arguments
            self, sim_id, name, active, process_memory, size,
            time_per_observation, observations_per_simulation, nodes,
            configuration):
        """Creates a generic scheduler and returns it"""
        resp = await self.request(
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
                'configuration': configuration,
            }})
        return _Scheduler(self, resp.json())

    async def get_games(self):
        """Get a generator of all games"""
        resp = await self.request('get', 'games')
        return [_Game(self, g) for g in resp.json()['games']]

    async def get_game(self, game_id):
        """Get a game from an id"""
        return await _Game(self, id=game_id).get_structure()

    async def create_game(self, sim_id, name, size, configuration):
        """Creates a game and returns it"""
        resp = await self.html_non_api_request(
            'post',
            'games',
            data={
                'auth_token': self.auth_token,  # Necessary for some reason
                'game': {
                    'name': name,
                    'size': size,
                },
                'selector': {
                    'simulator_id': sim_id,
                    'configuration': configuration,
                },
            })
        game_id = int(resp.xpath('//div[starts-with(@id, "game_")]')[0]
                      .attrib['id'][5:])
        return await self.get_game(game_id)

    async def get_canon_game(
            self, sim_id, symgrps, configuration):
        """Get the canonicalized game"""
        digest = hashlib.sha512()
        digest.update(str(sim_id).encode('utf8'))
        for role, count, strats in sorted(symgrps):
            digest.update(b'\0\0')
            digest.update(role.encode('utf8'))
            digest.update(b'\0')
            digest.update(str(count).encode('utf8'))
            for strat in sorted(strats):
                digest.update(b'\0')
                digest.update(strat.encode('utf8'))
        digest.update(b'\0')
        for key, value in sorted(configuration.items()):
            digest.update(b'\0\0')
            digest.update(key.encode('utf8'))
            digest.update(b'\0')
            digest.update(str(value).encode('utf8'))
        name = base64.b64encode(digest.digest()).decode('utf8')
        size = sum(p for _, p, _ in symgrps)

        for game in await self.get_games():
            if game['name'] != name:
                continue
            assert game['size'] == size, \
                'A hash collision happened'
            return game

        game = await self.create_game(sim_id, name, size, configuration)
        await game.add_symgroups(symgrps)
        return game


class _EgtaOnlineApi(object):
    """Class that allows access to an Egta Online server

    This can be used as context manager to automatically close the active
    session."""
    def __init__( # pylint: disable=too-many-arguments
            self, auth_token=None, domain='egtaonline.eecs.umich.edu',
            retry_on=(504,), num_tries=20, retry_delay=20, retry_backoff=1.2,
            executor=None):
        self.domain = domain
        self._sess = _EgtaOnlineSession(
            auth_token, domain, retry_on, num_tries, retry_delay,
            retry_backoff, executor)

    async def aopen(self):
        """Open the api"""
        try:
            await self._sess.aopen()
        except Exception as ex:
            await self.aclose()
            raise ex

    async def aclose(self):
        """Close the api"""
        await self._sess.aclose()

    async def __aenter__(self):
        await self.aopen()
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def get_simulators(self):
        """Get a generator of all simulators"""
        return await self._sess.get_simulators()

    async def get_simulator(self, sim_id):
        """Get a simulator with an id"""
        return await _Simulator(self._sess, id=sim_id).get_info()

    async def get_simulator_fullname(self, fullname):
        """Get a simulator with its full name

        A full name is <name>-<version>."""
        return await self._sess.get_simulator_fullname(fullname)

    async def get_generic_schedulers(self):
        """Get a generator of all generic schedulers"""
        resp = await self._sess.request('get', 'generic_schedulers')
        return [_Scheduler(self._sess, s) for s in
                resp.json()['generic_schedulers']]

    async def get_scheduler(self, sched_id):
        """Get a scheduler with an id"""
        return await _Scheduler(self._sess, id=sched_id).get_info()

    async def get_scheduler_name(self, name):
        """Get a scheduler from its names"""
        for sched in await self.get_generic_schedulers():
            if sched['name'] == name:
                return sched
        assert False, 'No scheduler found for name {}'.format(
            name)

    async def create_generic_scheduler( # pylint: disable=too-many-arguments
            self, sim_id, name, active, process_memory, size,
            time_per_observation, observations_per_simulation, nodes=1,
            configuration=None):
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
            for this scheduler. This bypasses the simulator default to just set
            the configuration specified. To use the simulator default, you must
            first get the configuration from the simulator object."""
        return await self._sess.create_generic_scheduler(
            sim_id, name, active, process_memory, size, time_per_observation,
            observations_per_simulation, nodes, configuration or {})

    async def get_games(self):
        """Get a generator of all games"""
        return await self._sess.get_games()

    async def get_game(self, game_id):
        """Get a game from an id"""
        return await self._sess.get_game(game_id)

    async def get_game_name(self, name):
        """Get a game from its names"""
        for game in await self.get_games():
            if game['name'] == name:
                return game
        assert False, 'No game found for name {}'.format(name)

    async def create_game(self, sim_id, name, size, configuration=None):
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
            for this scheduler. This ignores simulator defaults, and will only
            set the configuration parameters specified. To include simulator
            defaults you must manually get the configuration parameters from
            the simulator."""
        return await self._sess.create_game(
            sim_id, name, size, configuration or {})

    async def get_canon_game(self, sim_id, symgrps, configuration=None):
        """Get the canonicalized game

        This is a default version of the game with symgrps and configuration.
        This way games can be reused without worrying about making sure they
        exist or creating duplicate games.

        Parameters
        ----------
        sim_id : int
            The id of the simulator to make the game for.
        symgrps : [(role, players, [strategy])]
            The symmetry groups for the game. The game is created or fetched
            with these in mind, and should not be modified afterwards.
        """
        return await self._sess.get_canon_game(
            sim_id, symgrps, configuration or {})

    async def get_profile(self, prof_id):
        """Get a profile from its id

        `id`s can be found with a scheduler's `get_requirements`, when adding a
        profile to a scheduler, or from a game with sufficient granularity."""
        return await _Profile(self._sess, id=prof_id).get_structure()

    def get_simulations(self, page_start=1, asc=False, column=None, search=''):
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
            The column to sort on `page_start` must be at least 1. `column`
            should be one of 'job', 'folder', 'profile', 'simulator', or
            'state'.
        search : string, optional
            A string to optionally filter results by. See the page on
            egtaonline for more information about what this can be. By default
            no filtering is done.
        """
        column = _SIMS_MAPPING.get(column, column)
        data = {
            'direction': 'ASC' if asc else 'DESC',
            'search': search,
        }
        if column is not None:
            data['sort'] = column
        return _SimulationIterator(self._sess, page_start, data)

    async def get_simulation(self, folder):
        """Get a simulation from its folder number"""
        resp = await self._sess.html_non_api_request(
            'get',
            'simulations/{folder}'.format(folder=folder))
        info = resp.xpath('//div[@class="show_for simulation"]/p')
        parsed = (''.join(e.itertext()).split(':', 1) for e in info)
        return {key.lower().replace(' ', '_'): _sims_parse(val.strip())
                for key, val in parsed}


class _SimulationIterator(object): # pylint: disable=too-few-public-methods
    """AsyncIterator for simulations"""
    def __init__(self, session, page_start, data):
        self._sess = session
        self._data = data
        self._page = itertools.count(page_start)
        self._rows = iter(())

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            row = next(self._rows)
        except StopIteration:
            self._data['page'] = next(self._page)
            resp = await self._sess.html_non_api_request(
                'get', 'simulations', data=self._data)
            self._rows = iter(resp.xpath('//tbody/tr'))
            try:
                row = next(self._rows)
            except StopIteration:
                raise StopAsyncIteration
        res = (_sims_parse(''.join(e.itertext()))  # pragma: no branch
               for e in row.getchildren())
        return dict(zip(_SIMS_MAPPING, res))


class _Simulator(_Base):
    """Get information about and modify EGTA Online Simulators"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self['url'] = '/'.join([
            'https:/', self._sess.domain, 'simulators', str(self['id'])])

    async def get_info(self):
        """Return information about this simulator

        If the id is unknown this will search all simulators for one with the
        same name and optionally version. If version is unspecified, but only
        one simulator with that name exists, this lookup should still succeed.
        This returns a new simulator object, but will update the id of the
        current simulator if it was undefined."""
        resp = await self._sess.request(
            'get', 'simulators/{sim:d}.json'.format(sim=self['id']))
        result = resp.json()
        return _Simulator(self._sess, result)

    async def add_role(self, role):
        """Adds a role to the simulator"""
        await self._sess.request(
            'post',
            'simulators/{sim:d}/add_role.json'.format(sim=self['id']),
            data={'role': role})

    async def remove_role(self, role):
        """Removes a role from the simulator"""
        await self._sess.request(
            'post',
            'simulators/{sim:d}/remove_role.json'.format(sim=self['id']),
            data={'role': role})

    async def _add_strategy(self, role, strategy):
        """Like `add_strategy` but without the duplication check"""
        await self._sess.request(
            'post',
            'simulators/{sim:d}/add_strategy.json'.format(sim=self['id']),
            data={'role': role, 'strategy': strategy})

    async def add_strategy(self, role, strategy):
        """Adds a strategy to the simulator

        Note: This performs an extra check to prevent adding an existing
        strategy to the simulator."""
        # We call get_info to make sure we're up to date, but there are still
        # race condition issues with this.
        sim_info = await self.get_info()
        if strategy not in sim_info['role_configuration'][role]:
            await self._add_strategy(role, strategy)

    async def add_strategies(self, role_strat_dict):
        """Adds all of the roles and strategies in a dictionary

        The dictionary should be of the form {role: [strategies]}."""
        # We call get_info again to make sure we're up to date. There are
        # obviously race condition issues with this.
        existing = (await self.get_info())['role_configuration']

        async def add_role(role, strats):
            """Asynchronous add role"""
            existing_strats = set(existing.get(role, ()))
            strats = set(strats).difference(existing_strats)
            await self.add_role(role)
            await asyncio.gather(*[
                self._add_strategy(role, strat) for strat in strats])

        await asyncio.gather(*[
            add_role(role, strats) for role, strats
            in role_strat_dict.items()])

    async def remove_strategy(self, role, strategy):
        """Removes a strategy from the simulator"""
        await self._sess.request(
            'post',
            'simulators/{sim:d}/remove_strategy.json'.format(sim=self['id']),
            data={'role': role, 'strategy': strategy})

    async def remove_strategies(self, role_strat_dict):
        """Removes all of the strategies in a dictionary

        The dictionary should be of the form {role: [strategies]}. Empty roles
        are not removed."""
        await asyncio.gather(*[
            self.remove_strategy(role, strat) for role, strat
            in itertools.chain.from_iterable(
                ((role, strat) for strat in set(strats))
                for role, strats in role_strat_dict.items())])

    async def create_generic_scheduler( # pylint: disable=too-many-arguments
            self, name, active, process_memory, size, time_per_observation,
            observations_per_simulation, nodes=1, configuration=None):
        """Creates a generic scheduler for this simulator and returns it"""
        return await self._sess.create_generic_scheduler(
            self['id'], name, active, process_memory, size,
            time_per_observation, observations_per_simulation, nodes,
            configuration or {})

    async def create_game(self, name, size, configuration=None):
        """Creates a game for this simulator and returns it"""
        return await self._sess.create_game(
            self['id'], name, size, configuration or {})

    async def get_canon_game(self, symgrps, configuration=None):
        """Get the canon game for this simulator"""
        return await self._sess.get_canon_game(
            self['id'], symgrps, configuration or {})


class _Scheduler(_Base):
    """Get information and modify EGTA Online Scheduler"""

    async def get_info(self):
        """Get a scheduler information"""
        resp = await self._sess.request(
            'get',
            'schedulers/{sched_id}.json'.format(sched_id=self['id']))
        return _Scheduler(self._sess, resp.json())

    async def get_requirements(self):
        """Get the schedulign requirements of a scheduler"""
        resp = await self._sess.request(
            'get',
            'schedulers/{sched_id}.json'.format(sched_id=self['id']),
            {'granularity': 'with_requirements'})
        result = resp.json()
        # The or is necessary since egta returns null instead of an empty list
        # when a scheduler has not requirements
        reqs = result.get('scheduling_requirements', None) or ()
        result['scheduling_requirements'] = [
            _Profile(self._sess, prof, id=prof.pop('profile_id'))
            for prof in reqs]
        result['url'] = 'https://{}/{}s/{:d}'.format(
            self._sess.domain, inflection.underscore(result['type']),
            result['id'])
        return _Scheduler(self._sess, result)

    async def update(self, **kwargs):
        """Update the parameters of a given scheduler

        kwargs are any of the mandatory arguments for create_generic_scheduler,
        except for configuration, that cannont be updated for whatever
        reason."""
        if 'active' in kwargs:
            kwargs['active'] = int(kwargs['active'])
        await self._sess.request(
            'put',
            'generic_schedulers/{sid:d}.json'.format(sid=self['id']),
            data={'scheduler': kwargs})

    async def activate(self):
        """Activate the scheduler"""
        await self.update(active=True)

    async def deactivate(self):
        """Deactivate the scheduler"""
        await self.update(active=False)

    async def add_role(self, role, count):
        """Add a role with specific count to the scheduler"""
        await self._sess.request(
            'post',
            'generic_schedulers/{sid:d}/add_role.json'.format(sid=self['id']),
            data={'role': role, 'count': count})

    async def add_roles(self, role_counts):
        """Add roles

        Parameters
        ----------
        role_counts : {role: count}
            A dictionary of the roles and counts.
        """
        await asyncio.gather(*[
            self.add_role(role, count) for role, count
            in role_counts.items()])

    async def remove_role(self, role):
        """Remove a role from the scheduler"""
        await self._sess.request(
            'post',
            'generic_schedulers/{sid:d}/remove_role.json'.format(
                sid=self['id']),
            data={'role': role})

    async def remove_roles(self, roles):
        """Remove roles

        Parameters
        ----------
        roles : [role]
            An iterable of the roles to remove.
        """
        await asyncio.gather(*[
            self.remove_role(role) for role in roles])

    async def destroy_scheduler(self):
        """Delete a generic scheduler"""
        await self._sess.request(
            'delete',
            'generic_schedulers/{sid:d}.json'.format(sid=self['id']))

    async def add_profile(self, assignment, count):
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
        resp = await self._sess.request(
            'post',
            'generic_schedulers/{sid:d}/add_profile.json'.format(
                sid=self['id']),
            data={
                'assignment': assignment,
                'count': count
            })
        return _Profile(self._sess, resp.json(), assignment=assignment)

    async def remove_profile(self, prof_id):
        """Removes a profile from a scheduler

        Parameters
        ----------
        prof_id : int
            The profile id to remove
        """
        await self._sess.request(
            'post',
            'generic_schedulers/{sid:d}/remove_profile.json'.format(
                sid=self['id']),
            data={'profile_id': prof_id})

    async def remove_all_profiles(self):
        """Removes all profiles from a scheduler"""
        # We fetch scheduling requirements in case the data in self if out of
        # date.
        reqs = await self.get_requirements()
        await asyncio.gather(*[
            self.remove_profile(prof['id']) for prof
            in reqs['scheduling_requirements']])

    async def create_game(self, name=None):
        """Creates a game with the same parameters of the scheduler

        If name is unspecified, it will copy the name from the scheduler. This
        will fail if there's already a game with that name."""
        if {'configuration', 'name', 'simulator_id', 'size'}.difference(self):
            reqs = await self.get_requirements()
            return await reqs.create_game(name)
        return await self._sess.create_game(
            self['simulator_id'], self['name'] if name is None else name,
            self['size'], dict(self['configuration']))


class _Profile(_Base):
    """Class for manipulating profiles"""

    async def _get_info(self, granularity, validate):
        """Gets information about the profile

        Parameters
        ----------
        granularity : str
            String representing the granularity of data to fetch. This is
            identical to game level granularity.  It can be one of
            'structure', 'summary', 'observations', 'full'.  See the
            corresponding get_`granularity` methods.
        validate : bool
            Whether to validate the returned json to make sure it's
            valid.
        """
        jresp = await self._sess.json_validate_request(
            _PROF_SCHEMATA[granularity] if validate else _NO_SCHEMA,
            'get',
            'profiles/{pid:d}.json'.format(pid=self['id']),
            {'granularity': granularity})
        return _Profile(self._sess, jresp)

    async def get_structure(self, validate=True):
        """Get profile information but no payoff data"""
        return await self._get_info('structure', validate)

    async def get_summary(self, validate=True):
        """Return payoff data for each symmetry group"""
        return await self._get_info('summary', validate)

    async def get_observations(self, validate=True):
        """Return payoff data for each observation symmetry group"""
        return await self._get_info('observations', validate)

    async def get_full_data(self, validate=True):
        """Return payoff data for each player observation"""
        return await self._get_info('full', validate)


class _Game(_Base):
    """Get information and manipulate EGTA Online Games"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self['url'] = '/'.join([
            'https:/', self._sess.domain, 'games', str(self['id'])])

    async def _get_info(self, granularity, validate):
        """Gets game information and data

        Parameters
        ----------
        granularity : str
            Get data at one of the following granularities: structure, summary,
            observations, full. See the corresponding get_`granularity` methods
            for detailed descriptions of each granularity.
        validate : bool
            Whether to cvalidate the returned json. Since we make a non-api
            request, the result is often not valid, so this is usually
            preferred despite the icnrease in time.
        """
        try:
            # This call breaks convention because the api is broken, so we use
            # a different api.
            result = await self._sess.json_non_api_request(
                _GAME_SCHEMATA[granularity] if validate else _NO_SCHEMA,
                'get',
                'games/{gid:d}.json'.format(gid=self['id']),
                data={'granularity': granularity})
            if granularity == 'structure':
                # TODO Is there a good way to validate this? Given how
                # small it is its unlikely to be wrong, but this is still
                # a missed edge case
                result = json.loads(result)
            else:
                result['profiles'] = [
                    _Profile(self._sess, p) for p
                    in result['profiles'] or ()]
            return _Game(self._sess, result)
        except requests.exceptions.HTTPError as ex:
            if not (str(ex).startswith('500 Server Error:') and
                    granularity in {'observations', 'full'}):
                raise ex
            result = await self.get_summary()
            if granularity == 'observations':
                profs = await asyncio.gather(*[
                    prof.get_observations(validate) for prof
                    in result['profiles']])
                for gran in profs:
                    gran.pop('simulator_instance_id')
                    for obs in gran['observations']:
                        obs['extended_features'] = {}
                        obs['features'] = {}
            else:
                profs = await asyncio.gather(*[
                    prof.get_full_data(validate) for prof
                    in result['profiles']])
                for gran in profs:
                    gran.pop('simulator_instance_id')
                    for obs in gran['observations']:
                        obs['extended_features'] = {}
                        obs['features'] = {}
                        for prf in obs['players']:
                            prf['e'] = {}
                            prf['f'] = {}
            result['profiles'] = profs
            return result

    async def get_structure(self, validate=True):
        """Get game information without payoff data"""
        return await self._get_info('structure', validate)

    async def get_summary(self, validate=True):
        """Get payoff data for each profile by symmetry group"""
        return await self._get_info('summary', validate)

    async def get_observations(self, validate=True):
        """Get payoff data for each symmetry groups observation"""
        return await self._get_info('observations', validate)

    async def get_full_data(self, validate=True):
        """Get payoff data for each players observation"""
        return await self._get_info('full', validate)

    async def add_role(self, role, count):
        """Adds a role to the game"""
        await self._sess.request(
            'post',
            'games/{game:d}/add_role.json'.format(game=self['id']),
            data={'role': role, 'count': count})

    async def add_roles(self, role_count_dict):
        """Add roles to the game

        Parameters
        ----------
        role_count_dict : {role: count}
            A dictionary of the counts for each role to add.
        """
        await asyncio.gather(*[
            self.add_role(role, count) for role, count
            in role_count_dict.items()])

    async def remove_role(self, role):
        """Removes a role from the game"""
        await self._sess.request(
            'post',
            'games/{game:d}/remove_role.json'.format(game=self['id']),
            data={'role': role})

    async def remove_roles(self, roles):
        """Remove roles from the game

        Parameters
        ----------
        roles : [role]
            An iterable of the roles to remove
        """
        await asyncio.gather(*[
            self.remove_role(role) for role in roles])

    async def add_strategy(self, role, strategy):
        """Adds a strategy to the game"""
        await self._sess.request(
            'post',
            'games/{game:d}/add_strategy.json'.format(game=self['id']),
            data={'role': role, 'strategy': strategy})

    async def add_strategies(self, role_strat_dict):
        """Attempts to add all of the strategies in a dictionary

        The dictionary should be of the form {role: [strategies]}."""
        await asyncio.gather(*[
            self.add_strategy(role, strat) for role, strat
            in itertools.chain.from_iterable(
                ((role, strat) for strat in set(strats))
                for role, strats in role_strat_dict.items())])

    async def remove_strategy(self, role, strategy):
        """Removes a strategy from the game"""
        await self._sess.request(
            'post',
            'games/{game:d}/remove_strategy.json'.format(game=self['id']),
            data={'role': role, 'strategy': strategy})

    async def remove_strategies(self, role_strat_dict):
        """Removes all of the strategies in a dictionary

        The dictionary should be of the form {role: [strategies]}. Empty roles
        are not removed."""
        await asyncio.gather(*[
            self.remove_strategy(role, strat) for role, strat
            in itertools.chain.from_iterable(
                ((role, strat) for strat in set(strats))
                for role, strats in role_strat_dict.items())])

    async def add_symgroup(self, role, count, strategies):
        """Add a symmetry group to the game

        Parameters
        ----------
        role : str
        count : int
        strategies : [str]
        """
        await self.add_role(role, count)
        await asyncio.gather(*[
            self.add_strategy(role, strat) for strat in strategies])

    async def add_symgroups(self, symgrps):
        """Add all symgrps to the game

        Parameters
        ----------
        symgrps : [(role, count, [strat])]
            The symgroups to add to the game.
        """
        await asyncio.gather(*[
            self.add_symgroup(role, count, strats) for role, count, strats
            in symgrps])

    async def destroy_game(self):
        """Delete a game"""
        await self._sess.non_api_request(
            'post',
            'games/{game:d}'.format(game=self['id']),
            data={
                'auth_token': self._sess.auth_token,  # Necessary
                '_method': 'delete',
            })

    async def create_generic_scheduler( # pylint: disable=too-many-arguments
            self, name, active, process_memory, time_per_observation,
            observations_per_simulation, nodes=1, configuration=None):
        """Create a generic scheduler with the configuration of the game"""
        if not {'simulator_fullname', 'roles'} <= self.keys():
            summ = await self.get_summary()
            return await summ.create_generic_scheduler(
                name, active, process_memory, time_per_observation,
                observations_per_simulation, nodes, configuration)
        size = sum(symgrp['count'] for symgrp in self['roles'])
        sim = await self._sess.get_simulator_fullname(
            self['simulator_fullname'])
        sched = await self._sess.create_generic_scheduler(
            sim['id'], name, active, process_memory, size,
            time_per_observation, observations_per_simulation, nodes,
            configuration or {})
        await sched.add_roles({
            symgrp['name']: symgrp['count'] for symgrp in self['roles']})
        return sched


def api( # pylint: disable=too-many-arguments
        auth_token=None, domain='egtaonline.eecs.umich.edu', retry_on=(504,),
        num_tries=20, retry_delay=20, retry_backoff=1.2, executor=None):
    """Create an api object"""
    return _EgtaOnlineApi(
        auth_token, domain, retry_on, num_tries, retry_delay, retry_backoff,
        executor)


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


_SIMS_MAPPING = collections.OrderedDict([
    ('state', 'state'),
    ('profile', 'profiles.assignment'),
    ('simulator', 'simulator_fullname'),
    ('folder', 'id'),
    ('job', 'job_id'),
])

# Schemata
_PROF_STRUCT_SCHEMA = {
    'type': 'object',
    'properties': {
        'assignment': {'type': 'string'},
        'created_at': {'type': 'string'},
        'id': {'type': 'integer'},
        'observations_count': {'type': 'integer'},
        'role_configuration': {'type': 'object'},
        'simulator_instance_id': {'type': 'integer'},
        'size': {'type': 'integer'},
        'updated_at': {'type': 'string'},
    },
    'required': ['assignment', 'created_at', 'id', 'observations_count',
                 'role_configuration', 'simulator_instance_id', 'size',
                 'updated_at'],
}
_GAME_STRUCT_SCHEMA = {
    'type': 'object',
    'properties': {
        'created_at': {'type': 'string'},
        'id': {'type': 'integer'},
        'name': {'type': 'string'},
        'simulator_instance_id': {'type': 'integer'},
        'size': {'type': 'integer'},
        'updated_at': {'type': 'string'},
    },
    'required': ['created_at', 'id', 'name', 'simulator_instance_id',
                 'size', 'updated_at'],
}
_PROF_SUMM_SCHEMA = {
    'type': 'object',
    'properties': {
        'id': {'type': 'integer'},
        'observations_count': {'type': 'integer'},
        'symmetry_groups': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer'},
                    'payoff': {'type': ['number', 'null']},
                    'payoff_sd': {'type': ['number', 'null']},
                    'role': {'type': 'string'},
                    'strategy': {'type': 'string'},
                },
                'required': ['id', 'payoff', 'payoff_sd', 'role',
                             'strategy'],
            },
        },
    },
    'required': ['id', 'observations_count', 'symmetry_groups'],
}
_GAME_SUMM_SCHEMA = {
    'type': 'object',
    'properties': {
        'id': {'type': 'integer'},
        'simulator_fullname': {'type': 'string'},
        'profiles': {'oneOf': [
            {'type': 'null'},
            {
                'type': 'array',
                'items': _PROF_SUMM_SCHEMA,
            },
        ]},
        'name': {'type': 'string'},
        'configuration': {
            'type': 'array',
            'items': {
                'type': 'array',
                'items': {'type': 'string'},
                'maxItems': 2,
                'minItems': 2,
            }
        },
        'roles': {'oneOf': [
            {'type': 'null'},
            {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'name': {'type': 'string'},
                        'count': {'type': 'integer'},
                        'strategies': {
                            'type': 'array',
                            'items': {'type': 'string'},
                        }
                    },
                    'required': ['count', 'name', 'strategies']
                },
            },
        ]},
    },
    'required': ['id', 'simulator_fullname', 'profiles', 'name',
                 'configuration', 'roles'],
}
_OBS_OBS_SCHEMA = {
    'type': 'object',
    'properties': {
        'extended_features': {'type': ['object', 'null']},
        'features': {'type': ['object', 'null']},
        'symmetry_groups': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer'},
                    'payoff': {'type': 'number'},
                    'payoff_sd': {'type': ['number', 'null']},
                },
                'required': ['id', 'payoff', 'payoff_sd'],
            },
        },
    },
    'required': ['extended_features', 'features', 'symmetry_groups'],
}
_PROF_OBS_SCHEMA = {
    'type': 'object',
    'properties': {
        'id': {'type': 'integer'},
        'observations': {
            'type': 'array',
            'items': _OBS_OBS_SCHEMA,
        },
        'symmetry_groups': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer'},
                    'count': {'type': 'integer'},
                    'role': {'type': 'string'},
                    'strategy': {'type': 'string'},
                },
                'required': ['id', 'count', 'role', 'strategy'],
            },
        },
    },
    'required': ['id', 'observations', 'symmetry_groups'],
}
_GAME_OBS_SCHEMA = copy.deepcopy(_GAME_SUMM_SCHEMA)
_GAME_OBS_SCHEMA['properties']['profiles']['oneOf'][1]['items'] = \
    _PROF_OBS_SCHEMA
_PROF_FULL_SCHEMA = copy.deepcopy(_PROF_OBS_SCHEMA)
_PROF_FULL_SCHEMA['properties']['observations']['items'] = {
    'type': 'object',
    'properties': {
        'extended_features': {'type': ['object', 'null']},
        'features': {'type': ['object', 'null']},
        'players': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'e': {'type': ['object', 'null']},
                    'f': {'type': ['object', 'null']},
                    'sid': {'type': 'integer'},
                    'p': {'type': 'number'},
                },
                'required': ['e', 'f', 'p', 'sid'],
            },
        },
    },
    'required': ['extended_features', 'features', 'players'],
}
_GAME_FULL_SCHEMA = copy.deepcopy(_GAME_SUMM_SCHEMA)
_GAME_FULL_SCHEMA['properties']['profiles']['oneOf'][1]['items'] = \
    _PROF_FULL_SCHEMA

# TODO These don't check for sim_instance_id
_PROF_SCHEMATA = {
    'structure': _PROF_STRUCT_SCHEMA,
    'summary': _PROF_SUMM_SCHEMA,
    'observations': _PROF_OBS_SCHEMA,
    'full': _PROF_FULL_SCHEMA,
}
_GAME_SCHEMATA = {
    'structure': {'type': 'string'},  # bug in the way structure is returned
    'summary': _GAME_SUMM_SCHEMA,
    'observations': _GAME_OBS_SCHEMA,
    'full': _GAME_FULL_SCHEMA,
}
_NO_SCHEMA = {'type': ['string', 'object']}


def _sims_parse(res):
    """Converts N/A to `nan` and otherwise tries to parse integers"""
    try:
        return int(res)
    except ValueError:
        if res.lower() == 'n/a': # pylint: disable=no-else-return
            return float('nan')  # pragma: no cover
        else:
            return res
