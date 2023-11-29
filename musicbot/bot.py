from __future__ import annotations

import json
import logging
from typing import Any

import discord
import platformdirs
import wavelink
import xxhash
from discord import app_commands

from .commands import APP_COMMANDS
from .utils import (
    LavalinkCreds,
    MusicBotError,
    create_track_embed,
    resolve_path_with_links,
)


_log = logging.getLogger(__name__)

platformdir_info = platformdirs.PlatformDirs("discord-musicbot", "Sachaa-Thanasius", roaming=False)


class VersionableTree(app_commands.CommandTree):
    """A custom command tree to handle autosyncing and save command mentions.

    Credit to LeoCx1000: The implemention for storing mentions of tree commands is his.
    https://gist.github.com/LeoCx1000/021dc52981299b95ea7790416e4f5ca4

    Credit to @mikeshardmind: The hashing methods in this class are his.
    https://github.com/mikeshardmind/discord-rolebot/blob/ff0ca542ccc54a5527935839e511d75d3d178da0/rolebot/__main__.py#L486
    """

    def __init__(self, client: MusicBot, *, fallback_to_global: bool = True) -> None:
        super().__init__(client, fallback_to_global=fallback_to_global)
        self.application_commands: dict[int | None, list[app_commands.AppCommand]] = {}

    async def sync(self, *, guild: discord.abc.Snowflake | None = None) -> list[app_commands.AppCommand]:
        ret = await super().sync(guild=guild)
        self.application_commands[guild.id if guild else None] = ret
        return ret

    async def fetch_commands(self, *, guild: discord.abc.Snowflake | None = None) -> list[app_commands.AppCommand]:
        ret = await super().fetch_commands(guild=guild)
        self.application_commands[guild.id if guild else None] = ret
        return ret

    async def on_error(self, itx: discord.Interaction, error: app_commands.AppCommandError, /) -> None:
        error = getattr(error, "__cause__", error)

        if isinstance(error, MusicBotError):
            if not itx.response.is_done():
                await itx.response.send_message(error.message)
            else:
                await itx.followup.send(error.message)
        elif itx.command is not None:
            _log.error("Ignoring exception in command %r", itx.command.name, exc_info=error)
        else:
            _log.error("Ignoring exception in command tree", exc_info=error)

    async def find_mention_for(
        self,
        command: app_commands.Command[Any, ..., Any],
        *,
        guild: discord.abc.Snowflake | None = None,
    ) -> str | None:
        """Retrieves the mention of an AppCommand given a specific Command and optionally, a guild.

        Parameters
        ----------
        command: :class:`app_commands.Command`
            The command which it's mention we will attempt to retrieve.
        guild: :class:`discord.abc.Snowflake` | None
            The scope (guild) from which to retrieve the commands from.
            If None is given or not passed, the global scope will be used.
        """

        try:
            found_commands = self.application_commands[guild.id if guild else None]
        except KeyError:
            found_commands = await self.fetch_commands(guild=guild)

        root_parent = command.root_parent or command
        command_found = discord.utils.get(found_commands, name=root_parent.name)
        if command_found:
            return f"</{command.qualified_name}:{command_found.id}>"
        return None

    async def get_hash(self) -> bytes:
        """Generate a unique hash to represent all commands currently in the tree."""

        tree_commands = sorted(self._get_all_commands(guild=None), key=lambda c: c.qualified_name)

        translator = self.translator
        if translator:
            payload = [await command.get_translated_payload(translator) for command in tree_commands]
        else:
            payload = [command.to_dict() for command in tree_commands]

        return xxhash.xxh3_64_digest(json.dumps(payload).encode("utf-8"), seed=1)

    async def sync_if_commands_updated(self) -> None:
        """Sync the tree globally if its commands are different from the tree's most recent previous version.

        Comparison is done with hashes, with the hash being stored in a specific file if unique for later comparison.

        Notes
        -----
        This uses blocking file IO, so don't run this in situations where that matters. `setup_hook` should be fine
        a fine place though.
        """

        tree_hash = await self.get_hash()
        tree_hash_path = platformdir_info.user_cache_path / "musicbot_tree.hash"
        tree_hash_path = resolve_path_with_links(tree_hash_path)
        with tree_hash_path.open("r+b") as fp:
            data = fp.read()
            if data != tree_hash:
                _log.info("New version of the command tree. Syncing now.")
                await self.sync()
                fp.seek(0)
                fp.write(tree_hash)


class MusicBot(discord.AutoShardedClient):
    """The Discord client subclass that provides music-related functionality.

    Parameters
    ----------
    config : :class:`LavalinkCreds`
        The configuration data for the bot, including Lavalink node credentials.

    Attributes
    ----------
    config : :class:`LavalinkCreds`
        The configuration data for the bot, including Lavalink node credentials.
    """

    def __init__(self, config: LavalinkCreds) -> None:
        self.config = config
        super().__init__(
            intents=discord.Intents(guilds=True, voice_states=True),
            activity=discord.Game(name="https://github.com/Sachaa-Thanasius/discord-musicbot"),
        )
        self.tree = VersionableTree(self)

    async def on_connect(self) -> None:
        """(Re)set the client's general invite link every time it (re)connects to the Discord Gateway."""

        await self.wait_until_ready()
        data = await self.application_info()
        perms = discord.Permissions(274881367040)
        self.invite_link = discord.utils.oauth_url(data.id, permissions=perms)

    async def setup_hook(self) -> None:
        """Perform a few operations before the bot connects to the Discord Gateway."""

        # Connect to the Lavalink node that will provide the music.
        node = wavelink.Node(uri=self.config.uri, password=self.config.password)
        await wavelink.Pool.connect(client=self, nodes=[node])

        # Add the app commands to the tree.
        for cmd in APP_COMMANDS:
            self.tree.add_command(cmd)

        # Sync the tree if it's different from the previous version, using hashing for comparison.
        await self.tree.sync_if_commands_updated()

    async def close(self) -> None:
        await wavelink.Pool.close()
        await super().close()

    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
        """Called when a track starts playing.

        Sends a notification about the new track to the voice channel.
        """

        player = payload.player
        if not player:
            return

        current_embed = create_track_embed("Now Playing", payload.original or payload.track)
        await player.channel.send(embed=current_embed)
