from __future__ import annotations

import json
from io import BytesIO
from typing import TYPE_CHECKING, Any, Literal

import discord
import wavelink
from discord import app_commands

from .utils import (
    MusicPlayer,
    MusicQueueView,
    ShortTime,
    create_track_embed,
    ensure_voice_hook,
    is_in_bot_vc,
)


if TYPE_CHECKING:
    from .bot import MusicBot
else:
    MusicBot = discord.AutoShardedClient


__all__ = ("APP_COMMANDS",)


@app_commands.command(name="connect")
@app_commands.guild_only()
async def muse_connect(itx: discord.Interaction[MusicBot], channel: discord.VoiceChannel | None = None) -> None:
    """Join a voice channel.

    Parameters
    ----------
    channel: discord.VoiceChannel | None, optional
        The voice channel to connect to if you aren't currently in a voice channel. Defaults to the current user
        channel.
    """

    # Known at runtime.
    assert itx.guild
    assert isinstance(itx.user, discord.Member)
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc is not None and itx.user.voice is not None:
        # Not sure in what circumstances a member would have a voice state without being in a valid channel.
        target_channel = channel or itx.user.voice.channel
        if target_channel != vc.channel:
            if itx.user.guild_permissions.administrator:
                await vc.move_to(target_channel)
                await itx.response.send_message(f"Joined the {target_channel} channel.")
            else:
                await itx.response.send_message("Voice player is currently being used in another channel.")
        else:
            await itx.response.send_message("Voice player already connected to this voice channel.")
    elif itx.user.voice is None:
        if itx.user.guild_permissions.administrator and channel is not None:
            await channel.connect(cls=MusicPlayer)
            await itx.response.send_message(f"Joined the {channel} channel.")
        else:
            await itx.response.send_message("Please join a voice channel and try again.")
    else:
        # Not sure in what circumstances a member would have a voice state without being in a valid channel.
        assert itx.user.voice.channel
        await itx.user.voice.channel.connect(cls=MusicPlayer)
        await itx.response.send_message(f"Joined the {itx.user.voice.channel} channel.")


@app_commands.command(name="play")
@app_commands.guild_only()
@ensure_voice_hook
async def muse_play(itx: discord.Interaction[MusicBot], query: str) -> None:
    """Play audio from a YouTube url or search term.

    Parameters
    ----------
    itx: discord.Interaction
        The invocation context.
    query: str
        A search term/url that is converted into a track or playlist.
    """

    # Known at runtime.
    assert itx.guild
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer)  # Known due to ensure_voice_hook.

    await itx.response.defer()

    tracks: wavelink.Search = await wavelink.Playable.search(query)
    if not tracks:
        await itx.followup.send(f"Could not find any tracks based on the given query: `{query}`.")

    if isinstance(tracks, wavelink.Playlist):
        try:
            tracks.extras.requester = itx.user.mention
        except AttributeError:
            tracks.extras = {"requester": itx.user.mention}

        added = await vc.queue.put_wait(tracks)
        await itx.followup.send(f"Added {added} tracks from the `{tracks.name}` playlist to the queue.")
    else:
        track = tracks[0]
        track.extras.requester = itx.user.mention
        await vc.queue.put_wait(track)
        await itx.followup.send(f"Added `{track.title}` to the queue.")

    if not vc.playing:
        await vc.play(vc.queue.get())


@muse_play.autocomplete("query")
async def muse_play_autocomplete(_: discord.Interaction[MusicBot], current: str) -> list[app_commands.Choice[str]]:
    if not current:
        return []
    tracks: wavelink.Search = await wavelink.Playable.search(current)
    return [app_commands.Choice(name=track.title, value=track.uri or track.title) for track in tracks][:25]


@app_commands.command(name="pause")
@app_commands.guild_only()
@is_in_bot_vc()
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
@is_in_bot_vc()
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
@is_in_bot_vc()
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


class MuseQueueGroup(app_commands.Group):
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

            view = MusicQueueView(itx.user.id, list(vc.queue), per=10)
            queue_embeds.append(view.get_first_page())
            await itx.response.send_message(embeds=queue_embeds, view=view)
            view.message = await itx.original_response()

    @app_commands.command(name="remove")
    @is_in_bot_vc()
    async def queue_remove(self, itx: discord.Interaction[MusicBot], entry: int) -> None:
        """Remove a track from the queue by position.

        Parameters
        ----------
        itx: discord.Interaction
            The interaction that triggered this command.
        entry: int
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
                vc.queue.delete(entry - 1)
                await itx.response.send_message(f"Removed {entry} from the queue.")
        else:
            await itx.response.send_message("No player to perform this on.")

    @app_commands.command(name="clear")
    @is_in_bot_vc()
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
@is_in_bot_vc()
async def muse_move(itx: discord.Interaction[MusicBot], before: int, after: int) -> None:
    """Move a track from one spot to another within the queue.

    Parameters
    ----------
    itx: discord.Interaction
        The interaction that triggered this command.
    before: int
        The index of the track you want moved.
    after: int
        The index you want to move it to.
    """

    # Known at runtime.
    assert itx.guild
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        try:
            temp = vc.queue[before - 1]
            del vc.queue[before - 1]
            vc.queue.put_at(after - 1, temp)
        except IndexError:
            await itx.response.send_message("Please enter valid queue indices.")
        else:
            await itx.response.send_message(f"Successfully moved the track at {before} to {after} in the queue.")
    else:
        await itx.response.send_message("No player to perform this on.")


@app_commands.command(name="skip")
@app_commands.guild_only()
@is_in_bot_vc()
async def muse_skip(itx: discord.Interaction[MusicBot], index: int = 1) -> None:
    """Skip to the numbered track in the queue. If no number is given, skip to the next track.

    Parameters
    ----------
    itx: discord.Interaction
        The interaction that triggered this command.
    index: int
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

        if index <= 0 or index > len(vc.queue):
            await itx.response.send_message("Please enter a valid queue index; the given one is too big or too small.")
            return

        for _ in range(index):
            await vc.skip()

        await itx.response.send_message(f"Skipped to the track at position {index}")
    else:
        await itx.response.send_message("No player to perform this on.")


@app_commands.command(name="shuffle")
@app_commands.guild_only()
@is_in_bot_vc()
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
@is_in_bot_vc()
async def muse_loop(
    itx: discord.Interaction[MusicBot],
    loop: Literal["All Tracks", "Current Track", "Off"] = "Off",
) -> None:
    """Loop the current track(s).

    Parameters
    ----------
    itx: discord.Interaction
        The interaction that triggered this command.
    loop: Literal["All Tracks", "Current Track", "Off"]
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
@is_in_bot_vc()
async def muse_seek(itx: discord.Interaction[MusicBot], position: ShortTime) -> None:
    """Seek to a particular position in the current track, provided with a `hours:minutes:seconds` string.

    Parameters
    ----------
    itx: discord.Interaction
        The interaction that triggered this command.
    position: str
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
@is_in_bot_vc()
async def muse_volume(itx: discord.Interaction[MusicBot], volume: int | None = None) -> None:
    """Show the player's volume. If given a number, you can change it as well, with 1000 as the limit.

    Parameters
    ----------
    itx: discord.Interaction
        The interaction that triggered this command.
    volume: int, optional
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


@app_commands.command(name="export")
@app_commands.guild_only()
@is_in_bot_vc()
async def muse_export(itx: discord.Interaction[MusicBot]) -> None:
    """Export the current queue to a file. Can be re-imported later to recreate the queue."""

    # Known at runtime.
    assert itx.guild
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        raw_data = [track.raw_data for track in vc.queue]
        data_buffer = BytesIO(json.dumps(raw_data).encode())
        file = discord.File(
            data_buffer,
            filename=f"music_queue_export_{discord.utils.utcnow(): %Y-%m-%d_%H-%M}.json",
            description="The exported music queue information.",
            spoiler=True,
        )
        await itx.response.send_message("Exported current queue to file:", file=file)
    else:
        await itx.response.send_message("No player to perform this on.")


@app_commands.command(name="import")
@app_commands.guild_only()
@ensure_voice_hook
async def muse_import(itx: discord.Interaction[MusicBot], import_file: discord.Attachment) -> None:
    """Import a file with track information to recreate a music queue. May be created with /export.

    Parameters
    ----------
    itx: discord.Interaction
        The interaction that triggered this command.
    import_file: discord.Attachment
        A JSON file with track information to recreate the queue with. May be created by /export.
    """

    # Known at runtime.
    assert itx.guild
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        # Depending on the size of the file, this might take some time.
        await itx.response.defer()

        filename = import_file.filename
        if not filename.endswith(".json"):
            await itx.followup.send("Bad input: Given file must end with .json.")
            return

        raw_data = await import_file.read()
        loaded_data = json.loads(raw_data)
        converted_tracks = [wavelink.Playable(data) for data in loaded_data]

        # Set up the queue now.
        vc.queue.clear()
        vc.queue.put(converted_tracks)

        await itx.followup.send(f"Imported track information from `{filename}`. Starting queue now.")
        if not vc.playing:
            await vc.play(vc.queue.get())
    else:
        await itx.response.send_message("No player to perform this on.")


@muse_import.error  # pyright: ignore [reportUnknownMemberType] # Bug in discord.py.
async def muse_import_error(itx: discord.Interaction[MusicBot], error: discord.app_commands.AppCommandError) -> None:
    """Error handle for /import. Provides better error messages for users."""

    actual_error = error.__cause__ or error

    if isinstance(actual_error, discord.HTTPException):
        error_text = f"Bad input: {actual_error.text}"
    elif isinstance(actual_error, json.JSONDecodeError):
        error_text = "Bad input: Given attachment is formatted incorrectly."
    else:
        error_text = "Error: Failed to import attachment."

    if not itx.response.is_done():
        await itx.response.send_message(error_text)
    else:
        await itx.followup.send(error_text)


@app_commands.command(name="help")
async def _help(itx: discord.Interaction[MusicBot], ephemeral: bool = True) -> None:
    """See a brief overview of all the bot's available commands.

    Parameters
    ----------
    itx: discord.Interaction
        The interaction that triggered this command.
    ephemeral: bool, default=True
        Whether the output should be visible to only you. Defaults to True.
    """

    help_embed = discord.Embed(title="Help")

    for cmd in itx.client.tree.walk_commands():
        if isinstance(cmd, app_commands.Command):
            mention = await itx.client.tree.find_mention_for(cmd)
            description = cmd.callback.__doc__ or cmd.description
        else:
            mention = f"/{cmd.name}"
            description = cmd.__doc__ or cmd.description

        try:
            index = description.index("Parameters")
        except ValueError:
            pass
        else:
            description = description[:index]

        help_embed.add_field(name=mention, value=description, inline=False)

    await itx.response.send_message(embed=help_embed, ephemeral=ephemeral)


APP_COMMANDS: list[app_commands.Command[Any, ..., None] | app_commands.Group] = [
    muse_connect,
    muse_play,
    muse_pause,
    muse_resume,
    muse_stop,
    muse_current,
    MuseQueueGroup(),
    muse_move,
    muse_skip,
    muse_shuffle,
    muse_loop,
    muse_seek,
    muse_volume,
    _help,
    muse_export,
    muse_import,
]
