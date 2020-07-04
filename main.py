import asyncio
import concurrent.futures
import itertools
import re
from datetime import datetime, timedelta
from functools import partial
from json import dumps as json_dump
from time import time as unix_time
from contextlib import contextmanager

import aiohttp
import dateparser
import pytz

from config import Config
from consts import *
from models import Reminder, Todo, Timer, Message, Channel, Event, CommandAlias, Session
from passers import *
from time_extractor import TimeExtractor, InvalidTime

THEME_COLOR = 0x8fb677


class BotClient(discord.AutoShardedClient):
    def __init__(self, *args, **kwargs):
        self.start_time: float = unix_time()

        self.commands: typing.Dict[str, Command] = {

            'ping': Command('ping', self.time_stats),

            'help': Command('help', self.help, blacklists=False),
            'info': Command('info', self.info),
            'donate': Command('donate', self.donate),

            'timezone': Command('timezone', self.set_timezone),
            'lang': Command('lang', self.set_language),
            'clock': Command('clock', self.clock),

            'todo': Command('todo', self.todo),
            'todos': Command('todos', self.todos, False, PermissionLevels.MANAGED),

            'natural': Command('natural', self.natural, True, PermissionLevels.MANAGED),
            'n': Command('natural', self.natural, True, PermissionLevels.MANAGED),
            'remind': Command('remind', self.remind_cmd, True, PermissionLevels.MANAGED),
            'r': Command('remind', self.remind_cmd, True, PermissionLevels.MANAGED),
            'interval': Command('interval', self.interval_cmd, True, PermissionLevels.MANAGED),
            # TODO: remodel timer table with FKs for guild table
            'timer': Command('timer', self.timer, False, PermissionLevels.MANAGED),
            'del': Command('del', self.delete, True, PermissionLevels.MANAGED),
            # TODO: allow looking at reminder attributes in full by name
            'look': Command('look', self.look, True, PermissionLevels.MANAGED),

            'alias': Command('alias', self.create_alias, False, PermissionLevels.MANAGED),
            'a': Command('alias', self.create_alias, False, PermissionLevels.MANAGED),

            'prefix': Command('prefix', self.change_prefix, False, PermissionLevels.RESTRICTED),

            'blacklist': Command('blacklist', self.blacklist, False, PermissionLevels.RESTRICTED, blacklists=False),
            'restrict': Command('restrict', self.restrict, False, PermissionLevels.RESTRICTED),

            'offset': Command('offset', self.offset_reminders, True, PermissionLevels.RESTRICTED),
            'nudge': Command('nudge', self.nudge_channel, True, PermissionLevels.RESTRICTED),
            'pause': Command('pause', self.pause_channel, False, PermissionLevels.RESTRICTED),
        }

        self.match_string = None

        self.command_names = set(self.commands.keys())
        self.joined_names = '|'.join(self.command_names)

        # used in restrict command for filtration
        self.max_command_length = max(len(x) for x in self.command_names)

        self.config: Config = Config(filename='config.ini')

        self.executor: concurrent.futures.ThreadPoolExecutor = concurrent.futures.ThreadPoolExecutor()
        self.c_session: typing.Optional[aiohttp.ClientSession] = None

        self.session: sqlalchemy.orm.session.Session = None

        super(BotClient, self).__init__(*args, **kwargs)


    @contextmanager
    def get_session(self):
        session_already_exists = self.session is not None
        if not session_already_exists:
            self.session = Session()
        try:
            yield self.session
        except:
            self.session.rollback()
            raise
        finally:
            if not session_already_exists:
                Session.remove()
                self.session = None


    async def do_blocking(self, method):
        # perform a long running process within a threadpool
        a, _ = await asyncio.wait([self.loop.run_in_executor(self.executor, method)])
        return [x.result() for x in a][0]

    async def find_and_create_member(self, member_id: int, context_guild: typing.Optional[discord.Guild]) \
            -> typing.Optional[User]:
        u: User = self.session.query(User).filter(User.user == member_id).first()

        if u is None and context_guild is not None:
            m = context_guild.get_member(member_id) or self.get_user(member_id)

            if m is not None:
                c = Channel(channel=(await m.create_dm()).id)
                self.session.add(c)
                self.session.flush()

                u = User(user=m.id, name='{}'.format(m), dm_channel=c.id)

                self.session.add(u)
                self.session.commit()

        return u

    async def is_patron(self, member_id) -> bool:
        if self.config.patreon_enabled:

            url = 'https://discordapp.com/api/v6/guilds/{}/members/{}'.format(self.config.patreon_server, member_id)

            head = {
                'authorization': 'Bot {}'.format(self.config.token),
                'content-type': 'application/json'
            }

            async with self.c_session.get(url, headers=head) as resp:

                if resp.status == 200:
                    member = await resp.json()
                    roles = [int(x) for x in member['roles']]

                else:
                    return False

            return self.config.patreon_role in roles

        else:
            return True

    @staticmethod
    async def welcome(guild, *_):

        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages and not channel.is_nsfw():
                await channel.send('Thank you for adding reminder-bot! To begin, type `$help`!')
                break

            else:
                continue

    async def on_error(self, *a, **k):
        if self.session is not None:
            self.session.rollback()
        raise

    async def on_ready(self):

        print('Logged in as')
        print(self.user.name)
        print(self.user.id)

        self.match_string = \
            r'(?:(?:<@ID>\s+)|(?:<@!ID>\s+)|(?P<prefix>\S{1,5}?))(?P<cmd>COMMANDS)(?:$|\s+(?P<args>.*))' \
                .replace('ID', str(self.user.id)).replace('COMMANDS', self.joined_names)

        self.c_session: aiohttp.client.ClientSession = aiohttp.ClientSession()

        if self.config.patreon_enabled:
            print('Patreon is enabled. Will look for servers {}'.format(self.config.patreon_server))

        print('Local timezone set to *{}*'.format(self.config.local_timezone))
        print('Local language set to *{}*'.format(self.config.local_language))

    async def on_guild_join(self, guild):
        await self.send()

        await self.welcome(guild)

    # noinspection PyMethodMayBeStatic
    async def on_guild_remove(self, guild):
        self.session.query(Guild).filter(Guild.guild == guild.id).delete(synchronize_session='fetch')

    # noinspection PyMethodMayBeStatic
    async def on_guild_channel_delete(self, channel):
        self.session.query(Channel).filter(Channel.channel == channel.id).delete(synchronize_session='fetch')

    async def send(self):
        if self.config.dbl_token:
            guild_count = len(self.guilds)

            dump = json_dump({
                'server_count': guild_count
            })

            head = {
                'authorization': self.config.dbl_token,
                'content-type': 'application/json'
            }

            url = 'https://discordbots.org/api/bots/stats'
            async with self.c_session.post(url, data=dump, headers=head) as resp:
                print('returned {0.status} for {1}'.format(resp, dump))

    # noinspection PyBroadException
    async def on_message(self, message):

        def _check_self_permissions(_channel):
            p = _channel.permissions_for(message.guild.me)

            return p.send_messages and p.embed_links

        async def _get_user(_message):
            _user = self.session.query(User).filter(User.user == message.author.id).first()
            if _user is None:
                dm_channel_id = (await message.author.create_dm()).id

                c = self.session.query(Channel).filter(Channel.channel == dm_channel_id).first()

                if c is None:
                    c = Channel(channel=dm_channel_id)
                    self.session.add(c)
                    self.session.flush()

                    _user = User(user=_message.author.id, dm_channel=c.id, name='{}#{}'.format(
                        _message.author.name, _message.author.discriminator))
                    self.session.add(_user)
                    self.session.flush()

            return _user

        if (message.author.bot and self.config.ignore_bots) or \
                message.content is None or \
                message.tts or \
                len(message.attachments) > 0 or \
                self.match_string is None:

            # either a bot or cannot be a command
            return

        elif message.guild is None:
            with self.get_session() as session:
                # command has been DMed. dont check for prefix :)
                split = message.content.split(' ')

                command_word = split[0].lower()
                if len(command_word) > 0:
                    if command_word[0] == '$':
                        command_word = command_word[1:]

                    args = ' '.join(split[1:]).strip()

                    if command_word in self.command_names:
                        command = self.commands[command_word]

                        if command.allowed_dm:
                            # get user
                            user = await _get_user(message)

                            await command.func(message, args, Preferences(None, user))

        elif _check_self_permissions(message.channel):
            with self.get_session() as session:
                # command sent in guild. check for prefix & call
                match = re.match(
                    self.match_string,
                    message.content,
                    re.MULTILINE | re.DOTALL | re.IGNORECASE
                )

                if match is not None:
                    # matched command structure; now query for guild to compare prefix
                    guild = session.query(Guild).filter(Guild.guild == message.guild.id).first()
                    if guild is None:
                        guild = Guild(guild=message.guild.id)

                        session.add(guild)
                        session.flush()

                # if none, suggests mention has been provided instead since pattern still matched
                if (prefix := match.group('prefix')) in (guild.prefix, None):
                    # prefix matched, might as well get the user now since this is a very small subset of messages
                    user = await _get_user(message)

                    if guild not in user.guilds:
                        guild.users.append(user)

                    # create the nice info manager
                    info = Preferences(guild, user)

                    command_word = match.group('cmd').lower()
                    stripped = match.group('args') or ''
                    command = self.commands[command_word]

                    # some commands dont get blacklisted e.g help, blacklist
                    if command.blacklists:
                        channel, just_created = Channel.get_or_create(message.channel)

                        if channel.guild_id is None:
                            channel.guild_id = guild.id

                        await channel.attach_webhook(message.channel)

                        if channel.blacklisted:
                            await message.channel.send(
                                embed=discord.Embed(description=info.language.get_string(self.session, 'blacklisted')))
                            return

                    # blacklist checked; now do command permissions
                    if command.check_permissions(message.author, guild):
                        if message.guild.me.guild_permissions.manage_webhooks:
                            await command.func(message, stripped, info)
                            session.commit()

                        else:
                            await message.channel.send(info.language.get_string(self.session, 'no_perms_webhook'))

                    else:
                        await message.channel.send(
                            info.language.get_string(
                                str(command.permission_level)).format(prefix=prefix))

        else:
            return

    async def time_stats(self, message, *_):
        uptime: float = unix_time() - self.start_time

        message_ts: float = message.created_at.timestamp()

        m: discord.Message = await message.channel.send('.')

        ping: float = m.created_at.timestamp() - message_ts

        await m.edit(content='''
        Uptime: {}s
        Ping: {}ms
        '''.format(round(uptime), round(ping * 1000)))

    async def help(self, message, _stripped, preferences):
        await message.channel.send(embed=discord.Embed(
            description=preferences.language.get_string(self.session, 'help'),
            color=THEME_COLOR
        ))

    async def info(self, message, _stripped, preferences):
        await message.channel.send(embed=discord.Embed(
            description=preferences.language.get_string(self.session, 'info').format(prefix=preferences.prefix, user=self.user.name),
            color=THEME_COLOR
        ))

    @staticmethod
    async def donate(message, _stripped, preferences):
        await message.channel.send(embed=discord.Embed(
            description=preferences.language.get_string(self.session, 'donate'),
            color=THEME_COLOR
        ))

    async def change_prefix(self, message, stripped, preferences):

        if stripped:

            stripped += ' '
            new = stripped[:stripped.find(' ')]

            if len(new) > 5:
                await message.channel.send(preferences.language.get_string(self.session, 'prefix/too_long'))

            else:
                preferences.prefix = new
                self.session.commit()

                await message.channel.send(preferences.language.get_string(self.session, 'prefix/success').format(
                    prefix=preferences.prefix))

        else:
            await message.channel.send(preferences.language.get_string(self.session, 'prefix/no_argument').format(
                prefix=preferences.prefix))

    async def create_alias(self, message, stripped, preferences):
        groups = re.fullmatch(r'(?P<name>[a-zA-Z0-9]{1,12})(?:(?: (?P<cmd>.*)$)|$)', stripped)

        if groups is not None:
            named_groups: typing.Dict[str, str] = groups.groupdict()

            name: str = named_groups['name']
            command: typing.Optional[str] = named_groups.get('cmd')

            if (name in ['list',
                         'remove'] or command is not None) and not message.author.guild_permissions.manage_guild:
                await message.channel.send(preferences.language.get_string(self.session, 'no_perms_restricted'))

            elif name == 'list':
                alias_concat = ''

                for alias in preferences.guild.aliases:
                    alias_concat += '**{}**: `{}`\n'.format(alias.name, alias.command)

                await message.channel.send('Aliases: \n{}'.format(alias_concat))

            elif name == 'remove':
                name = command

                query = self.session.query(CommandAlias) \
                    .filter(CommandAlias.name == name) \
                    .filter(CommandAlias.guild == preferences.guild)

                if query.first() is not None:
                    query.delete(synchronize_session='fetch')
                    await message.channel.send(preferences.language.get_string(self.session, 'alias/removed').format(count=1))

                else:
                    await message.channel.send(preferences.language.get_string(self.session, 'alias/removed').format(count=0))

            elif command is None:
                # command not specified so look for existing alias
                try:
                    aliased_command = next(filter(lambda alias: alias.name == name, preferences.guild.aliases))

                except StopIteration:
                    await message.channel.send(preferences.language.get_string(self.session, 'alias/not_found').format(name=name))

                else:
                    command = aliased_command.command
                    split = command.split(' ')

                    command_obj = self.commands.get(split[0])

                    if command_obj is None or command_obj.name == 'alias':
                        await message.channel.send(preferences.language.get_string(self.session, 'alias/invalid_command'))

                    elif command_obj.check_permissions(message.author, preferences.guild):
                        await command_obj.func(message, ' '.join(split[1:]), preferences)

                    else:
                        await message.channel.send(
                            preferences.language.get_string(self.session, str(command_obj.permission_level))
                                .format(prefix=preferences.guild.prefix))

            else:
                # command provided so create new alias
                if (cmd := command.split(' ')[0]) not in self.command_names and cmd not in ['alias', 'a']:
                    await message.channel.send(preferences.language.get_string(self.session, 'alias/invalid_command'))

                else:
                    if (alias := self.session.query(CommandAlias)
                            .filter_by(guild=preferences.guild, name=name).first()) is not None:

                        alias.command = command

                    else:
                        alias = CommandAlias(guild=preferences.guild, command=command, name=name)
                        self.session.add(alias)

                    self.session.commit()

                    await message.channel.send(preferences.language.get_string(self.session, 'alias/created').format(name=name))

        else:
            await message.channel.send(preferences.language.get_string(self.session, 'alias/help').format(prefix=preferences.guild.prefix))

    async def set_timezone(self, message, stripped, preferences):

        if message.guild is not None and message.author.guild_permissions.manage_guild:
            s = 'timezone/set'
            admin = True
        else:
            s = 'timezone/set_p'
            admin = False

        if stripped == '':
            await message.channel.send(embed=discord.Embed(
                description=preferences.language.get_string(self.session, 'timezone/no_argument').format(
                    prefix=preferences.prefix, timezone=preferences.timezone)))

        else:
            if stripped not in pytz.all_timezones:
                await message.channel.send(
                    embed=discord.Embed(description=preferences.language.get_string(self.session, 'timezone/no_timezone')))
            else:
                if admin:
                    preferences.server_timezone = stripped
                else:
                    preferences.timezone = stripped

                d = datetime.now(pytz.timezone(stripped))

                await message.channel.send(embed=discord.Embed(
                    description=preferences.language.get_string(self.session, s).format(
                        timezone=stripped, time=d.strftime('%H:%M:%S'))))

                self.session.commit()

    async def set_language(self, message, stripped, preferences):

        new_lang = self.session.query(Language).filter(
            (Language.code == stripped.upper()) | (Language.name == stripped.lower())).first()

        if new_lang is not None:
            preferences.language = new_lang.code
            self.session.commit()

            await message.channel.send(embed=discord.Embed(description=new_lang.get_string(self.session, 'lang/set_p')))

        else:
            await message.channel.send(
                embed=discord.Embed(description=preferences.language.get_string(self.session, 'lang/invalid').format(
                    '\n'.join(
                        ['{} ({})'.format(lang.name.title(), lang.code.upper()) for lang in self.session.query(Language)])
                )
                )
            )

    async def clock(self, message, stripped, preferences):

        if '12' in stripped:
            f_string = '%I:%M:%S %p'
        else:
            f_string = '%H:%M:%S'

        t = datetime.now(pytz.timezone(preferences.timezone))

        await message.channel.send(preferences.language.get_string(self.session, 'clock/time').format(t.strftime(f_string)))

    async def natural(self, message, stripped, server):

        if len(stripped.split(server.language.get_string(self.session, 'natural/send'))) < 2:
            await message.channel.send(embed=discord.Embed(
                description=server.language.get_string(self.session, 'natural/no_argument').format(prefix=server.prefix)))
            return

        location_ids: typing.List[int] = [message.channel.id]

        time_crop = stripped.split(server.language.get_string(self.session, 'natural/send'))[0]
        message_crop = stripped.split(server.language.get_string(self.session, 'natural/send'), 1)[1]
        datetime_obj = await self.do_blocking(partial(dateparser.parse, time_crop, settings={
            'TIMEZONE': server.timezone,
            'TO_TIMEZONE': self.config.local_timezone,
            'RELATIVE_BASE': datetime.now(pytz.timezone(server.timezone)).replace(tzinfo=None),
            'PREFER_DATES_FROM': 'future'
        }))

        if datetime_obj is None:
            await message.channel.send(
                embed=discord.Embed(description=server.language.get_string(self.session, 'natural/invalid_time')))
            return

        if message.guild is not None:
            chan_split = message_crop.split(server.language.get_string(self.session, 'natural/to'))
            if len(chan_split) > 1 and all(bool(set(x) & set('0123456789')) for x in chan_split[-1].split(' ')):
                location_ids = [int(''.join([x for x in z if x in '0123456789'])) for z in chan_split[-1].split(' ')]

                message_crop: str = message_crop.rsplit(server.language.get_string(self.session, 'natural/to'), 1)[0]

        interval_split = message_crop.split(server.language.get_string(self.session, 'natural/every'))
        recurring: bool = False
        interval: int = 0

        if len(interval_split) > 1:
            interval_dt = await self.do_blocking(partial(dateparser.parse, '1 ' + interval_split[-1]))

            if interval_dt is None:
                pass

            elif await self.is_patron(message.author.id):
                recurring = True

                interval = abs((interval_dt - datetime.now()).total_seconds())

                message_crop = message_crop.rsplit(server.language.get_string(self.session, 'natural/every'), 1)[0]

            else:
                await message.channel.send(embed=discord.Embed(
                    description=server.language.get_string(self.session, 'interval/donor').format(prefix=server.prefix)))
                return

        mtime: int = int(datetime_obj.timestamp())
        responses: typing.List[ReminderInformation] = []

        for location_id in location_ids:
            response: ReminderInformation = await self.create_reminder(message, location_id, message_crop, mtime,
                                                                       interval=interval if recurring else None,
                                                                       method='natural')
            responses.append(response)

        if len(responses) == 1:
            result: ReminderInformation = responses[0]
            string: str = NATURAL_STRINGS.get(result.status, REMIND_STRINGS[result.status])

            # hacky way of showing offset as days, hours, minutes, seconds without changing languages/ file
            delta_seconds = int(round(result.time - unix_time()))
            delta_seconds = max(delta_seconds, 0)   # don't go negative
            td = timedelta(seconds=delta_seconds)
            hours, remainder = divmod(td.seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            offset: str = ''
            if td.days != 0: offset += f"{td.days} day{'s' if td.days > 1 else ''}, "
            if hours != 0: offset += f"{hours} hour{'s' if hours > 1 else ''}, "
            if minutes != 0: offset += f"{minutes} minute{'s' if minutes > 1 else ''}, "
            offset += str(seconds)

            response = server.language.get_string(self.session, string).format(location=result.location.mention,
                                                                               offset=offset,
                                                                               min_interval=MIN_INTERVAL, max_time=MAX_TIME_DAYS)

            await message.channel.send(embed=discord.Embed(description=response))

        else:
            successes: int = len([r for r in responses if r.status == CreateReminderResponse.OK])

            await message.channel.send(
                embed=discord.Embed(description=server.language.get_string(self.session, 'natural/bulk_set').format(successes)))

    async def remind_cmd(self, message, stripped, server):
        await self.remind(False, message, stripped, server)

    async def interval_cmd(self, message, stripped, server):
        await self.remind(True, message, stripped, server)

    async def remind(self, is_interval, message, stripped, server):

        def filter_blanks(args, max_blanks=2):
            actual_args = 0

            for arg in args:
                if len(arg) == 0 and actual_args <= max_blanks:
                    continue

                else:
                    actual_args += 1
                    yield arg

        args = [x for x in filter_blanks(stripped.split(' '))]

        if len(args) < 2:
            if is_interval:
                await message.channel.send(embed=discord.Embed(
                    description=server.language.get_string(self.session, 'interval/no_argument').format(prefix=server.prefix)))

            else:
                await message.channel.send(embed=discord.Embed(
                    description=server.language.get_string(self.session, 'remind/no_argument').format(prefix=server.prefix)))

        else:
            if is_interval and not await self.is_patron(message.author.id):
                await message.channel.send(
                    embed=discord.Embed(description=server.language.get_string(self.session, 'interval/donor')))

            else:
                interval = None
                scope_id = message.channel.id

                if args[0][0] == '<' and message.guild is not None:
                    arg = args.pop(0)
                    scope_id = int(''.join(x for x in arg if x in '0123456789'))

                t = args.pop(0)
                time_parser = TimeExtractor(t, server.timezone)

                try:
                    mtime = time_parser.extract_exact()

                except InvalidTime:
                    await message.channel.send(
                        embed=discord.Embed(description=server.language.get_string(self.session, 'remind/invalid_time')))
                else:
                    if is_interval:
                        i = args.pop(0)

                        parser = TimeExtractor(i, server.timezone)

                        try:
                            interval = parser.extract_displacement()

                        except InvalidTime:
                            await message.channel.send(embed=discord.Embed(
                                description=server.language.get_string(self.session, 'interval/invalid_interval')))
                            return

                    text = ' '.join(args)

                    result = await self.create_reminder(message, scope_id, text, mtime, interval, method='remind')

                    response = server.language.get_string(self.session, REMIND_STRINGS[result.status]).format(
                        location=result.location.mention, offset=timedelta(seconds=int(result.time - unix_time())),
                        min_interval=MIN_INTERVAL, max_time=MAX_TIME_DAYS)

                    await message.channel.send(embed=discord.Embed(description=response))

    async def create_reminder(self, message: discord.Message, location: int, text: str, time: int,
                              interval: typing.Optional[int] = None, method: str = 'natural') -> ReminderInformation:
        ut: float = unix_time()

        if time > ut + MAX_TIME:
            return ReminderInformation(CreateReminderResponse.LONG_TIME)

        elif time < ut:

            if (ut - time) < 10:
                time = int(ut)

            else:
                return ReminderInformation(CreateReminderResponse.PAST_TIME)

        channel: typing.Optional[Channel] = None
        user: typing.Optional[User] = None

        creator: User = User.from_discord(message.author)

        # noinspection PyUnusedLocal
        discord_channel: typing.Optional[typing.Union[discord.TextChannel, DMChannelId]] = None

        # command fired inside a guild
        if message.guild is not None:
            discord_channel = message.guild.get_channel(location)

            if discord_channel is not None:  # if not a DM reminder

                channel, _ = Channel.get_or_create(discord_channel)

                await channel.attach_webhook(discord_channel)

                time += channel.nudge

            else:
                user = await self.find_and_create_member(location, message.guild)

                if user is None:
                    return ReminderInformation(CreateReminderResponse.INVALID_TAG)

                discord_channel = DMChannelId(user.dm_channel, user.user)

        # command fired in a DM; only possible target is the DM itself
        else:
            user = User.from_discord(message.author)
            discord_channel = DMChannelId(user.dm_channel, message.author.id)

        if interval is not None:
            if MIN_INTERVAL > interval:
                return ReminderInformation(CreateReminderResponse.SHORT_INTERVAL)

            elif interval > MAX_TIME:
                return ReminderInformation(CreateReminderResponse.LONG_INTERVAL)

            else:
                # noinspection PyArgumentList
                reminder = Reminder(
                    message=Message(content=text),
                    channel=channel or user.channel,
                    time=time,
                    enabled=True,
                    method=method,
                    interval=interval,
                    set_by=creator.id)
                self.session.add(reminder)
                self.session.commit()

        else:
            # noinspection PyArgumentList
            reminder = Reminder(
                message=Message(content=text),
                channel=channel or user.channel,
                time=time,
                enabled=True,
                method=method,
                set_by=creator.id)
            self.session.add(reminder)
            self.session.commit()

        return ReminderInformation(CreateReminderResponse.OK, channel=discord_channel, time=time)

    @staticmethod
    async def timer(self, message, stripped, preferences):
        owner: int = message.guild.id

        if message.guild is None:
            owner = message.author.id

        if stripped == 'list':
            timers = self.session.query(Timer).filter(Timer.owner == owner)

            e = discord.Embed(title='Timers')
            for timer in timers:
                delta = int((datetime.now() - timer.start_time).total_seconds())
                minutes, seconds = divmod(delta, 60)
                hours, minutes = divmod(minutes, 60)
                e.add_field(name=timer.name, value="{:02d}:{:02d}:{:02d}".format(hours, minutes, seconds))

            await message.channel.send(embed=e)

        elif stripped.startswith('start'):
            timers = self.session.query(Timer).filter(Timer.owner == owner)

            if timers.count() >= 25:
                await message.channel.send(preferences.language.get_string(self.session, 'timer/limit'))

            else:
                n = stripped.split(' ')[1:2] or 'New timer #{}'.format(timers.count() + 1)

                if len(n) > 32:
                    await message.channel.send(preferences.language.get_string(self.session, 'timer/name_length').format(len(n)))

                elif n in [x.name for x in timers]:
                    await message.channel.send(preferences.language.get_string(self.session, 'timer/unique'))

                else:
                    t = Timer(name=n, owner=owner)

                    self.session.add(t)
                    self.session.commit()

                    await message.channel.send(preferences.language.get_string(self.session, 'timer/success'))

        elif stripped.startswith('delete '):

            n = ' '.join(stripped.split(' ')[1:])

            timers = self.session.query(Timer).filter(Timer.owner == owner).filter(Timer.name == n)

            if timers.count() < 1:
                await message.channel.send(preferences.language.get_string(self.session, 'timer/not_found'))

            else:
                timers.delete(synchronize_session='fetch')
                self.session.commit()

                await message.channel.send(preferences.language.get_string(self.session, 'timer/deleted'))

        else:
            await message.channel.send(preferences.language.get_string(self.session, 'timer/help'))

    @staticmethod
    async def blacklist(self, message, _, preferences):

        target_channel = message.channel_mentions[0] if len(message.channel_mentions) > 0 else message.channel

        channel, _ = Channel.get_or_create(target_channel)

        channel.blacklisted = not channel.blacklisted

        if channel.blacklisted:
            await message.channel.send(
                embed=discord.Embed(description=preferences.language.get_string(self.session, 'blacklist/added')))

        else:
            await message.channel.send(
                embed=discord.Embed(description=preferences.language.get_string(self.session, 'blacklist/removed')))

        self.session.commit()

    async def restrict(self, message, stripped, preferences):

        role_tag = re.search(r'<@&([0-9]+)>', stripped)

        args: typing.List[str] = re.findall(r'([a-z]+)', stripped)

        if len(args) == 0:
            if role_tag is None:
                # no parameters given so just show existing
                await message.channel.send(
                    embed=discord.Embed(
                        description=preferences.language.get_string(self.session, 'restrict/allowed').format(
                            '\n'.join(
                                ['{} can use `{}`'.format(r.role, r.command)
                                 for r in preferences.command_restrictions]
                            )
                        )
                    )
                )

            else:
                # only a role is given so delete all the settings for this role
                role_query = preferences.guild.roles.filter(Role.role == int(role_tag.group(1)))

                if (role := role_query.first()) is not None:
                    preferences.command_restrictions \
                        .filter(CommandRestriction.role == role) \
                        .delete(synchronize_session='fetch')

                await message.channel.send(
                    embed=discord.Embed(description=preferences.language.get_string(self.session, 'restrict/disabled')))

        elif role_tag is None:
            # misused- show help
            await message.channel.send(embed=discord.Embed(
                description=preferences.language.get_string(self.session, 'restrict/help')))

        else:
            # enable permissions for role for selected commands
            role_id: int = int(role_tag.group(1))
            enabled: bool = False

            for command in filter(lambda x: len(x) <= 9, args):
                c: typing.Optional[Command] = self.commands.get(command)

                if c is not None and c.permission_level == PermissionLevels.MANAGED:
                    role_query = preferences.guild.roles.filter(Role.role == role_id)

                    if (role := role_query.first()) is not None:

                        q = preferences.command_restrictions \
                            .filter(CommandRestriction.command == c.name) \
                            .filter(CommandRestriction.role == role)

                        if q.first() is None:
                            new_restriction = CommandRestriction(guild_id=preferences.guild.id, command=c.name,
                                                                 role=role)

                            enabled = True

                            self.session.add(new_restriction)

                    else:
                        role = Role(role=role_id, guild=preferences.guild)
                        new_restriction = CommandRestriction(guild_id=preferences.guild.id, command=c.name, role=role)

                        self.session.add(new_restriction)

                else:
                    await message.channel.send(embed=discord.Embed(
                        description=preferences.language.get_string(self.session, 'restrict/failure').format(command=command)))

            if enabled:
                await message.channel.send(embed=discord.Embed(
                    description=preferences.language.get_string(self.session, 'restrict/enabled')))

        self.session.commit()

    async def todo(self, message, stripped, preferences):
        await self.todo_command(message, stripped, preferences, 'todo')

    async def todos(self, message, stripped, preferences):
        await self.todo_command(message, stripped, preferences, 'todos')

    async def todo_command(self, message, stripped, preferences, command):
        if command == 'todos':
            location, _ = Channel.get_or_create(message.channel)
            name = 'Channel'
            todos = location.todo_list.all() + preferences.guild.todo_list.filter(Todo.channel_id.is_(None)).all()
            channel = location
            guild = preferences.guild

        else:
            location = preferences.user
            name = 'Your'
            todos = location.todo_list.filter(Todo.guild_id.is_(None)).all()
            channel = None
            guild = None

        splits = stripped.split(' ')

        if len(splits) == 1 and splits[0] == '':
            msg = ['\n{}: {}'.format(i, todo.value) for i, todo in enumerate(todos, start=1)]
            if len(msg) == 0:
                msg.append(preferences.language.get_string(self.session, 'todo/add').format(
                    prefix=preferences.prefix, command=command))

            s = ''
            for item in msg:
                if len(item) + len(s) < 2048:
                    s += item
                else:
                    await message.channel.send(embed=discord.Embed(title='{} TODO'.format(name), description=s))
                    s = ''

            if len(s) > 0:
                await message.channel.send(embed=discord.Embed(title='{} TODO'.format(name), description=s))

        elif len(splits) >= 2:
            if splits[0] == 'add':
                s = ' '.join(splits[1:])

                todo = Todo(value=s, guild=guild, user=preferences.user, channel=channel)
                self.session.add(todo)
                await message.channel.send(preferences.language.get_string(self.session, 'todo/added').format(name=s))

            elif splits[0] == 'remove':
                try:
                    todo = self.session.query(Todo).filter(Todo.id == todos[int(splits[1]) - 1].id).first()
                    self.session.query(Todo).filter(Todo.id == todos[int(splits[1]) - 1].id).delete(
                        synchronize_session='fetch')

                    await message.channel.send(preferences.language.get_string(self.session, 'todo/removed').format(todo.value))

                except ValueError:
                    await message.channel.send(
                        preferences.language.get_string(self.session, 'todo/error_value').format(
                            prefix=preferences.prefix, command=command))

                except IndexError:
                    await message.channel.send(preferences.language.get_string(self.session, 'todo/error_index'))

            else:
                await message.channel.send(
                    preferences.language.get_string(self.session, 'todo/help').format(prefix=preferences.prefix, command=command))

        else:
            if stripped == 'clear':
                todos.clear()
                await message.channel.send(preferences.language.get_string(self.session, 'todo/cleared'))

            else:
                await message.channel.send(
                    preferences.language.get_string(self.session, 'todo/help').format(prefix=preferences.prefix, command=command))

        self.session.commit()

    async def delete(self, message, _stripped, preferences):
        if message.guild is not None:
            channels = preferences.guild.channels
            reminders = itertools.chain(*[c.reminders for c in channels])

        else:
            reminders = preferences.user.channel.reminders

        await message.channel.send(preferences.language.get_string(self.session, 'del/listing'))

        enumerated_reminders = [x for x in enumerate(reminders, start=1)]

        s = ''
        for count, reminder in enumerated_reminders:
            string = '''**{}**: '{}' *{}* at {}\n'''.format(
                count,
                reminder.message_content(),
                reminder.channel,
                datetime.fromtimestamp(reminder.time, pytz.timezone(preferences.timezone)).strftime(
                    '%Y-%m-%d %H:%M:%S'))

            if len(s) + len(string) > 2000:
                await message.channel.send(s, allowed_mentions=NoMention)
                s = string
            else:
                s += string

        if s:
            await message.channel.send(s, allowed_mentions=NoMention)

        await message.channel.send(preferences.language.get_string(self.session, 'del/listed'))

        try:
            num = await client.wait_for('message',
                                        check=lambda m: m.author == message.author and m.channel == message.channel,
                                        timeout=30)

        except asyncio.exceptions.TimeoutError:
            pass

        else:
            num_content = num.content.replace(',', ' ')

            nums = set([int(x) for x in re.findall(r'(\d+)(?:\s|$)', num_content)])

            removal_ids: typing.Set[int] = set()

            for count, reminder in enumerated_reminders:
                if count in nums:
                    removal_ids.add(reminder.id)
                    nums.remove(count)

            if message.guild is not None:
                deletion_event = Event(
                    event_name='delete', bulk_count=len(removal_ids), guild=preferences.guild, user=preferences.user)
                self.session.add(deletion_event)

            self.session.query(Reminder).filter(Reminder.id.in_(removal_ids)).delete(synchronize_session='fetch')
            self.session.commit()

            await message.channel.send(preferences.language.get_string(self.session, 'del/count').format(len(removal_ids)))

    async def look(self, message, stripped, preferences):

        def relative_time(t):
            days, seconds = divmod(int(t - unix_time()), 86400)
            hours, seconds = divmod(seconds, 3600)
            minutes, seconds = divmod(seconds, 60)

            sections = []

            for var, name in zip((days, hours, minutes, seconds), ('days', 'hours', 'minutes', 'seconds')):
                if var > 0:
                    sections.append('{} {}'.format(var, name))

            return ', '.join(sections)

        def absolute_time(t):
            return datetime.fromtimestamp(t, pytz.timezone(preferences.timezone)).strftime('%Y-%m-%d %H:%M:%S')

        r = re.search(r'(\d+)', stripped)

        limit: typing.Optional[int] = None
        if r is not None:
            limit = int(r.groups()[0])

        if 'enabled' in stripped:
            show_disabled = False
        else:
            show_disabled = True

        if 'time' in stripped:
            time_func = absolute_time

        else:
            time_func = relative_time

        if message.guild is None:
            channel = preferences.user.channel
            new = False

        else:
            discord_channel = message.channel_mentions[0] if len(message.channel_mentions) > 0 else message.channel

            channel, new = Channel.get_or_create(discord_channel)

        if new:
            await message.channel.send(preferences.language.get_string(self.session, 'look/no_reminders'))

        else:
            reminder_query = channel.reminders.order_by(Reminder.time)

            if not show_disabled:
                reminder_query = reminder_query.filter(Reminder.enabled)

            if limit is not None:
                reminder_query = reminder_query.limit(limit)

            if reminder_query.count() > 0:
                if limit is not None:
                    await message.channel.send(preferences.language.get_string(self.session, 'look/listing_limited').format(
                        reminder_query.count()))

                else:
                    await message.channel.send(preferences.language.get_string(self.session, 'look/listing'))

                s = ''
                for reminder in reminder_query:
                    string = '\'{}\' *{}* **{}** {}\n'.format(
                        reminder.message_content(),
                        preferences.language.get_string(self.session, 'look/inter'),
                        time_func(reminder.time),
                        '' if reminder.enabled else '`disabled`')

                    if len(s) + len(string) > 2000:
                        await message.channel.send(s, allowed_mentions=NoMention)
                        s = string
                    else:
                        s += string

                await message.channel.send(s, allowed_mentions=NoMention)

            else:
                await message.channel.send(preferences.language.get_string(self.session, 'look/no_reminders'))

    async def offset_reminders(self, message, stripped, preferences):

        if message.guild is None:
            reminders = preferences.user.reminders
        else:
            reminders = itertools.chain(*[channel.reminders for channel in preferences.guild.channels])

        time_parser = TimeExtractor(stripped, preferences.timezone)

        try:
            time = time_parser.extract_displacement()

        except InvalidTime:
            await message.channel.send(
                embed=discord.Embed(description=preferences.language.get_string(self.session, 'offset/invalid_time')))

        else:
            if time == 0:
                await message.channel.send(embed=discord.Embed(
                    description=preferences.language.get_string(self.session, 'offset/help').format(prefix=preferences.prefix)))

            else:
                c = 0
                for r in reminders:
                    c += 1
                    r.time += time

                if message.guild is not None:
                    edit_event = Event(
                        event_name='edit', bulk_count=c, guild=preferences.guild, user=preferences.user)
                    self.session.add(edit_event)

                self.session.commit()

                await message.channel.send(
                    embed=discord.Embed(description=preferences.language.get_string(self.session, 'offset/success').format(time)))

    async def nudge_channel(self, message, stripped, preferences):

        time_parser = TimeExtractor(stripped, preferences.timezone)

        try:
            t = time_parser.extract_displacement()

        except InvalidTime:
            await message.channel.send(embed=discord.Embed(
                description=preferences.language.get_string(self.session, 'nudge/invalid_time')))

        else:
            if 2 ** 15 > t > -2 ** 15:
                channel, _ = Channel.get_or_create(message.channel)

                channel.nudge = t

                self.session.commit()

                await message.channel.send(
                    embed=discord.Embed(description=preferences.language.get_string(self.session, 'nudge/success').format(t)))

            else:
                await message.channel.send(
                    embed=discord.Embed(description=preferences.language.get_string(self.session, 'nudge/invalid_time')))

    async def pause_channel(self, message, stripped, preferences):

        channel, _ = Channel.get_or_create(message.channel)

        if len(stripped) > 0:
            # argument provided for time
            time_parser = TimeExtractor(stripped, preferences.timezone)

            try:
                t = time_parser.extract_displacement()

            except InvalidTime:
                await message.channel.send(embed=discord.Embed(
                    description=preferences.language.get_string(self.session, 'pause/invalid_time')))

            else:
                channel.paused = True
                channel.paused_until = datetime.now() + timedelta(seconds=t)

                display = channel.paused_until \
                    .astimezone(pytz.timezone(preferences.timezone)) \
                    .strftime('%Y-%m-%d, %H:%M:%S')

                await message.channel.send(
                    embed=discord.Embed(description=preferences.language.get_string(self.session, 'pause/paused_until').format(display)))

        else:
            # otherwise toggle the paused status and clear the time
            channel.paused = not channel.paused
            channel.paused_until = None

            if channel.paused:
                await message.channel.send(
                    embed=discord.Embed(description=preferences.language.get_string(self.session, 'pause/paused_indefinite')))

            else:
                await message.channel.send(
                    embed=discord.Embed(description=preferences.language.get_string(self.session, 'pause/unpaused')))


client = BotClient(max_messages=100, guild_subscriptions=False, fetch_offline_members=False)
client.run(client.config.token)
