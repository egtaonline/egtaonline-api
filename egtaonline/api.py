"""Python package to handle python interface to egta online api"""
import async_generator
import asyncio
import base64
import collections
import copy
import functools
import hashlib
import inflection
import itertools
import json
import logging
import requests
from os import path


import jsonschema
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
                 retry_on=(504,), num_tries=20, retry_delay=20,
                 retry_backoff=1.2, executor=None):
        self.domain = domain
        self._auth_token = _load_auth_token(auth_token)
        self._retry_on = frozenset(retry_on)
        self._num_tries = num_tries
        self._retry_delay = retry_delay
        self._retry_backoff = retry_backoff
        self._executor = executor
        self._loop = asyncio.get_event_loop()

        self._session = None

    async def open(self):
        assert self._session is None
        try:
            self._session = requests.Session()
            # This authenticates us for the duration of the session
            resp = self._session.get(
                'https://{domain}'.format(domain=self.domain),
                data={'auth_token': self._auth_token})
            resp.raise_for_status()
            assert '<a href="/users/sign_in">Sign in</a>' not in resp.text, \
                "Couldn't authenticate with auth_token: '{}'".format(
                    self._auth_token)
        except Exception as ex:
            await self.close()
            raise ex

    async def close(self):
        if self._session is not None:  # pragma: no branch
            self._session.close()
            self._session = None

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def _retry_request(self, verb, url, data):
        data = _encode_data(data)
        response = None
        timeout = self._retry_delay
        for i in range(self._num_tries):
            _log.debug('%s request to %s with data %s', verb, url, data)
            try:
                response = await self._loop.run_in_executor(
                    self._executor, functools.partial(
                        self._session.request, verb, url, data=data))
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
            logging.warning(  # pragma: no cover
                'sleeping %d due to connection error', timeout)
            await asyncio.sleep(timeout)  # pragma: no cover
            timeout *= self._retry_backoff  # pragma: no cover
        # TODO catch session level errors and reinitialize it
        raise ConnectionError()  # pragma: no cover

    async def _request(self, verb, api, data={}):
        """Convenience method for making requests"""
        url = 'https://{domain}/api/v3/{endpoint}'.format(
            domain=self.domain, endpoint=api)
        return await self._retry_request(verb, url, data)

    async def _json_validate_request(self, schema, verb, api, data={}):
        """Convenience method for making validated json requests"""
        sleep = self._retry_delay
        exception = None
        for _ in range(self._num_tries):  # pragma: no branch
            resp = await self._request(verb, api, data)
            resp.raise_for_status()
            try:
                jresp = resp.json()
                jsonschema.validate(jresp, schema)
                return jresp
            except (json.decoder.JSONDecodeError,
                    jsonschema.ValidationError) as ex:
                exception = ex
                logging.warning(
                    'sleeping %d due to invalid json', sleep)
                await asyncio.sleep(sleep)
                sleep *= self._retry_backoff
        raise exception

    async def _non_api_request(self, verb, api, data={}):
        url = 'https://{domain}/{endpoint}'.format(
            domain=self.domain, endpoint=api)
        return await self._retry_request(verb, url, data)

    async def _json_non_api_request(
            self, schema, verb, api, data={}):
        """non api request for json"""
        sleep = self._retry_delay
        exception = None
        for _ in range(self._num_tries):  # pragma: no branch
            resp = await self._non_api_request(verb, api, data)
            resp.raise_for_status()
            try:
                jresp = resp.json()
                jsonschema.validate(jresp, schema)
                return jresp
            except (json.decoder.JSONDecodeError,
                    jsonschema.ValidationError) as ex:
                exception = ex
                logging.warning(
                    'sleeping %d due to invalid json', sleep)
                await asyncio.sleep(sleep)
                sleep *= self._retry_backoff
        raise exception

    async def _html_non_api_request(self, verb, api, data={}):
        """non api request for xml"""
        resp = await self._non_api_request(verb, api, data)
        resp.raise_for_status()
        return etree.HTML(resp.text)

    @async_generator.async_generator
    async def get_simulators(self):
        """Get a generator of all simulators"""
        resp = await self._request('get', 'simulators')
        resp.raise_for_status()
        for s in resp.json()['simulators']:
            await async_generator.yield_(Simulator(self, s))

    async def get_simulator(self, sim_id):
        """Get a simulator with an id"""
        return await Simulator(self, id=sim_id).get_info()

    async def get_simulator_fullname(self, fullname):
        """Get a simulator with its full name

        A full name is <name>-<version>."""
        sims = self.get_simulators()
        try:
            async for sim in sims:
                if '{}-{}'.format(sim['name'], sim['version']) == fullname:
                    return sim
            assert False, "No simulator found for full name {}".format(
                fullname)
        finally:
            await sims.aclose()

    @async_generator.async_generator
    async def get_generic_schedulers(self):
        """Get a generator of all generic schedulers"""
        resp = await self._request('get', 'generic_schedulers')
        resp.raise_for_status()
        for s in resp.json()['generic_schedulers']:
            await async_generator.yield_(Scheduler(self, s))

    async def get_scheduler(self, sid):
        """Get a scheduler with an id"""
        return await Scheduler(self, id=sid).get_info()

    async def get_scheduler_name(self, name):
        """Get a scheduler from its names"""
        scheds = self.get_generic_schedulers()
        try:
            async for sched in scheds:
                if sched['name'] == name:
                    return sched
            assert False, "No scheduler found for name {}".format(
                name)
        finally:
            await scheds.aclose()

    async def create_generic_scheduler(
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
        conf = (await self.get_simulator(sim_id))['configuration']
        conf.update(configuration)
        resp = await self._request(
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

    @async_generator.async_generator
    async def get_games(self):
        """Get a generator of all games"""
        resp = await self._request('get', 'games')
        resp.raise_for_status()
        for g in resp.json()['games']:
            await async_generator.yield_(Game(self, g))

    async def get_game(self, game_id):
        """Get a game from an id"""
        return await Game(self, id=game_id).get_structure()

    async def get_game_name(self, name):
        """Get a game from its names"""
        games = self.get_games()
        try:
            async for game in games:
                if game['name'] == name:
                    return game
            assert False, "No game found for name {}".format(name)
        finally:
            await games.aclose()

    async def create_game(self, sim_id, name, size, configuration={}):
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
        conf = (await self.get_simulator(sim_id))['configuration']
        conf.update(configuration)
        resp = await self._html_non_api_request(
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
        return await self.get_game(game_id)

    async def get_canon_game(
            self, sim_id, symgrps, configuration={}):
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
        sim_info = await self.get_simulator(sim_id)
        roles = sim_info['role_configuration']
        assert {r for r, _, _ in symgrps} <= roles.keys(), \
            "roles must exist in simulator"
        for role, _, strats in symgrps:
            assert role in roles, "role {} not in simulator".format(role)
            assert set(strats) <= set(roles[role]), \
                "not all strategies exist in simulator"
        conf = sim_info['configuration']
        conf.update(configuration)

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
        for key, value in sorted(conf.items()):
            digest.update(b'\0\0')
            digest.update(key.encode('utf8'))
            digest.update(b'\0')
            digest.update(str(value).encode('utf8'))
        name = base64.b64encode(digest.digest()).decode('utf8')
        size = sum(p for _, p, _ in symgrps)

        games = self.get_games()
        try:
            async for game in games:
                if game['name'] != name:
                    continue
                assert game['size'] == size, \
                    "A hash collision happened"
                return game
        finally:
            await games.aclose()

        game = await self.create_game(sim_id, name, size, conf)
        await game.add_symgroups(symgrps)
        return game

    async def get_profile(self, id):
        """Get a profile from its id

        `id`s can be found with a scheduler's `get_requirements`, when adding a
        profile to a scheduler, or from a game with sufficient granularity."""
        return await Profile(self, id=id).get_structure()

    @async_generator.async_generator
    async def get_simulations(self, page_start=1, asc=False, column=None):
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
            resp = await self._html_non_api_request(
                'get', 'simulations', data=data)
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
                await async_generator.yield_(dict(zip(_sims_mapping, res)))

    # TODO Add simulation search function

    async def get_simulation(self, folder):
        """Get a simulation from its folder number"""
        resp = await self._html_non_api_request(
            'get',
            'simulations/{folder}'.format(folder=folder))
        info = resp.xpath('//div[@class="show_for simulation"]/p')
        parsed = (''.join(e.itertext()).split(':', 1) for e in info)
        return {key.lower().replace(' ', '_'): _sims_parse(val.strip())
                for key, val in parsed}


class Simulator(_Base):
    """Get information about and modify EGTA Online Simulators"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self['url'] = '/'.join([
            'https:/', self._api.domain, 'simulators', str(self['id'])])

    async def get_info(self):
        """Return information about this simulator

        If the id is unknown this will search all simulators for one with the
        same name and optionally version. If version is unspecified, but only
        one simulator with that name exists, this lookup should still succeed.
        This returns a new simulator object, but will update the id of the
        current simulator if it was undefined."""
        resp = await self._api._request(
            'get', 'simulators/{sim:d}.json'.format(sim=self['id']))
        resp.raise_for_status()
        result = resp.json()
        return Simulator(self._api, result)

    async def add_role(self, role):
        """Adds a role to the simulator"""
        resp = await self._api._request(
            'post',
            'simulators/{sim:d}/add_role.json'.format(sim=self['id']),
            data={'role': role})
        resp.raise_for_status()

    async def remove_role(self, role):
        """Removes a role from the simulator"""
        resp = await self._api._request(
            'post',
            'simulators/{sim:d}/remove_role.json'.format(sim=self['id']),
            data={'role': role})
        resp.raise_for_status()

    async def _add_strategy(self, role, strategy):
        """Like `add_strategy` but without the duplication check"""
        resp = await self._api._request(
            'post',
            'simulators/{sim:d}/add_strategy.json'.format(sim=self['id']),
            data={'role': role, 'strategy': strategy})
        resp.raise_for_status()

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
        resp = await self._api._request(
            'post',
            'simulators/{sim:d}/remove_strategy.json'.format(sim=self['id']),
            data={'role': role, 'strategy': strategy})
        resp.raise_for_status()

    async def remove_strategies(self, role_strat_dict):
        """Removes all of the strategies in a dictionary

        The dictionary should be of the form {role: [strategies]}. Empty roles
        are not removed."""
        await asyncio.gather(*[
            self.remove_strategy(role, strat) for role, strat
            in itertools.chain.from_iterable(
                ((role, strat) for strat in set(strats))
                for role, strats in role_strat_dict.items())])

    async def create_generic_scheduler(
            self, name, active, process_memory, size, time_per_observation,
            observations_per_simulation, nodes=1, configuration={}):
        """Creates a generic scheduler for this simulator and returns it"""
        return await self._api.create_generic_scheduler(
            self['id'], name, active, process_memory, size,
            time_per_observation, observations_per_simulation, nodes,
            configuration)

    async def create_game(self, name, size, configuration={}):
        """Creates a game for this simulator and returns it"""
        return await self._api.create_game(
            self['id'], name, size, configuration)

    async def get_canon_game(self, symgrps, configuration={}):
        """Get the canon game for this simulator"""
        return await self._api.get_canon_game(
            self['id'], symgrps, configuration)


class Scheduler(_Base):
    """Get information and modify EGTA Online Scheduler"""

    async def get_info(self):
        """Get a scheduler information"""
        resp = await self._api._request(
            'get',
            'schedulers/{sched_id}.json'.format(sched_id=self['id']))
        resp.raise_for_status()
        return Scheduler(self._api, resp.json())

    async def get_requirements(self):
        resp = await self._api._request(
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

    async def update(self, **kwargs):
        """Update the parameters of a given scheduler

        kwargs are any of the mandatory arguments for create_generic_scheduler,
        except for configuration, that cannont be updated for whatever
        reason."""
        if 'active' in kwargs:
            kwargs['active'] = int(kwargs['active'])
        resp = await self._api._request(
            'put',
            'generic_schedulers/{sid:d}.json'.format(sid=self['id']),
            data={'scheduler': kwargs})
        resp.raise_for_status()

    async def activate(self):
        await self.update(active=True)

    async def deactivate(self):
        await self.update(active=False)

    async def add_role(self, role, count):
        """Add a role with specific count to the scheduler"""
        resp = await self._api._request(
            'post',
            'generic_schedulers/{sid:d}/add_role.json'.format(sid=self['id']),
            data={'role': role, 'count': count})
        resp.raise_for_status()

    async def add_roles(self, role_counts):
        await asyncio.gather(*[
            self.add_role(role, count) for role, count
            in role_counts.items()])

    async def remove_role(self, role):
        """Remove a role from the scheduler"""
        resp = await self._api._request(
            'post',
            'generic_schedulers/{sid:d}/remove_role.json'.format(
                sid=self['id']),
            data={'role': role})
        resp.raise_for_status()

    async def remove_roles(self, roles):
        await asyncio.gather(*[
            self.remove_role(role) for role in roles])

    async def destroy_scheduler(self):
        """Delete a generic scheduler"""
        resp = await self._api._request(
            'delete',
            'generic_schedulers/{sid:d}.json'.format(sid=self['id']))
        resp.raise_for_status()

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
        resp = await self._api._request(
            'post',
            'generic_schedulers/{sid:d}/add_profile.json'.format(
                sid=self['id']),
            data={
                'assignment': assignment,
                'count': count
            })
        resp.raise_for_status()
        return Profile(self._api, resp.json(), assignment=assignment)

    async def remove_profile(self, prof_id):
        """Removes a profile from a scheduler

        Parameters
        ----------
        prof_id : int
            The profile id to remove
        """
        resp = await self._api._request(
            'post',
            'generic_schedulers/{sid:d}/remove_profile.json'.format(
                sid=self['id']),
            data={'profile_id': prof_id})
        resp.raise_for_status()

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
        return await self._api.create_game(
            self['simulator_id'], self['name'] if name is None else name,
            self['size'], dict(self['configuration']))


class Profile(_Base):
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
        jresp = await self._api._json_validate_request(
            _prof_schemata[granularity] if validate else _no_schema,
            'get',
            'profiles/{pid:d}.json'.format(pid=self['id']),
            {'granularity': granularity})
        return Profile(self._api, jresp)

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


class Game(_Base):
    """Get information and manipulate EGTA Online Games"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self['url'] = '/'.join([
            'https:/', self._api.domain, 'games', str(self['id'])])

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
            result = await self._api._json_non_api_request(
                _game_schemata[granularity] if validate else _no_schema,
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
                    Profile(self._api, p) for p
                    in result['profiles'] or ()]
            return Game(self._api, result)
        except requests.exceptions.HTTPError as ex:
            if not (str(ex).startswith('500 Server Error:') and
                    granularity in {'observations', 'full'}):
                raise ex
            result = await self.get_summary()
            profs = await asyncio.gather(*[
                prof._get_info(granularity, validate) for prof
                in result['profiles']])
            for gran in profs:
                gran.pop('simulator_instance_id')
                for obs in gran['observations']:
                    obs['extended_features'] = {}
                    obs['features'] = {}
                    if granularity == 'full':
                        for p in obs['players']:
                            p['e'] = {}
                            p['f'] = {}
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
        resp = await self._api._request(
            'post',
            'games/{game:d}/add_role.json'.format(game=self['id']),
            data={'role': role, 'count': count})
        resp.raise_for_status()

    async def add_roles(self, role_count_dict):
        await asyncio.gather(*[
            self.add_role(role, count) for role, count
            in role_count_dict.items()])

    async def remove_role(self, role):
        """Removes a role from the game"""
        resp = await self._api._request(
            'post',
            'games/{game:d}/remove_role.json'.format(game=self['id']),
            data={'role': role})
        resp.raise_for_status()

    async def remove_roles(self, roles):
        await asyncio.gather(*[
            self.remove_role(role) for role in roles])

    async def add_strategy(self, role, strategy):
        """Adds a strategy to the game"""
        resp = await self._api._request(
            'post',
            'games/{game:d}/add_strategy.json'.format(game=self['id']),
            data={'role': role, 'strategy': strategy})
        resp.raise_for_status()

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
        resp = await self._api._request(
            'post',
            'games/{game:d}/remove_strategy.json'.format(game=self['id']),
            data={'role': role, 'strategy': strategy})
        resp.raise_for_status()

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
        resp = await self._api._non_api_request(
            'post',
            'games/{game:d}'.format(game=self['id']),
            data={
                'auth_token': self._api._auth_token,  # Necessary
                '_method': 'delete',
            })
        resp.raise_for_status()

    async def create_generic_scheduler(
            self, name, active, process_memory, time_per_observation,
            observations_per_simulation, nodes=1, configuration={}):
        if not {'simulator_fullname', 'roles'} <= self.keys():
            summ = await self.get_summary()
            return await summ.create_generic_scheduler(
                name, active, process_memory, time_per_observation,
                observations_per_simulation, nodes, configuration)
        size = sum(symgrp['count'] for symgrp in self['roles'])
        sim = await self._api.get_simulator_fullname(
            self['simulator_fullname'])
        sched = await self._api.create_generic_scheduler(
            sim['id'], name, active, process_memory, size,
            time_per_observation, observations_per_simulation, nodes,
            configuration)
        await sched.add_roles({
            symgrp['name']: symgrp['count'] for symgrp in self['roles']})
        return sched


def api(*args, **kwargs):
    return EgtaOnlineApi(*args, **kwargs)


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

# Schemata
_prof_struct_schema = {
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
_game_struct_schema = {
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
_prof_summ_schema = {
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
_game_summ_schema = {
    'type': 'object',
    'properties': {
        'id': {'type': 'integer'},
        'simulator_fullname': {'type': 'string'},
        'profiles': {'oneOf': [
            {'type': 'null'},
            {
                'type': 'array',
                'items': _prof_summ_schema,
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
_obs_obs_schema = {
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
_prof_obs_schema = {
    'type': 'object',
    'properties': {
        'id': {'type': 'integer'},
        'observations': {
            'type': 'array',
            'items': _obs_obs_schema,
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
_game_obs_schema = copy.deepcopy(_game_summ_schema)
_game_obs_schema['properties']['profiles']['oneOf'][1]['items'] = \
    _prof_obs_schema
_prof_full_schema = copy.deepcopy(_prof_obs_schema)
_prof_full_schema['properties']['observations']['items'] = {
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
_game_full_schema = copy.deepcopy(_game_summ_schema)
_game_full_schema['properties']['profiles']['oneOf'][1]['items'] = \
    _prof_full_schema

# TODO These don't check for sim_instance_id
_prof_schemata = {
    'structure': _prof_struct_schema,
    'summary': _prof_summ_schema,
    'observations': _prof_obs_schema,
    'full': _prof_full_schema,
}
_game_schemata = {
    'structure': {'type': 'string'},  # bug in the way structure is returned
    'summary': _game_summ_schema,
    'observations': _game_obs_schema,
    'full': _game_full_schema,
}
_no_schema = {'type': ['string', 'object']}


def _sims_parse(res):
    """Converts N/A to `nan` and otherwise tries to parse integers"""
    try:
        return int(res)
    except ValueError:
        if res.lower() == 'n/a':
            return float('nan')  # pragma: no cover
        else:
            return res
