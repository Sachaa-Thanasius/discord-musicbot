from __future__ import annotations

import argparse
import asyncio
import functools
import getpass
import json
import logging
import os
from collections.abc import Callable, Coroutine
from datetime import timedelta
from pathlib import Path
from typing import Any, Concatenate, Literal, NamedTuple, ParamSpec, Self, TypeVar

import base2048
import discord
import platformdirs
import wavelink
import xxhash
from discord import app_commands


try:
    import uvloop  # type: ignore
except ModuleNotFoundError:
    uvloop = None


P = ParamSpec("P")
T = TypeVar("T")
UnboundCommandCallback = Callable[Concatenate[discord.Interaction[Any], P], Coroutine[Any, Any, T]]

# Set up logging.
discord.utils.setup_logging()
log = logging.getLogger(__name__)

platformdir_info = platformdirs.PlatformDirs("discord-musicbot", "Sachaa-Thanasius", roaming=False)
escape_markdown = functools.partial(discord.utils.escape_markdown, as_needed=True)

MUSIC_EMOJIS: dict[str, str] = {
    "youtube": "<:youtube:1108460195270631537>",
    "youtubemusic": "<:youtubemusic:954046930713985074>",
    "soundcloud": "<:soundcloud:1147265178505846804>",
    "spotify": "<:spotify:1108458132826501140>",
}


class MusicBotError(Exception):
    """Marker exception for all errors specific to this music bot."""

    def __init__(self, message: str, *args: object) -> None:
        self.message = message
        super().__init__(*args)


class NotInVoiceChannel(MusicBotError, app_commands.CheckFailure):
    """Exception raised when the message author is not in a voice channel if that is necessary to do something.

    This inherits from :exc:`app_commands.CheckFailure`.
    """

    def __init__(self, *args: object) -> None:
        message = "You are not connected to a voice channel."
        super().__init__(message, *args)


class NotInBotVoiceChannel(MusicBotError, app_commands.CheckFailure):
    """Exception raised when the message author is not in the same voice channel as the bot in a context's guild.

    This inherits from :exc:`app_commands.CheckFailure`.
    """

    def __init__(self, *args: object) -> None:
        message = "You are not connected to the same voice channel as the bot."
        super().__init__(message, *args)


class InvalidShortTimeFormat(MusicBotError):
    """Exception raised when a given input does not match the short time format needed as a command parameter.

    This inherits from :exc:`app_commands.TransformerError`.
    """

    def __init__(self, value: str, *args: object) -> None:
        message = f"Failed to convert {value}. Make sure you're using the `<hours>:<minutes>:<seconds>` format."
        super().__init__(message, *args)


class WavelinkSearchError(MusicBotError):
    """Exception raised when a wavelink search fails to find any tracks.

    This inherits from :exc:`app_commands.AppCommandError`.
    """

    def __init__(self, value: str, *args: object) -> None:
        message = f"Failed to find any tracks matching that query: {value}."
        super().__init__(message, *args)


def resolve_path_with_links(path: Path, folder: bool = False) -> Path:
    """Resolve a path strictly with more secure default permissions, creating the path if necessary.

    Python only resolves with strict=True if the path exists.

    Source: https://github.com/mikeshardmind/discord-rolebot/blob/4374149bc75d5a0768d219101b4dc7bff3b9e38e/rolebot.py#L350
    """

    try:
        return path.resolve(strict=True)
    except FileNotFoundError:
        path = resolve_path_with_links(path.parent, folder=True) / path.name
        if folder:
            path.mkdir(mode=0o700)  # python's default is world read/write/traversable... (0o777)
        else:
            path.touch(mode=0o600)  # python's default is world read/writable... (0o666)
        return path.resolve(strict=True)


def create_track_embed(title: str, track: wavelink.Playable) -> discord.Embed:
    """Modify an embed to show information about a Wavelink track."""

    icon = MUSIC_EMOJIS.get(track.source, "\N{MUSICAL NOTE}")
    title = f"{icon} {title}"
    uri = track.uri or ""
    author = escape_markdown(track.author)
    track_title = escape_markdown(track.title)

    try:
        end_time = timedelta(seconds=track.length // 1000)
    except OverflowError:
        end_time = "\N{INFINITY}"

    description = f"[{track_title}]({uri})\n{author}\n`[0:00-{end_time}]`"

    embed = discord.Embed(color=0x0389DA, title=title, description=description)

    if track.artwork:
        embed.set_thumbnail(url=track.artwork)

    if track.album.name:
        embed.add_field(name="Album", value=track.album.name)

    return embed


def generate_tracks_add_notification(tracks: wavelink.Playable | wavelink.Playlist | list[wavelink.Playable]) -> str:
    """Return the appropriate notification string for tracks being added to a queue.

    This accounts for the tracks being indvidual, in a list, or in async iterator format — no others.
    """

    if isinstance(tracks, wavelink.Playlist):
        return f"Added {len(tracks.tracks)} tracks from the `{tracks.name}` playlist to the queue."
    if isinstance(tracks, list) and (len(tracks)) > 1:
        return f"Added `{len(tracks)}` tracks to the queue."
    if isinstance(tracks, list):
        return f"Added `{tracks[0].title}` to the queue."

    return f"Added `{tracks.title}` to the queue."


def assign_requester(
    item: wavelink.Playable | wavelink.Playlist | list[wavelink.Playable],
    requester: str | None = None,
) -> None:
    """Assign requesters to a track or collection of tracks.

    Parameters
    ----------
    item : :class:`AnyPlayable` | :class:`AnyTrackIterable`
        The track or collection of tracks to add to the queue.
    requester : :class:`str`, optional
        A string representing the user who queued this up. Optional.
    """

    if requester is not None:
        if isinstance(item, wavelink.Playable):
            item.requester = requester  # type: ignore # Runtime attribute assignment.
        elif isinstance(item, wavelink.Playlist):
            item.track_extras(requester=requester)
        else:
            for subitem in item:
                subitem.requester = requester  # type: ignore # Runtime attribute assignment.


def ensure_voice_hook(func: UnboundCommandCallback[P, T]) -> UnboundCommandCallback[P, T]:
    """A makeshift pre-command hook, ensuring that a voice client automatically connects the right channel.

    This is currently only used for /muse_play.

    Raises
    ------
    NotInVoiceChannel
        The user isn't currently connected to a voice channel.
    """

    @functools.wraps(func)
    async def callback(itx: discord.Interaction[MusicBot], *args: P.args, **kwargs: P.kwargs) -> T:
        # Known at runtime in guild-only situation.
        assert itx.guild and isinstance(itx.user, discord.Member)
        vc = itx.guild.voice_client
        assert isinstance(vc, MusicPlayer | None)

        # For consistency in itx.followup usage within functions decorated with this.
        await itx.response.defer()

        if vc is None:
            if itx.user.voice:
                # Not sure in what circumstances a member would have a voice state without being in a valid channel.
                assert itx.user.voice.channel
                await itx.user.voice.channel.connect(cls=MusicPlayer)
            else:
                raise NotInVoiceChannel
        return await func(itx, *args, **kwargs)

    return callback


def in_bot_vc(itx: discord.Interaction[MusicBot]) -> bool:
    """A slash command check that checks if the person invoking this command is in
    the same voice channel as the bot within a guild.

    Raises
    ------
    app_commands.NoPrivateMessage
        This command cannot be run outside of a guild context.
    NotInBotVoiceChannel
        Derived from :exc:`app_commands.CheckFailure`. The user invoking this command isn't in the same
        channel as the bot.
    """

    if not itx.guild or not isinstance(itx.user, discord.Member):
        raise app_commands.NoPrivateMessage

    vc = itx.guild.voice_client

    if not (
        itx.user.guild_permissions.administrator or (vc and itx.user.voice and (itx.user.voice.channel == vc.channel))
    ):
        raise NotInBotVoiceChannel
    return True


class LavalinkCreds(NamedTuple):
    """Credentials for the Lavalink node this bot is connecting to."""

    uri: str
    password: str


class ShortTime(NamedTuple):
    """A tuple meant to hold the string representation of a time and the total number of seconds it represents."""

    original: str
    seconds: int

    @classmethod
    async def transform(cls: type[Self], _: discord.Interaction, position_str: str, /) -> Self:
        try:
            zipped_time_segments = zip((1, 60, 3600, 86400), reversed(position_str.split(":")), strict=False)
            position_seconds = int(sum(x * float(t) for x, t in zipped_time_segments) * 1000)
        except ValueError:
            raise InvalidShortTimeFormat(position_str) from None
        else:
            return cls(position_str, position_seconds)


class WavelinkSearchTransformer(app_commands.Transformer):
    """Transforms command argument to a wavelink track or collection of tracks."""

    async def transform(self, _: discord.Interaction, value: str, /) -> wavelink.Playable | wavelink.Playlist:
        tracks: wavelink.Search = await wavelink.Playable.search(value)
        if not tracks:
            raise WavelinkSearchError(value, discord.AppCommandOptionType.string, self)
        return tracks if isinstance(tracks, wavelink.Playlist) else tracks[0]

    async def autocomplete(self, _: discord.Interaction, value: str) -> list[app_commands.Choice[str]]:  # type: ignore # Narrowing.
        tracks: wavelink.Search = await wavelink.Playable.search(value)
        return [app_commands.Choice(name=track.title, value=track.uri or track.title) for track in tracks][:25]


class MusicQueue(wavelink.Queue):
    """A version of :class:`wavelink.Queue` with extra operations."""

    def put_at(self, index: int, item: wavelink.Playable, /) -> None:
        if index >= len(self._queue) or index < 0:
            msg = "The index is out of range."
            raise IndexError(msg)
        self._queue.rotate(-index)
        self._queue.appendleft(item)
        self._queue.rotate(index)

    def skip_to(self, index: int, /) -> None:
        if index >= len(self._queue) or index < 0:
            msg = "The index is out of range."
            raise IndexError(msg)
        for _ in range(index - 1):
            self.get()

    def swap(self, first: int, second: int, /) -> None:
        if first >= len(self._queue) or second >= len(self._queue):
            msg = "One of the given indices is out of range."
            raise IndexError(msg)
        if first == second:
            msg = "These are the same index; swapping will have no effect."
            raise IndexError(msg)
        self._queue.rotate(-first)
        first_item = self._queue[0]
        self._queue.rotate(first - second)
        second_item = self._queue.popleft()
        self._queue.appendleft(first_item)
        self._queue.rotate(second - first)
        self._queue.popleft()
        self._queue.appendleft(second_item)
        self._queue.rotate(first)

    def move(self, before: int, after: int, /) -> None:
        if before >= len(self._queue) or after >= len(self._queue):
            msg = "One of the given indices is out of range."
            raise IndexError(msg)
        if before == after:
            msg = "These are the same index; swapping will have no effect."
            raise IndexError(msg)
        self._queue.rotate(-before)
        item = self._queue.popleft()
        self._queue.rotate(before - after)
        self._queue.appendleft(item)
        self._queue.rotate(after)


class MusicPlayer(wavelink.Player):
    """A version of :class:`wavelink.Player` with a different queue.

    Attributes
    ----------
    queue : :class:`MusicQueue`
        A version of :class:`wavelink.Queue` extra operations.
    """

    def __init__(
        self,
        client: discord.Client = discord.utils.MISSING,
        channel: discord.abc.Connectable = discord.utils.MISSING,
        *,
        nodes: list[wavelink.Node] | None = None,
    ) -> None:
        super().__init__(client, channel, nodes=nodes)
        self.autoplay = wavelink.AutoPlayMode.partial
        self.queue: MusicQueue = MusicQueue()  # type: ignore # overridden symbol

    async def move_to(self, channel: discord.abc.Snowflake | None) -> None:
        await self.channel.guild.change_voice_state(channel=channel)


class PageNumEntryModal(discord.ui.Modal):
    """A discord modal that allows users to enter a page number to jump to in the view that references this.

    Attributes
    ----------
    input_page_num : :class:`TextInput`
        A UI text input element to allow users to enter a page number.
    interaction : :class:`discord.Interaction`
        The interaction of the user with the modal.
    """

    input_page_num: discord.ui.TextInput[Self] = discord.ui.TextInput(
        label="Page",
        placeholder="Enter page number here...",
        required=True,
        min_length=1,
    )

    def __init__(self) -> None:
        super().__init__(title="Page Jump")
        self.interaction: discord.Interaction | None = None

    async def on_submit(self, interaction: discord.Interaction, /) -> None:
        """Performs validation on the input and saves the interaction for a later response."""

        self.interaction = interaction


class MusicQueueView(discord.ui.View):
    """A view that handles paginated embeds and page buttons for seeing the tracks in an embed-based music queue.

    Parameters
    ----------
    author_id : :class:`int`
        The Discord ID of the user that triggered this view. No one else can use it.
    pages_content : list[Any]
        The text content for every possible page.
    per : :class:`int`
        The number of entries to be displayed per page.
    timeout : :class:`float`, optional
        Timeout in seconds from last interaction with the UI before no longer accepting input.
        If ``None`` then there is no timeout.

    Attributes
    ----------
    message : :class:`discord.Message`
        The message to which the view is attached to, allowing interaction without a :class:`discord.Interaction`.
    author_id : :class:`int`
        The Discord ID of the user that triggered this view. No one else can use it.
    per_page : :class:`int`
        The number of entries to be displayed per page.
    pages : list[str]
        A list of content for pages, split according to how much content is wanted per page.
    page_index : :class:`int`
        The index for the current page.
    total_pages
    """

    message: discord.Message

    def __init__(self, author_id: int, pages_content: list[str], per: int = 1, *, timeout: float | None = 180) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.per_page = per
        self.pages = [pages_content[i : (i + per)] for i in range(0, len(pages_content), per)]
        self.page_index: int = 1

        # Have the right buttons visible and enabled on instantiation.
        self.clear_items()
        self.add_page_buttons()
        self.disable_page_buttons()

    @property
    def total_pages(self) -> int:
        """:class:``int`: The total number of pages."""

        return len(self.pages)

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        """Ensures that the user interacting with the view was the one who instantiated it."""

        check = self.author_id == interaction.user.id
        if not check:
            await interaction.response.send_message("You cannot interact with this view.", ephemeral=True)
        return check

    async def on_timeout(self) -> None:
        """Disables all buttons when the view times out."""

        self.clear_items()
        await self.message.edit(view=self)
        self.stop()

    def add_page_buttons(self) -> None:
        """Only adds the necessary page buttons based on how many pages there are."""

        # Done this way to preserve button order.
        if self.total_pages > 2:
            self.add_item(self.turn_to_first)
        if self.total_pages > 1:
            self.add_item(self.turn_to_previous)
        if self.total_pages > 2:
            self.add_item(self.enter_page)
        if self.total_pages > 1:
            self.add_item(self.turn_to_next)
        if self.total_pages > 2:
            self.add_item(self.turn_to_last)

        self.add_item(self.quit_view)

    def disable_page_buttons(self) -> None:
        """Enables and disables page-turning buttons based on page count, position, and movement."""

        # Disable buttons based on the total number of pages.
        if self.total_pages <= 1:
            for button in (
                self.turn_to_first,
                self.turn_to_next,
                self.turn_to_previous,
                self.turn_to_last,
                self.enter_page,
            ):
                button.disabled = True
            return

        self.enter_page.disabled = False

        # Disable buttons based on the current page.
        self.turn_to_previous.disabled = self.turn_to_first.disabled = self.page_index == 1
        self.turn_to_next.disabled = self.turn_to_last.disabled = self.page_index == self.total_pages

    def format_page(self) -> discord.Embed:
        """Makes the embed 'page' that the user will see."""

        embed_page = discord.Embed(color=0x149CDF, title="Music Queue")

        if self.total_pages == 0:
            embed_page.description = "The queue is empty."
            embed_page.set_footer(text="Page 0/0")
        else:
            # Expected page size of 10
            content = self.pages[self.page_index - 1]
            organized = (f"{(i + 1) + (self.page_index - 1) * 10}. {track}" for i, track in enumerate(content))
            embed_page.description = "\n".join(organized)
            embed_page.set_footer(text=f"Page {self.page_index}/{self.total_pages}")

        return embed_page

    def get_first_page(self) -> discord.Embed:
        """Get the embed of the first page."""

        temp = self.page_index
        self.page_index = 1
        embed = self.format_page()
        self.page_index = temp
        return embed

    async def update_page(self, interaction: discord.Interaction, new_page: int) -> None:
        """Update and display the view for the given page."""

        self.page_index = new_page
        embed_page = self.format_page()
        self.disable_page_buttons()
        await interaction.response.edit_message(embed=embed_page, view=self)

    @discord.ui.button(label="\N{MUCH LESS-THAN}", style=discord.ButtonStyle.blurple, disabled=True)
    async def turn_to_first(self, interaction: discord.Interaction, _: discord.ui.Button[Self]) -> None:
        """Skips to the first page of the view."""

        await self.update_page(interaction, 1)

    @discord.ui.button(label="<", style=discord.ButtonStyle.blurple, disabled=True, custom_id="page_view:prev")
    async def turn_to_previous(self, interaction: discord.Interaction, _: discord.ui.Button[Self]) -> None:
        """Turns to the previous page of the view."""

        await self.update_page(interaction, self.page_index - 1)

    @discord.ui.button(label="\N{BOOK}", style=discord.ButtonStyle.green, disabled=True, custom_id="page_view:enter")
    async def enter_page(self, interaction: discord.Interaction, _: discord.ui.Button[Self]) -> None:
        """Sends a modal that a user to enter their own page number into."""

        # Get page number from a modal.
        modal = PageNumEntryModal()
        await interaction.response.send_modal(modal)
        modal_timed_out = await modal.wait()

        if modal_timed_out or self.is_finished():
            return

        # Validate the input.
        try:
            temp_new_page = int(modal.input_page_num.value)
        except ValueError:
            return

        if temp_new_page > self.total_pages or temp_new_page < 1 or self.page_index == temp_new_page:
            return

        if modal.interaction:
            await self.update_page(modal.interaction, temp_new_page)

    @discord.ui.button(label=">", style=discord.ButtonStyle.blurple)
    async def turn_to_next(self, interaction: discord.Interaction, _: discord.ui.Button[Self]) -> None:
        """Turns to the next page of the view."""

        await self.update_page(interaction, self.page_index + 1)

    @discord.ui.button(label="\N{MUCH GREATER-THAN}", style=discord.ButtonStyle.blurple)
    async def turn_to_last(self, interaction: discord.Interaction, _: discord.ui.Button[Self]) -> None:
        """Skips to the last page of the view."""

        await self.update_page(interaction, self.total_pages)

    @discord.ui.button(label="\N{MULTIPLICATION X}", style=discord.ButtonStyle.red)
    async def quit_view(self, interaction: discord.Interaction, _: discord.ui.Button[Self]) -> None:
        """Deletes the original message with the view after a slight delay."""

        self.stop()
        await interaction.response.defer()
        await asyncio.sleep(0.25)
        await interaction.delete_original_response()


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

    assign_requester(search, itx.user.mention)
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


muse_queue = app_commands.Group(
    name="queue",
    description="Music queue-related commands. Use `play` to add things to the queue.",
    guild_only=True,
)


@muse_queue.command(name="get")
async def queue_get(itx: discord.Interaction[MusicBot]) -> None:
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


@muse_queue.command(name="remove")
@app_commands.check(in_bot_vc)
async def queue_remove(itx: discord.Interaction[MusicBot], entry: int) -> None:
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


@muse_queue.command(name="clear")
@app_commands.check(in_bot_vc)
async def queue_clear(itx: discord.Interaction[MusicBot]) -> None:
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
    muse_queue,
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

        player: wavelink.Player | None = payload.player
        if not player:
            return

        current_embed = create_track_embed("Now Playing", payload.track)
        await player.channel.send(embed=current_embed)


def _get_stored_credentials(filename: str) -> tuple[str, ...] | None:
    secret_file_path = platformdir_info.user_config_path / filename
    secret_file_path = resolve_path_with_links(secret_file_path)
    with secret_file_path.open("r", encoding="utf-8") as fp:
        return tuple(base2048.decode(line.removesuffix("\n")).decode("utf-8") for line in fp.readlines())


def _store_credentials(filename: str, *credentials: str) -> None:
    secret_file_path = platformdir_info.user_config_path / filename
    secret_file_path = resolve_path_with_links(secret_file_path)
    with secret_file_path.open("w", encoding="utf-8") as fp:
        for cred in credentials:
            fp.write(base2048.encode(cred.encode()))
            fp.write("\n")


def _input_token() -> None:
    prompt = "Paste your discord token (won't be visible), then press enter. It will be stored for later use."
    token = getpass.getpass(prompt)
    if not token:
        msg = "Not storing empty token."
        raise RuntimeError(msg)
    _store_credentials("musicbot.token", token)


def _input_lavalink_creds() -> None:
    prompts = (
        "Paste your Lavalink node URI (won't be visible), then press enter. It will be stored for later use.",
        "Paste your Lavalink node password (won't be visible), then press enter. It will be stored for later use.",
    )
    creds: list[str] = []
    for prompt in prompts:
        secret = getpass.getpass(prompt)
        if not secret:
            msg = "Not storing empty lavalink cred."
            raise RuntimeError(msg)
        creds.append(secret)
    _store_credentials("musicbot_lavalink.secrets", *creds)


def _get_token() -> str:
    token = os.getenv("DISCORD_TOKEN") or _get_stored_credentials("musicbot.token")
    if token is None:
        msg = (
            "You're missing a Discord bot token. Use '--token' in the CLI to trigger setup for it, or provide an "
            "environmental variable labelled 'DISCORD_TOKEN'."
        )
        raise RuntimeError(msg)
    return token[0] if isinstance(token, tuple) else token


def _get_lavalink_creds() -> LavalinkCreds:
    if (ll_uri := os.getenv("LAVALINK_URI")) and (ll_pwd := os.getenv("LAVALINK_PASSWORD")):
        lavalink_creds = LavalinkCreds(ll_uri, ll_pwd)
    elif ll_creds := _get_stored_credentials("musicbot_lavalink.secrets"):
        lavalink_creds = LavalinkCreds(ll_creds[0], ll_creds[1])
    else:
        msg = (
            "You're missing Lavalink node credentials. Use '--lavalink' in the CLI to trigger setup for it, or provide "
            "environmental variables labelled 'LAVALINK_URI' and 'LAVALINK_PASSWORD'."
        )
        raise RuntimeError(msg)
    return lavalink_creds


def run_client() -> None:
    """Confirm existence of required credentials and launch the radio bot."""

    async def bot_runner(client: MusicBot) -> None:
        async with client:
            await client.start(token, reconnect=True)

    token = _get_token()
    lavalink_creds = _get_lavalink_creds()

    client = MusicBot(lavalink_creds)

    loop = uvloop.new_event_loop if (uvloop is not None) else None  # type: ignore
    with asyncio.Runner(loop_factory=loop) as runner:  # type: ignore
        runner.run(bot_runner(client))


def main() -> None:
    parser = argparse.ArgumentParser(description="A minimal configuration discord bot for server radios.")
    setup_group = parser.add_argument_group(
        "setup",
        description="Choose credentials to specify. Discord token and Lavalink credentials are required on first run.",
    )
    setup_group.add_argument(
        "--token",
        action="store_true",
        help="Whether to specify the Discord token. Initiates interactive setup.",
        dest="specify_token",
    )
    setup_group.add_argument(
        "--lavalink",
        action="store_true",
        help="Whether you want to specify the Lavalink node URI.",
        dest="specify_lavalink",
    )

    args = parser.parse_args()

    if args.specify_token:
        _input_token()
    if args.specify_lavalink:
        _input_lavalink_creds()

    run_client()


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
