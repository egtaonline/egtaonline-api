import argparse
import json
import re
import requests
import sys
import tabulate
import textwrap
from datetime import datetime
from os import path

from egtaonline import api


_PARSER = argparse.ArgumentParser(
    prog='eo', description="""Command line access to egta online apis. This
    works well in concert with `jq`.""")
_PARSER.add_argument(
    '--verbose', '-v', action='count', default=0, help="""Sets the verbosity of
    commands. Output is send to standard error.""")

_PARSER_AUTH = _PARSER.add_mutually_exclusive_group()
_PARSER_AUTH.add_argument(
    '--auth-string', '-a', metavar='<auth-string>', help="""The string
    authorization token to connect to egta online.""")
_PARSER_AUTH.add_argument(
    '--auth-file', '-f', metavar='<auth-file>', help="""Filename that just
    contains the string of the auth token.""")

_SUBPARSERS = _PARSER.add_subparsers(title='Subcommands', dest='command')
_SUBPARSERS.required = True

_PARSER_SIM = _SUBPARSERS.add_parser(
    'sim', help="""Get information or modify a simulator.""",
    description="""Operate on EGTA simulators""")
_PARSER_SIM.add_argument(
    'sim_id', metavar='sim-id', nargs='?', help="""The identifier of the
    simulation to get information from.  By default this should be the
    simulator id as a number. If unspecified, all simulators are returned.""")
_PARSER_SIM.add_argument(
    '--version', '-n', nargs='?', metavar='version-name',
    default=argparse.SUPPRESS, const=None, help="""If this is specified then
the `sim-id` is treated as the name of the simulator and an optionally supplied
argument is treated as the version.  If no version is specified then this will
    try to find a single simulator with the name. An error is thrown if there
    are 0 or 2 or more simulators with the same name.""")
_PARSER_SIM.add_argument(
    '--json', '-j', metavar='json-file', nargs='?',
    type=argparse.FileType('r'), default=argparse.SUPPRESS, const=sys.stdin,
    help="""Modify simulator using the specified json file. By default this
    will add all of the roles and strategies in the json file. If `delete` is
    specified, this will remove only the strategies specified in the file. `-`
    can be used to read from stdin. (default: stdin)""")
_PARSER_SIM.add_argument(
    '--role', '-r', metavar='<role>', help="""Modify `role` of the simulator.
    By default this will add `role` to the simulator. If `delete` is specified
    this will remove `role` instead. If `strategy` is specified see
    strategy.""")
_PARSER_SIM.add_argument(
    '--strategy', '-s', metavar='<strategy>', help="""Modify `strategy` of the
    simulator. This requires that `role` is also specified. By default this
    adds `strategy` to `role`. If `delete` is specified, then this removes the
    strategy instead.""")
_PARSER_SIM.add_argument(
    '--delete', '-d', action='store_true', help="""Triggers removal of roles or
    strategies instead of addition""")
_PARSER_SIM.add_argument(
    '--zip', '-z', action='store_true', help="""Download simulator zip file to
    stdout.""")

_PARSER_GAME = _SUBPARSERS.add_parser(
    'game', help="""Get information and data or create, destroy, or modify a
    game.""", description="""Operate on EGTA Online games.""")
_PARSER_GAME.add_argument(
    'game_id', nargs='?', metavar='game-id', help="""The identifier of the game
    to get data from. By default this should be the game id number. If
    unspecified this will return a list of all of the games.""")
_PARSER_GAME.add_argument(
    '--name', '-n', action='store_true', help="""If specified then get the game
    via its string name not its id number. This is much slower than accessing
    via id number.""")
_PARSER_GAME.add_argument(
    '--json', '-j', metavar='json-file', nargs='?',
    type=argparse.FileType('r'), default=argparse.SUPPRESS, const=sys.stdin,
    help="""Modify game using the specified json file.  By default this will
    add all of the roles and strategies in the json file. If `delete` is
    specified, this will remove only the strategies specified in the file.
    (default: stdin)""")
_PARSER_GAME.add_argument(
    '--role', '-r', metavar='<role>', help="""Modify `role` of the game. By
    default this will add `role` to the game. If `delete` is specified this
    will remove `role` instead. If `strategy` is specified see strategy.""")
_PARSER_GAME.add_argument(
    '--count', '-c', metavar='<count>', help="""If adding a role, the count of
    the role must be specified.""")
_PARSER_GAME.add_argument(
    '--strategy', '-s', metavar='<strategy>', help="""Modify `strategy` of the
    game. This requires that `role` is also specified. By default this adds
    `strategy` to `role`. If `delete` is specified, then this removes the
    strategy instead.""")
_PARSER_GAME.add_argument(
    '--delete', '-d', action='store_true', help="""Triggers removal of roles or
    strategies instead of addition""")
_PARSER_GRAN = (_PARSER_GAME.add_argument_group('data granularity')
                .add_mutually_exclusive_group())
_PARSER_GRAN.add_argument(
    '--structure', action='store_true', help="""Return game information but no
    profile information.  (default)""")
_PARSER_GRAN.add_argument(
    '--summary', action='store_true', help="""Return profiles with aggregated
    payoffs.""")
_PARSER_GRAN.add_argument(
    '--observations', action='store_true', help="""Return profiles with
    observation data.""")
_PARSER_GRAN.add_argument(
    '--full', action='store_true', help="""Return all collected payoff
    data.""")

_PARSER_SCHED = _SUBPARSERS.add_parser(
    'sched', help="""Get information or create, destroy, or modify a
    scheduler.""", description="""Operate on EGTA Online schedulers.""")
_PARSER_SCHED.add_argument(
    'sched_id', nargs='?', metavar='sched-id', help="""The identifier of the
    scheduler to get data from. By default this should be the scheduler id
    number. If unspecified this will return a list of all of the generic
    schedulers.""")
_PARSER_SCHED.add_argument(
    '--name', '-n', action='store_true', help="""If specified then get the
    scheduler via its string name not its id number. This is much slower than
    accessing via id number, and only works for generic schedulers.""")
_PARSER_SCHED.add_argument(
    '--days-old', metavar='<days>', type=int, default=7, help="""The number of
    days stagnant (unupdated) a scheduler has to be to consider it ready for
    removal. (default: %(default)d)""")
_PARSER_ACT = (_PARSER_SCHED.add_argument_group('scheduler action')
               .add_mutually_exclusive_group())
_PARSER_ACT.add_argument(
    '--requirements', '-r', action='store_true', help="""Get scheuler
    requirements instead of just information.""")
_PARSER_ACT.add_argument(
    '--deactivate', action='store_true', help="""Deactivate the specified
    scheduler.""")
_PARSER_ACT.add_argument(
    '--delete', '-d', action='count', help="""If specified with a scheduler,
delete it. If no scheduler is specified then filter by inactive or complete
generic schedulers that match quiesce generated names and haven't been updated
in a week and delete them.  If specified multiple times, then remove all
    without prompt.""")

_PARSER_SIMS = _SUBPARSERS.add_parser(
    'sims', help="""Get information about pending, scheduled, queued, complete,
    or failed simulations.""", description="""Get information about EGTA Online
simulations. These are the actual scheduled simulations instead of the
simulators that generate them.  If no folder is specified, each stream comes
    out on a different line, and can be easily filtered with `head` and
    `jq`.""")
_PARSER_SIMS.add_argument(
    'folder', metavar='folder-id', nargs='?', help="""The identifier of the
simulation to get data from, normally referred to as the folder number. If
unspecified this will return a stream of json scheduler information that can be
streamed through jq to get necessary information. If streamed, each result is
    on a new line.""")
_PARSER_SIMS.add_argument(
    '--page', '-p', metavar='<start-page>', default=1, type=int, help="""The
    page to start scanning at.  (default: %(default)d)""")
_PARSER_SIMS.add_argument(
    '--ascending', '-a', action='store_true', help="""Return results in
    ascending order instead of descending.""")
_PARSER_SIMS.add_argument(
    '--sort-column', '-s', choices=('job', 'folder', 'profile', 'state'),
    default='job', help="""Column to order results by.  (default:
    %(default)s)""")


def main():
    args = _PARSER.parse_args()
    if args.auth_string is None and args.auth_file is not None:
        with open(args.auth_file) as auth_file:
            args.auth_string = auth_file.read().strip()

    with api.EgtaOnlineApi(args.auth_string, logLevel=args.verbose) as eo:

        if args.command == 'sim':
            if args.sim_id is None:  # Get all simulators
                json.dump(list(eo.get_simulators()), sys.stdout)

            else:  # Operate on a single simulator
                # Get simulator
                if not hasattr(args, 'version'):
                    sim = eo.get_simulator(int(args.sim_id))
                else:
                    sim = eo.get_simulator(args.sim_id, args.version)

                # Operate
                if args.zip:
                    info = sim.get_info()
                    url = 'https://' + eo.domain + info['source']['url']
                    resp = requests.get(url)
                    resp.raise_for_status()
                    sys.stdout.buffer.write(resp.content)

                elif hasattr(args, 'json'):  # Load from json
                    role_strat = json.load(args.json)
                    if args.delete:
                        sim.remove_dict(role_strat)
                    else:
                        sim.add_dict(role_strat)
                elif args.role is not None:  # Operate on single role or strat
                    if args.strategy is not None:  # Operate on strategy
                        if args.delete:
                            sim.remove_strategy(args.role, args.strategy)
                        else:
                            sim.add_strategy(args.role, args.strategy)
                    else:  # Operate on role
                        if args.delete:
                            sim.remove_role(args.role)
                        else:
                            sim.add_role(args.role)
                else:  # Return information instead
                    json.dump(sim.get_info(), sys.stdout)

        elif args.command == 'game':
            if args.game_id is None:  # Get all games
                json.dump(list(eo.get_games()), sys.stdout)

            else:  # Operate on specific game
                # Get game
                if args.name:
                    game = eo.get_game(args.game_id)
                else:
                    game = eo.get_game(int(args.game_id))

                # Operate
                if hasattr(args, 'json'):  # Load from json
                    role_strat = json.load(args.json)
                    if args.delete:
                        game.remove_dict(role_strat)
                    else:
                        game.add_dict(role_strat)

                elif args.role is not None:  # Operate on single role or strat
                    if args.strategy is not None:  # Operate on strategy
                        if args.delete:
                            game.remove_strategy(args.role, args.strategy)
                        else:
                            game.add_strategy(args.role, args.strategy)
                    else:  # Operate on role
                        if args.delete:
                            game.remove_role(args.role)
                        elif args.count:
                            game.add_role(args.role, args.count)
                        else:
                            raise ValueError(
                                'If adding a role, count must be specified')

                else:  # Return information instead
                    if args.summary:
                        dump = game.get_summary()
                    elif args.observations:
                        dump = game.get_observations()
                    elif args.full:
                        dump = game.get_full_data()
                    else:
                        dump = game.get_structure()
                    json.dump(dump, sys.stdout)

        elif args.command == 'sched':
            if args.sched_id is None:  # Get all schedulers
                scheds = eo.get_generic_schedulers()
                if args.delete:
                    reg = re.compile(
                        r'.*_generic_quiesce(_[a-zA-Z]+_\d+)*_[a-zA-Z0-9]{6}')
                    now = datetime.utcnow()
                    fscheds = (s for s in scheds
                               if reg.fullmatch(s.name)
                               and args.days_old <= (now - datetime.strptime(
                                   s.updated_at, '%Y-%m-%dT%H:%M:%S.%fZ')).days
                               and (
                                   not s.active or
                                   all(p['current_count'] >= p['requirement']
                                       for p in s.get_requirements()
                                       .scheduling_requirements)))

                    if args.delete > 1:
                        sys.stdout.write('Gathering scheduler information...')
                        fscheds = list(fscheds)
                        if fscheds:
                            sys.stdout.write('\n')
                            sys.stdout.write(textwrap.fill(' '.join(
                                '''Delete all {:d} quiesce generic schedulers
                                that haven\'t been updated in at least {} days
                                and are either inactive or done scheduling
                                [N/y]:'''.format(len(fscheds), args.days_old)
                                .split())))
                            inp = input(' ').lower()
                            if 'y' == inp:
                                for sched in fscheds:
                                    sched.delete_scheduler()
                        else:
                            sys.stdout.write(
                                ' no schedulers meet criteria.\n')
                    else:
                        for sched in fscheds:
                            print(tabulate.tabulate(sched.items()))
                            inp = input('Delete Scheduler [N/y]: ').lower()
                            if 'y' == inp:
                                sched.delete_scheduler()

                else:
                    json.dump(list(scheds), sys.stdout)

            else:  # Get a single scheduler
                # Get scheduler
                if args.name:
                    sched = eo.get_scheduler(args.sched_id)
                else:
                    sched = eo.get_scheduler(int(args.sched_id))

                # Resolve
                if args.deactivate:
                    sched.deactivate()
                elif args.delete:
                    sched.delete_scheduler()
                elif args.requirements:
                    json.dump(sched.get_requirements(), sys.stdout)
                else:
                    json.dump(sched.get_info(), sys.stdout)

        elif args.command == 'sims':
            if args.folder is not None:  # Get info on one simulation
                sim = eo.get_simulation(args.folder)
                json.dump(sim, sys.stdout)

            else:  # Stream simulations
                sims = eo.get_simulations(
                    page_start=args.page, asc=args.ascending,
                    column=args.sort_column)
                try:
                    for sim in sims:
                        json.dump(sim, sys.stdout)
                        sys.stdout.write('\n')
                except (BrokenPipeError, KeyboardInterrupt):
                    pass  # Don't care if stream breaks or is killed

        else:
            raise ValueError('Invalid option "{0}" specified'.format(
                args.command))
