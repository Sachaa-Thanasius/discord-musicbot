from __future__ import annotations

import json
import logging
from typing import Literal

import discord
import platformdirs
import wavelink
import xxhash
from discord import app_commands

from .utils import (
    LavalinkCreds,
    MusicBotError,
    MusicPlayer,
    MusicQueueView,
    ShortTime,
    WavelinkSearchTransformer,
    create_track_embed,
    ensure_voice_hook,
    generate_tracks_add_notification,
    in_bot_vc,
    resolve_path_with_links,
)


log = logging.getLogger(__name__)

platformdir_info = platformdirs.PlatformDirs("discord-musicbot", "Sachaa-Thanasius", roaming=False)


@app_commands.command(name="connect")
@app_commands.guild_only()
async def muse_connect(itx: discord.Interaction[MusicBot]) -> None:
    """Join a voice channel."""

    # Known at runtime.
    assert itx.guild and isinstance(itx.user, discord.Member)
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc is not None and itx.user.voice is not None:
        if vc.channel != itx.user.voice.channel:
            if itx.user.guild_permissions.administrator:
                # Not sure in what circumstances a member would have a voice state without being in a valid channel.
                await vc.move_to(itx.user.voice.channel)
                await itx.response.send_message(f"Joined the {itx.user.voice.channel} channel.")
            else:
                await itx.response.send_message("Voice player is currently being used in another channel.")
        else:
            await itx.response.send_message("Voice player already connected to this voice channel.")
    elif itx.user.voice is None:
        await itx.response.send_message("Please join a voice channel and try again.")
    else:
        # Not sure in what circumstances a member would have a voice state without being in a valid channel.
        assert itx.user.voice.channel
        await itx.user.voice.channel.connect(cls=MusicPlayer)
        await itx.response.send_message(f"Joined the {itx.user.voice.channel} channel.")


@app_commands.command(name="play")
@app_commands.guild_only()
@ensure_voice_hook
async def muse_play(
    itx: discord.Interaction[MusicBot],
    search: app_commands.Transform[wavelink.Playable | wavelink.Playlist, WavelinkSearchTransformer],
) -> None:
    """Play audio from a YouTube url or search term.

    Parameters
    ----------
    itx : :class:`discord.Interaction`
        The invocation context.
    search : AnyTrack | AnyTrackIterable
        A search term/url that is converted into a track or list of tracks.
    """

    # Known at runtime.
    assert itx.guild
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer)  # Known due to ensure_voice_hook.

    if isinstance(search, wavelink.Playable):
        search.requester = itx.user.mention  # type: ignore # Runtime attribute assignment.
    else:
        search.track_extras(requester=itx.user.mention)

    await vc.queue.put_wait(search)
    notif_text = generate_tracks_add_notification(search)
    await itx.followup.send(notif_text)

    if not vc.playing:
        first_track = vc.queue.get()
        await vc.play(first_track)


@app_commands.command(name="pause")
@app_commands.guild_only()
@app_commands.check(in_bot_vc)
async def muse_pause(itx: discord.Interaction[MusicBot]) -> None:
    """Pause the audio."""

    # Known at runtime.
    assert itx.guild
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        pause_changed_status = "Resumed" if vc.paused else "Paused"
        await vc.pause(not vc.paused)
        await itx.response.send_message(f"{pause_changed_status} playback.")
    else:
        await itx.response.send_message("No player to perform this on.")


@app_commands.command(name="resume")
@app_commands.guild_only()
@app_commands.check(in_bot_vc)
async def muse_resume(itx: discord.Interaction[MusicBot]) -> None:
    """Resume the audio if paused."""

    # Known at runtime.
    assert itx.guild
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        if vc.paused:
            await vc.pause(False)
            await itx.response.send_message("Resumed playback.")
        else:
            await itx.response.send_message("The music player is not paused.")
    else:
        await itx.response.send_message("No player to perform this on.")


@app_commands.command(name="stop")
@app_commands.guild_only()
@app_commands.check(in_bot_vc)
async def muse_stop(itx: discord.Interaction[MusicBot]) -> None:
    """Stop playback and disconnect the bot from voice."""

    # Known at runtime.
    assert itx.guild
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        await vc.disconnect()
        await itx.response.send_message("Disconnected from voice channel.")
    else:
        await itx.response.send_message("No player to perform this on.")


@app_commands.command(name="current")
@app_commands.guild_only()
async def muse_current(itx: discord.Interaction[MusicBot]) -> None:
    """Display the current track."""

    # Known at runtime.
    assert itx.guild
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc and vc.current:
        current_embed = create_track_embed("Now Playing", vc.current)
    else:
        current_embed = discord.Embed(
            color=0x149CDF,
            title="Now Playing",
            description="Nothing is playing currently.",
        )

    await itx.response.send_message(embed=current_embed)


class MuseQueue(app_commands.Group):
    def __init__(self) -> None:
        super().__init__(
            name="queue",
            description="Music queue-related commands. Use `play` to add things to the queue.",
            guild_only=True,
        )
    
    @app_commands.command(name="get")
    async def queue_get(self, itx: discord.Interaction[MusicBot]) -> None:
        """Display everything in the queue."""

        # Known at runtime.
        assert itx.guild
        vc = itx.guild.voice_client
        assert isinstance(vc, MusicPlayer | None)

        queue_embeds: list[discord.Embed] = []
        if vc:
            if vc.current:
                current_embed = create_track_embed("Now Playing", vc.current)
                queue_embeds.append(current_embed)

            view = MusicQueueView(
                author_id=itx.user.id,
                pages_content=[track.title for track in vc.queue],
                per=10,
            )
            queue_embeds.append(view.get_first_page())
            await itx.response.send_message(embeds=queue_embeds, view=view)
            view.message = await itx.original_response()

    @app_commands.command(name="remove")
    @app_commands.check(in_bot_vc)
    async def queue_remove(self, itx: discord.Interaction[MusicBot], entry: int) -> None:
        """Remove a track from the queue by position.

        Parameters
        ----------
        itx : :class:`discord.Interaction`
            The interaction that triggered this command.
        entry : :class:`int`
            The track's position.
        """

        # Known at runtime.
        assert itx.guild
        vc = itx.guild.voice_client
        assert isinstance(vc, MusicPlayer | None)

        if vc:
            if entry > len(vc.queue) or entry < 1:
                await itx.response.send_message("That track does not exist and cannot be removed.")
            else:
                await vc.queue.delete(entry - 1)
                await itx.response.send_message(f"Removed {entry} from the queue.")
        else:
            await itx.response.send_message("No player to perform this on.")

    
    @app_commands.command(name="clear")
    @app_commands.check(in_bot_vc)
    async def queue_clear(self, itx: discord.Interaction[MusicBot]) -> None:
        """Empty the queue."""

        # Known at runtime.
        assert itx.guild
        vc = itx.guild.voice_client
        assert isinstance(vc, MusicPlayer | None)

        if vc:
            if vc.queue:
                vc.queue.clear()
                await itx.response.send_message("Queue cleared.")
            else:
                await itx.response.send_message("The queue is already empty.")
        else:
            await itx.response.send_message("No player to perform this on.")



@app_commands.command(name="move")
@app_commands.guild_only()
@app_commands.check(in_bot_vc)
async def muse_move(itx: discord.Interaction[MusicBot], before: int, after: int) -> None:
    """Move a track from one spot to another within the queue.

    Parameters
    ----------
    itx : :class:`discord.Interaction`
        The interaction that triggered this command.
    before : :class:`int`
        The index of the track you want moved.
    after : :class:`int`
        The index you want to move it to.
    """

    # Known at runtime.
    assert itx.guild
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        try:
            vc.queue.move(before - 1, after - 1)
        except IndexError:
            await itx.response.send_message("Please enter valid queue indices.")
        else:
            await itx.response.send_message(f"Successfully moved the track at {before} to {after} in the queue.")
    else:
        await itx.response.send_message("No player to perform this on.")


@app_commands.command(name="skip")
@app_commands.guild_only()
@app_commands.check(in_bot_vc)
async def muse_skip(itx: discord.Interaction[MusicBot], index: int = 1) -> None:
    """Skip to the numbered track in the queue. If no number is given, skip to the next track.

    Parameters
    ----------
    itx: :class:`discord.Interaction`
        The interaction that triggered this command.
    index : :class:`int`
        The place in the queue to skip to.
    """

    # Known at runtime.
    assert itx.guild
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        if not vc.queue:
            await itx.response.send_message("The queue is empty and can't be skipped into.")
            return

        try:
            vc.queue.skip_to(index - 1)
        except IndexError:
            await itx.response.send_message("Please enter a valid queue index.")
        else:
            await vc.skip()
            await itx.response.send_message(f"Skipped to the track at position {index}")
    else:
        await itx.response.send_message("No player to perform this on.")


@app_commands.command(name="shuffle")
@app_commands.guild_only()
@app_commands.check(in_bot_vc)
async def muse_shuffle(itx: discord.Interaction[MusicBot]) -> None:
    """Shuffle the tracks in the queue."""

    # Known at runtime.
    assert itx.guild
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        if vc.queue:
            vc.queue.shuffle()
            await itx.response.send_message("Shuffled the queue.")
        else:
            await itx.response.send_message("There's nothing in the queue to shuffle right now.")
    else:
        await itx.response.send_message("No player to perform this on.")


@app_commands.command(name="loop")
@app_commands.guild_only()
@app_commands.check(in_bot_vc)
async def muse_loop(
    itx: discord.Interaction[MusicBot],
    loop: Literal["All Tracks", "Current Track", "Off"] = "Off",
) -> None:
    """Loop the current track(s).

    Parameters
    ----------
    itx : :class:`discord.Interaction`
        The interaction that triggered this command.
    loop : Literal["All Tracks", "Current Track", "Off"]
        The loop settings. "All Tracks" loops everything in the queue, "Current Track" loops the playing track, and
        "Off" resets all looping.
    """

    # Known at runtime.
    assert itx.guild
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        if loop == "All Tracks":
            vc.queue.mode = wavelink.QueueMode.loop
            await itx.response.send_message("Looping over all tracks in the queue until disabled.")
        elif loop == "Current Track":
            vc.queue.mode = wavelink.QueueMode.loop_all
            await itx.response.send_message("Looping the current track until disabled.")
        else:
            vc.queue.mode = wavelink.QueueMode.normal
            await itx.response.send_message("Reset the looping settings.")
    else:
        await itx.response.send_message("No player to perform this on.")


@app_commands.command(name="seek")
@app_commands.guild_only()
@app_commands.check(in_bot_vc)
async def muse_seek(itx: discord.Interaction[MusicBot], position: ShortTime) -> None:
    """Seek to a particular position in the current track, provided with a `hours:minutes:seconds` string.

    Parameters
    ----------
    itx : :class:`discord.Interaction`
        The interaction that triggered this command.
    position : :class:`str`
        The time to jump to, given in a format like `<hours>:<minutes>:<seconds>` or `<minutes>:<seconds>`.
    """

    # Known at runtime.
    assert itx.guild
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        if vc.current:
            if vc.current.is_seekable:
                if position.seconds > vc.current.length or position.seconds < 0:
                    await itx.response.send_message("The track length doesn't support that position.")
                else:
                    await vc.seek(position.seconds)
                    await itx.response.send_message(f"Jumped to position `{position.original}` in the current track.")
            else:
                await itx.response.send_message("This track doesn't allow seeking, sorry.")
        else:
            await itx.response.send_message("No track currently playing to seek within.")
    else:
        await itx.response.send_message("No player to perform this on.")


@app_commands.command(name="volume")
@app_commands.guild_only()
@app_commands.check(in_bot_vc)
async def muse_volume(itx: discord.Interaction[MusicBot], volume: int | None = None) -> None:
    """Show the player's volume. If given a number, you can change it as well, with 1000 as the limit.

    Parameters
    ----------
    itx : :class:`discord.Interaction`
        The interaction that triggered this command.
    volume : :class:`int`, optional
        The volume to change to, with a maximum of 1000.
    """

    # Known at runtime.
    assert itx.guild
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        if volume is None:
            await itx.response.send_message(f"Current volume is {vc.volume}.")
        else:
            await vc.set_volume(volume)
            await itx.response.send_message(f"Changed volume to {volume}.")
    else:
        await itx.response.send_message("No player to perform this on.")


@app_commands.command()
async def invite(itx: discord.Interaction[MusicBot]) -> None:
    """Get a link to invite this bot to a server."""

    embed = discord.Embed(description="Click the link below to invite me to one of your servers.")
    view = discord.ui.View().add_item(discord.ui.Button(label="Invite", url=itx.client.invite_link))
    await itx.response.send_message(embed=embed, view=view, ephemeral=True)


APP_COMMANDS = [
    muse_connect,
    muse_play,
    muse_pause,
    muse_resume,
    muse_stop,
    muse_current,
    MuseQueue(),
    muse_move,
    muse_skip,
    muse_shuffle,
    muse_loop,
    muse_seek,
    muse_volume,
    invite,
]


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
    config : LavalinkCreds
        The configuration data for the bot, including Lavalink node credentials.

    Attributes
    ----------
    config : LavalinkCreds
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
