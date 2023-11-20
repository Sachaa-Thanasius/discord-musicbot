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


log = logging.getLogger(__name__)

platformdir_info = platformdirs.PlatformDirs("discord-musicbot", "Sachaa-Thanasius", roaming=False)


class VersionableTree(app_commands.CommandTree):
    """A command tree with a two new methods:

    1. Generate a unique hash to represent all commands currently in the tree.
    2. Compare hash of the current tree against that of a previous version using the above method.

    Credit to @mikeshardmind: Everything in this class is his.

    Notes
    -----
    The main use case is autosyncing using the hash comparison as a condition.
    """

    async def on_error(self, itx: discord.Interaction, error: app_commands.AppCommandError, /) -> None:
        """Attempt to catch any errors unique to this bot."""

        error = getattr(error, "__cause__", error)

        if isinstance(error, MusicBotError):
            send_method = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            await send_method(error.message)
        elif itx.command is not None:
            log.error("Ignoring exception in command %r", itx.command.name, exc_info=error)
        else:
            log.error("Ignoring exception in command tree", exc_info=error)

    def get_nested_command(
        self,
        name: str,
        *,
        guild: discord.Guild | None = None,
    ) -> app_commands.Command[Any, ..., Any] | app_commands.Group | None:
        """Retrieves a nested command or group from its name.

        Parameters
        -----------
        name: :class:`str`
            The name of the command or group to retrieve.

        Returns
        --------
        :class:`discord.app_commands.Command` | :class:`~discord.app_commands.Group` | None
            The command or group that was retrieved. If nothing was found
            then ``None`` is returned instead.
        """

        key, *keys = name.split(" ")
        cmd = self.get_command(key, guild=guild) or self.get_command(key)

        for key in keys:
            if cmd is None:
                return None
            if isinstance(cmd, app_commands.Command):
                break

            cmd = cmd.get_command(key)

        return cmd

    async def get_hash(self) -> bytes:
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
                log.info("New version of the command tree. Syncing now.")
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

    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
        """Called when a track starts playing.

        Sends a notification about the new track to the voice channel.
        """

        player = payload.player
        if not player:
            return

        current_embed = create_track_embed("Now Playing", payload.track)
        await player.channel.send(embed=current_embed)
