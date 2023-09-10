"""Heavily inspired by @mikeshardmind's one-file bots, which may explain if this looks familiar."""

from __future__ import annotations

import argparse
import asyncio
import functools
import getpass
import json
import logging
import os
from collections.abc import AsyncIterator, Callable, Coroutine, Iterable
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Concatenate, Literal, ParamSpec, TypeAlias, TypeVar, cast

import base2048
import discord
import platformdirs
import wavelink
import xxhash
import yarl
from discord import app_commands
from wavelink.ext import spotify


try:
    import uvloop  # type: ignore
except ModuleNotFoundError:
    uvloop = None


if TYPE_CHECKING:
    from typing_extensions import Self
else:
    Self = Any

P = ParamSpec("P")
T = TypeVar("T")
Coro = Coroutine[Any, Any, T]
UnboundCommandCallback = Callable[Concatenate[discord.Interaction[Any], P], Coro[T]]

AnyTrack: TypeAlias = wavelink.Playable | spotify.SpotifyTrack
AnyTrackIterable: TypeAlias = list[wavelink.Playable] | list[spotify.SpotifyTrack] | spotify.SpotifyAsyncIterator


# Set up logging.
discord.utils.setup_logging()
log = logging.getLogger(__name__)

platformdir_info = platformdirs.PlatformDirs("discord-musicbot", "Sachaa-Thanasius", roaming=False)
escape_markdown = functools.partial(discord.utils.escape_markdown, as_needed=True)

MUSIC_EMOJIS: dict[type[AnyTrack], str] = {
    wavelink.YouTubeTrack: "<:youtube:1108460195270631537>",
    wavelink.YouTubeMusicTrack: "<:youtubemusic:954046930713985074>",
    wavelink.SoundCloudTrack: "<:soundcloud:1147265178505846804>",
    spotify.SpotifyTrack: "<:spotify:1108458132826501140>",
}


class NotInBotVoiceChannel(app_commands.CheckFailure):
    """Exception raised when the message author is not in the same voice channel as the bot in a context's guild.

    This inherits from :exc:`app_commands.CheckFailure`.
    """


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


async def format_track_embed(title: str, track: AnyTrack) -> discord.Embed:
    """Modify an embed to show information about a Wavelink track."""

    icon = MUSIC_EMOJIS.get(type(track), "\N{MUSICAL NOTE}")
    title = f"{icon} {title}"
    description_template = "[{0}]({1})\n{2}\n`[0:00-{3}]`"

    try:
        end_time = timedelta(seconds=track.duration // 1000)
    except OverflowError:
        end_time = "\N{INFINITY}"

    if isinstance(track, wavelink.Playable):
        uri = track.uri or ""
        author = escape_markdown(track.author or "")
    else:
        uri = f"https://open.spotify.com/track/{track.uri.rpartition(':')[2]}"
        author = escape_markdown(", ".join(track.artists))

    track_title = escape_markdown(track.title)
    description = description_template.format(track_title, uri, author, end_time)

    if requester := getattr(track, "requester", None):
        description += f"\n\nRequested by: {requester}"

    embed = discord.Embed(color=0x0389DA, title=title, description=description)

    if isinstance(track, wavelink.YouTubeTrack):
        thumbnail = await track.fetch_thumbnail()
        embed.set_thumbnail(url=thumbnail)

    return embed


async def generate_tracks_add_notification(tracks: AnyTrack | AnyTrackIterable) -> str:
    """Return the appropriate notification string for tracks being added to a queue.

    This accounts for the tracks being indvidual, in a list, or in async iterator format — no others.
    """

    if isinstance(tracks, wavelink.YouTubePlaylist | wavelink.SoundCloudPlaylist):
        return f"Added {len(tracks.tracks)} tracks from the `{tracks.name}` playlist to the queue."
    if isinstance(tracks, list) and (length := len(tracks)) > 1:
        return f"Added `{length}` tracks to the queue."
    if isinstance(tracks, list):
        return f"Added `{tracks[0].title}` to the queue."
    if isinstance(tracks, spotify.SpotifyAsyncIterator):
        return f"Added `{tracks._count}` tracks to the queue."  # type: ignore # Can't iterate for count.

    return f"Added `{tracks.title}` to the queue."


def ensure_voice(func: UnboundCommandCallback[P, T]) -> UnboundCommandCallback[P, T]:
    """A pre-slash command hook, ensuring that a voice client automatically connects the right channel."""

    @functools.wraps(func)
    async def callback(itx: discord.Interaction[MusicBot], *args: P.args, **kwargs: P.kwargs) -> T:
        # Known at runtime in guild-only situation.
        assert itx.guild and isinstance(itx.user, discord.Member)
        vc = itx.guild.voice_client
        assert isinstance(vc, MusicPlayer | None)

        # For consistency in itxn.followup usage within functions decorated with this.
        await itx.response.defer()

        if vc is None:
            if itx.user.voice:
                # Not sure in what circumstances a member would have a voice state without being in a valid channel.
                assert itx.user.voice.channel
                await itx.user.voice.channel.connect(cls=MusicPlayer)  # type: ignore # Valid VoiceProtocol subclass.
            else:
                await itx.followup.send("You are not connected to a voice channel.")
                msg = "User not connected to a voice channel."
                raise app_commands.AppCommandError(msg)
        return await func(itx, *args, **kwargs)

    return callback


async def in_bot_vc(itx: discord.Interaction[MusicBot]) -> bool:
    """A :func:`.check` that checks if the person invoking this command is in
    the same voice channel as the bot within a guild.

    This check raises a special exception, :exc:`NotInBotVoiceChannel` that is derived
    from :exc:`commands.CheckFailure`.
    """

    if not itx.guild or not isinstance(itx.user, discord.Member):
        raise app_commands.NoPrivateMessage

    vc = itx.guild.voice_client

    if not (
        itx.user.guild_permissions.administrator or (vc and itx.user.voice and (itx.user.voice.channel == vc.channel))
    ):
        msg = "You are not connected to the same voice channel as the bot."
        raise NotInBotVoiceChannel(msg)
    return True


class WavelinkSearchTransformer(app_commands.Transformer):
    """Converts to what Wavelink considers a playable track (:class:`AnyPlayable` or :class:`AnyTrackIterable`).

    The lookup strategy is as follows (in order):

    1. Lookup by :class:`wavelink.YouTubeTrack` if the argument has no url "scheme".
    2. Lookup by first valid wavelink track class if the argument matches the search/url format.
    3. Lookup by assuming argument to be a direct url or local file address.
    """

    @staticmethod
    def _get_search_type(argument: str) -> type[AnyTrack]:
        """Get the searchable wavelink class that matches the argument string closest."""

        check = yarl.URL(argument)

        if (
            (not check.host and not check.scheme)
            or (check.host in ("youtube.com", "www.youtube.com", "m.youtube.com") and "v" in check.query)
            or check.scheme == "ytsearch"
        ):
            search_type = wavelink.YouTubeTrack
        elif (
            check.host in ("youtube.com", "www.youtube.com", "m.youtube.com") and "list" in check.query
        ) or check.scheme == "ytpl":
            search_type = wavelink.YouTubePlaylist
        elif check.host == "music.youtube.com" or check.scheme == "ytmsearch":
            search_type = wavelink.YouTubeMusicTrack
        elif check.host in ("soundcloud.com", "www.soundcloud.com") and "sets" in check.parts:
            search_type = wavelink.SoundCloudPlaylist
        elif check.host in ("soundcloud.com", "www.soundcloud.com") or check.scheme == "scsearch":
            search_type = wavelink.SoundCloudTrack
        elif check.host in ("spotify.com", "open.spotify.com"):
            search_type = spotify.SpotifyTrack
        else:
            search_type = wavelink.GenericTrack

        return search_type

    async def _convert(self, argument: str) -> AnyTrack | AnyTrackIterable:
        """Attempt to convert a string into a Wavelink track or list of tracks."""

        search_type = self._get_search_type(argument)
        if issubclass(search_type, spotify.SpotifyTrack):
            try:
                tracks = search_type.iterator(query=argument)
            except TypeError:
                tracks = await search_type.search(argument)
        else:
            tracks = await search_type.search(argument)

        if not tracks:
            msg = f"Your search query `{argument}` returned no tracks."
            raise wavelink.NoTracksError(msg)

        # Still technically possible for tracks to be a Playlist subclass now.
        if issubclass(search_type, wavelink.Playable) and isinstance(tracks, list):
            tracks = tracks[0]

        return tracks

    async def transform(self, _: discord.Interaction, value: str, /) -> AnyTrack | AnyTrackIterable:
        return await self._convert(value)

    async def autocomplete(  # type: ignore # Narrowing input and return types to str.
        self,
        _: discord.Interaction,
        value: str,
    ) -> list[app_commands.Choice[str]]:
        search_type = self._get_search_type(value)
        tracks = await search_type.search(value)
        return [app_commands.Choice(name=track.title, value=track.uri or track.title) for track in tracks][:25]


class MusicQueue(wavelink.Queue):
    """A version of :class:`wavelink.Queue` that can skip to a specific index."""

    def remove_before_index(self, index: int) -> None:
        """Remove all members from the queue before a certain index.

        Credit to Chillymosh for the implementation.
        """

        for _ in range(index):
            try:
                del self[0]
            except IndexError:
                break

    async def put_all_wait(self, item: AnyTrack | AnyTrackIterable, requester: str | None = None) -> None:
        """Put items individually or from an iterable into the queue asynchronously using await.

        This can include some playlist subclasses.

        Parameters
        ----------
        item : :class:`AnyPlayable` | :class:`AnyTrackIterable`
            The track or collection of tracks to add to the queue.
        requester : :class:`str`, optional
            A string representing the user who queued this up. Optional.
        """

        if isinstance(item, Iterable):
            for sub_item in item:
                sub_item.requester = requester  # type: ignore # Runtime attribute assignment.
                await self.put_wait(sub_item)
        elif isinstance(item, spotify.SpotifyAsyncIterator):
            # Awkward casting to satisfy pyright since wavelink isn't fully typed.
            async for sub_item in cast(AsyncIterator[spotify.SpotifyTrack], item):
                sub_item.requester = requester  # type: ignore # Runtime attribute assignment.
                await self.put_wait(sub_item)
        else:
            item.requester = requester  # type: ignore # Runtime attribute assignment.
            await self.put_wait(item)


class MusicPlayer(wavelink.Player):
    """A version of :class:`wavelink.Player` with a different queue.

    Attributes
    ----------
    queue : :class:`SkippableQueue`
        A version of :class:`wavelink.Queue` that can be skipped into.
    """

    def __init__(
        self,
        client: discord.Client = discord.utils.MISSING,
        channel: discord.VoiceChannel | discord.StageChannel = discord.utils.MISSING,
        *,
        nodes: list[wavelink.Node] | None = None,
        swap_node_on_disconnect: bool = True,
    ) -> None:
        super().__init__(client, channel, nodes=nodes, swap_node_on_disconnect=swap_node_on_disconnect)
        self.queue: MusicQueue = MusicQueue()


class PageNumEntryModal(discord.ui.Modal):
    """A discord modal that allows users to enter a page number to jump to in the view that references this.

    Parameters
    ----------
    page_limit : :class:`int`
        The maximum integer value of pages that can be entered.

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
    pages : list[Any]
        A list of content for pages, split according to how much content is wanted per page.
    page_index : :class:`int`
        The index for the current page.
    total_pages
    """

    message: discord.Message

    def __init__(self, author_id: int, pages_content: list[Any], per: int = 1, *, timeout: float | None = 180) -> None:
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
        self.stop()
        await self.message.edit(view=self)

    def add_page_buttons(self) -> None:
        """Only adds the necessary page buttons based on how many pages there are."""

        # No point recalculating these.
        more_than_2, more_than_1 = self.total_pages > 2, self.total_pages > 1

        # Done this way to preserve button order.
        if more_than_2:
            self.add_item(self.turn_to_first)
        if more_than_1:
            self.add_item(self.turn_to_previous)
        if more_than_2:
            self.add_item(self.enter_page)
        if more_than_1:
            self.add_item(self.turn_to_next)
        if more_than_2:
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

        # Disable buttons based on the page extremes.
        self.turn_to_previous.disabled = self.turn_to_first.disabled = (self.page_index == 1)
        self.turn_to_next.disabled = self.turn_to_last.disabled = (self.page_index == self.total_pages)

    def format_page(self) -> discord.Embed:
        """Makes the embed 'page' that the user will see."""

        embed_page = discord.Embed(color=0x149CDF, title="Music Queue")

        if self.total_pages == 0:
            embed_page.description = "The queue is empty."
            embed_page.set_footer(text="Page 0/0")
        else:
            # Expected page size of 10
            content = self.pages[self.page_index - 1]
            organized = (f"{(i + 1) + (self.page_index - 1) * 10}. {song}" for i, song in enumerate(content))
            embed_page.description = "\n".join(organized)
            embed_page.set_footer(text=f"Page {self.page_index}/{self.total_pages}")

        return embed_page

    def get_starting_embed(self) -> discord.Embed:
        """Get the embed of the first page."""

        self.page_index = 1
        return self.format_page()

    async def update_page(self, interaction: discord.Interaction, new_page: int) -> None:
        """Update and display the view for the given page."""

        self.page_index = new_page
        embed_page = self.format_page()  # Update the page embed.
        self.disable_page_buttons()  # Update the page buttons.
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


@app_commands.command()
@app_commands.guild_only()
async def muse_connect(itx: discord.Interaction[MusicBot]) -> None:
    """Join a voice channel."""

    # Known at runtime in guild-only command.
    assert itx.guild and isinstance(itx.user, discord.Member)
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc is not None and itx.user.voice is not None:
        if vc.channel != itx.user.voice.channel:
            if itx.user.guild_permissions.administrator:
                await vc.move_to(itx.user.voice.channel)  # type: ignore # Valid channel to move to.
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
        await itx.user.voice.channel.connect(cls=MusicPlayer)  # type: ignore # Valid VoiceProtocol subclass.
        await itx.response.send_message(f"Joined the {itx.user.voice.channel} channel.")


@app_commands.command()
@app_commands.guild_only()
@ensure_voice
async def muse_play(
    itx: discord.Interaction[MusicBot],
    *,
    search: app_commands.Transform[AnyTrack | AnyTrackIterable, WavelinkSearchTransformer],
) -> None:
    """Play audio from a YouTube url or search term.

    Parameters
    ----------
    itx : :class:`discord.Interaction`
        The invocation context.
    search : AnyTrack | AnyTrackIterable
        A track or list/async iterable of tracks.
    """

    # Known at runtime in guild-only situation.
    assert itx.guild and isinstance(itx.user, discord.Member)
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer)  # Ensured by this command's before_invoke.

    await vc.queue.put_all_wait(search, itx.user.mention)
    notif_text = await generate_tracks_add_notification(search)
    await itx.followup.send(notif_text)

    if not vc.is_playing():
        first_track = vc.queue.get()
        await vc.play(first_track)


@app_commands.command()
@app_commands.guild_only()
@app_commands.check(in_bot_vc)
async def muse_pause(itx: discord.Interaction[MusicBot]) -> None:
    """Pause the audio."""

    # Known at runtime in guild-only situation.
    assert itx.guild and isinstance(itx.user, discord.Member)
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        if vc.is_paused():
            await vc.resume()
            await itx.response.send_message("Resumed playback.")
        else:
            await vc.pause()
            await itx.response.send_message("Paused playback.")
    else:
        await itx.response.send_message("No player to perform this on.")


@app_commands.command()
@app_commands.guild_only()
@app_commands.check(in_bot_vc)
async def muse_resume(itx: discord.Interaction[MusicBot]) -> None:
    """Resume the audio if paused."""

    # Known at runtime in guild-only situation.
    assert itx.guild and isinstance(itx.user, discord.Member)
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        if vc.is_paused():
            await vc.resume()
            await itx.response.send_message("Resumed playback.")
    else:
        await itx.response.send_message("No player to perform this on.")


@app_commands.command()
@app_commands.guild_only()
@app_commands.check(in_bot_vc)
async def muse_stop(itx: discord.Interaction[MusicBot]) -> None:
    """Stop playback and disconnect the bot from voice."""

    # Known at runtime in guild-only situation.
    assert itx.guild and isinstance(itx.user, discord.Member)
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        await vc.disconnect()  # type: ignore # Incomplete wavelink typing.
        await itx.response.send_message("Disconnected from voice channel.")
    else:
        await itx.response.send_message("No player to perform this on.")


@app_commands.command()
@app_commands.guild_only()
async def muse_current(itx: discord.Interaction[MusicBot]) -> None:
    """Display the current track."""

    # Known at runtime in guild-only situation.
    assert itx.guild and isinstance(itx.user, discord.Member)
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc and vc.current:
        current_embed = await format_track_embed("Now Playing", vc.current)
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


@muse_queue.command()
async def queue_get(itx: discord.Interaction[MusicBot]) -> None:
    """Display everything in the queue."""

    # Known at runtime in guild-only situation.
    assert itx.guild and isinstance(itx.user, discord.Member)
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    queue_embeds: list[discord.Embed] = []
    if vc:
        if vc.current:
            current_embed = await format_track_embed("Now Playing", vc.current)
            queue_embeds.append(current_embed)

        view = MusicQueueView(
            author_id=itx.user.id,
            pages_content=[track.title for track in vc.queue],
            per=10,
        )
        queue_embeds.append(view.get_starting_embed())
        await itx.response.send_message(embeds=queue_embeds, view=view)
        view.message = await itx.original_response()


@muse_queue.command()
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

    # Known at runtime in guild-only situation.
    assert itx.guild and isinstance(itx.user, discord.Member)
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        if entry > vc.queue.count or entry < 1:
            await itx.response.send_message("That track does not exist and cannot be removed.")
        else:
            del vc.queue[entry - 1]
            await itx.response.send_message(f"Removed {entry} from the queue.")
    else:
        await itx.response.send_message("No player to perform this on.")


@muse_queue.command()
@app_commands.check(in_bot_vc)
async def queue_clear(itx: discord.Interaction[MusicBot]) -> None:
    """Empty the queue."""

    # Known at runtime in guild-only situation.
    assert itx.guild and isinstance(itx.user, discord.Member)
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        if not vc.queue.is_empty:
            vc.queue.clear()
            await itx.response.send_message("Queue cleared.")
        else:
            await itx.response.send_message("The queue is already empty.")
    else:
        await itx.response.send_message("No player to perform this on.")


@app_commands.command()
@app_commands.guild_only()
@app_commands.check(in_bot_vc)
async def muse_move(itx: discord.Interaction[MusicBot], before: int, after: int) -> None:
    """Move a song from one spot to another within the queue.

    Parameters
    ----------
    itx : :class:`discord.Interaction`
        The interaction that triggered this command.
    before : :class:`int`
        The index of the song you want moved.
    after : :class:`int`
        The index you want to move it to.
    """

    # Known at runtime in guild-only situation.
    assert itx.guild and isinstance(itx.user, discord.Member)
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        for num in (before, after):
            if num > len(vc.queue) or num < 1:
                await itx.response.send_message("Please enter valid queue indices.")
                return

        if before != after:
            vc.queue.put_at_index(after - 1, vc.queue[before - 1])
            del vc.queue[before]
        await itx.response.send_message(f"Successfully moved the track at {before} to {after} in the queue.")
    else:
        await itx.response.send_message("No player to perform this on.")


@app_commands.command()
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

    # Known at runtime in guild-only situation.
    assert itx.guild and isinstance(itx.user, discord.Member)
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        if vc.queue.is_empty:
            await itx.response.send_message("The queue is empty and can't be skipped into.")
        elif index > vc.queue.count or index < 1:
            await itx.response.send_message("Please enter a valid queue index.")
        else:
            if index > 1:
                vc.queue.remove_before_index(index - 1)
            vc.queue.loop = False
            await vc.stop()
            await itx.response.send_message(f"Skipped to the song at position {index}")
    else:
        await itx.response.send_message("No player to perform this on.")


@app_commands.command()
@app_commands.guild_only()
@app_commands.check(in_bot_vc)
async def muse_shuffle(itx: discord.Interaction[MusicBot]) -> None:
    """Shuffle the tracks in the queue."""

    # Known at runtime in guild-only situation.
    assert itx.guild and isinstance(itx.user, discord.Member)
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        if not vc.queue.is_empty:
            vc.queue.shuffle()
            await itx.response.send_message("Shuffled the queue.")
        else:
            await itx.response.send_message("There's nothing in the queue to shuffle right now.")
    else:
        await itx.response.send_message("No player to perform this on.")


@app_commands.command()
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

    # Known at runtime in guild-only situation.
    assert itx.guild and isinstance(itx.user, discord.Member)
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        if loop == "All Tracks":
            vc.queue.loop, vc.queue.loop_all = False, True
            await itx.response.send_message("Looping over all tracks in the queue until disabled.")
        elif loop == "Current Track":
            vc.queue.loop, vc.queue.loop_all = True, False
            await itx.response.send_message("Looping the current track until disabled.")
        else:
            vc.queue.loop, vc.queue.loop_all = False, False
            await itx.response.send_message("Reset the looping settings.")
    else:
        await itx.response.send_message("No player to perform this on.")


@app_commands.command()
@app_commands.guild_only()
@app_commands.check(in_bot_vc)
async def muse_seek(itx: discord.Interaction[MusicBot], *, position: str) -> None:
    """Seek to a particular position in the current track, provided with a `hours:minutes:seconds` string.

    Parameters
    ----------
    itx : :class:`discord.Interaction`
        The interaction that triggered this command.
    position : :class:`str`
        The time to jump to, given in the format `hours:minutes:seconds` or `minutes:seconds`.
    """

    # Known at runtime in guild-only situation.
    assert itx.guild and isinstance(itx.user, discord.Member)
    vc = itx.guild.voice_client
    assert isinstance(vc, MusicPlayer | None)

    if vc:
        if vc.current:
            if vc.current.is_seekable:
                pos_time = int(
                    sum(x * float(t) for x, t in zip([1, 60, 3600, 86400], reversed(position.split(":")), strict=False))
                    * 1000,
                )
                if pos_time > vc.current.duration or pos_time < 0:
                    await itx.response.send_message("Invalid position to seek.")
                else:
                    await vc.seek(pos_time)
                    await itx.response.send_message(f"Jumped to position `{position}` in the current track.")
            else:
                await itx.response.send_message("This track doesn't allow seeking, sorry.")
        else:
            await itx.response.send_message("No track to seek within currently playing.")
    else:
        await itx.response.send_message("No player to perform this on.")


@app_commands.command()
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

    # Known at runtime in guild-only situation.
    assert itx.guild and isinstance(itx.user, discord.Member)
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


MUSIC_APP_COMMANDS: list[app_commands.Command[Any, ..., Any] | app_commands.Group] = [
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
        This uses blocking file IO, so don't run this in situations where that matters. `setup_hook()` should be fine
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
    config : dict[str, Any]
        The configuration data for the radios, including Lavalink node credentials and potentially Spotify
        application credentials to allow Spotify links to work for stations.

    Attributes
    ----------
    config : dict[str, Any]
        The configuration data for the radios, including Lavalink node credentials and potentially Spotify
        application credentials to allow Spotify links to work for stations.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        super().__init__(
            intents=discord.Intents(guilds=True, voice_states=True, typing=True),  # TODO: Evaluate required intents.
            activity=discord.Game(name="https://github.com/Sachaa-Thanasius/discord-musicbot"),
        )
        self.tree = VersionableTree(self)

    async def on_connect(self) -> None:
        """(Re)set the client's general invite link every time it (re)connects to the Discord Gateway."""

        await self.wait_until_ready()
        data = await self.application_info()
        perms = discord.Permissions(274881367040)  # TODO: Evaluate required perms.
        self.invite_link = discord.utils.oauth_url(data.id, permissions=perms)

    async def setup_hook(self) -> None:
        """Perform a few operations before the bot connects to the Discord Gateway."""

        # Connect to the Lavalink node that will provide the music.
        node = wavelink.Node(**self.config["LAVALINK"])
        sc = spotify.SpotifyClient(**self.config["SPOTIFY"]) if ("SPOTIFY" in self.config) else None
        await wavelink.NodePool.connect(client=self, nodes=[node], spotify=sc)

        # Add the app commands to the tree.
        for cmd in MUSIC_APP_COMMANDS:
            self.tree.add_command(cmd)

        # Sync the tree if it's different from the previous version, using hashing for comparison.
        await self.tree.sync_if_commands_updated()

    async def on_wavelink_track_start(self, payload: wavelink.TrackEventPayload) -> None:
        """Called when a track starts playing.

        Sends a notification about the new track to the voice channel.
        """

        if payload.original:
            current_embed = await format_track_embed("Now Playing", payload.original)
            if payload.player.channel:
                await payload.player.channel.send(embed=current_embed)

    async def on_wavelink_track_end(self, payload: wavelink.TrackEventPayload) -> None:
        """Called when the current track has finished playing.

        Attempts to play the next track if available.
        """

        player = payload.player

        if player.is_connected():
            if player.queue.loop or player.queue.loop_all:
                next_track = player.queue.get()
            else:
                next_track = await player.queue.get_wait()
            await player.play(next_track)
        else:
            await player.stop()


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


def _input_spotify_creds() -> None:
    prompts = (
        "If you want the radio to process Spotify links, paste your Spotify app client id (won't be visible), then "
        "press enter. It will be stored for later use. Otherwise, just press enter to continue.",
        "If you previously entered a Spotify app client id, paste your corresponding app client secret, then press "
        "enter. It will be stored for later use. Otherwise, just press enter to continue.",
    )
    creds: list[str] = [secret for prompt in prompts if (secret := getpass.getpass(prompt))]
    if not creds:
        log.info("No Spotify credentials passed in. Continuing...")
        return
    if len(creds) == 1:
        msg = "If you add Spotify credentials, you must add the client ID AND the client secret, not just one."
        raise RuntimeError(msg)
    _store_credentials("musicbot_spotify.secrets", *creds)


def _get_token() -> str:
    token = os.getenv("DISCORD_TOKEN") or _get_stored_credentials("musicbot.token")
    if token is None:
        msg = (
            "You're missing a Discord bot token. Use '--token' in the CLI to trigger setup for it, or provide an "
            "environmental variable labelled 'DISCORD_TOKEN'."
        )
        raise RuntimeError(msg)
    return token[0] if isinstance(token, tuple) else token


def _get_lavalink_creds() -> dict[str, str]:
    if (ll_uri := os.getenv("LAVALINK_URI")) and (ll_pwd := os.getenv("LAVALINK_PASSWORD")):
        lavalink_creds = {"uri": ll_uri, "password": ll_pwd}
    elif ll_creds := _get_stored_credentials("musicbot_lavalink.secrets"):
        lavalink_creds = {"uri": ll_creds[0], "password": ll_creds[1]}
    else:
        msg = (
            "You're missing Lavalink node credentials. Use '--lavalink' in the CLI to trigger setup for it, or provide "
            "environmental variables labelled 'LAVALINK_URI' and 'LAVALINK_PASSWORD'."
        )
        raise RuntimeError(msg)
    return lavalink_creds


def _get_spotify_creds() -> dict[str, str] | None:
    if (sp_client_id := os.getenv("SPOTIFY_CLIENT_ID")) and (sp_client_secret := os.getenv("SPOTIFY_CLIENT_SECRET")):
        spotify_creds = {"client_id": sp_client_id, "client_secret": sp_client_secret}
    elif sp_creds := _get_stored_credentials("musicbot_spotify.secrets"):
        spotify_creds = {"client_id": sp_creds[0], "client_secret": sp_creds[1]}
    else:
        log.warning(
            "(Optional) You're missing Spotify node credentials. Use '--spotify' in the CLI to trigger setup for it, "
            "or provide environmental variables labelled 'SPOTIFY_CLIENT_ID' and 'SPOTIFY_CLIENT_SECRET'.",
        )
        spotify_creds = None
    return spotify_creds


def run_client() -> None:
    """Confirm existence of required credentials and launch the radio bot."""

    if uvloop:
        uvloop.install()  # type: ignore

    token = _get_token()
    lavalink_creds = _get_lavalink_creds()
    spotify_creds = _get_spotify_creds()

    config: dict[str, Any] = {"LAVALINK": lavalink_creds}
    if spotify_creds:
        config["SPOTIFY"] = spotify_creds

    client = MusicBot(config)

    client.run(token)


def main() -> None:
    token_req = bool(_get_stored_credentials("musicbot.token"))
    lavalink_req = bool(_get_stored_credentials("musicbot_lavalink.secrets"))

    parser = argparse.ArgumentParser(description="A minimal configuration discord bot for server radios.")
    setup_group = parser.add_argument_group(
        "setup",
        description="Choose credentials to specify. Discord token and Lavalink credentials are required on first run.",
    )
    setup_group.add_argument(
        "--token",
        action="store_true",
        default=not token_req,
        help="Whether to specify the Discord token. Initiates interactive setup.",
        dest="specify_token",
    )
    setup_group.add_argument(
        "--lavalink",
        action="store_true",
        default=not lavalink_req,
        help="Whether you want to specify the Lavalink node URI.",
        dest="specify_lavalink",
    )

    spotify_help = (
        "Whether to specify your Spotify app's credentials (required to use Spotify links in stations). "
        "Initiates interactive setup."
    )
    setup_group.add_argument("--spotify", action="store_true", help=spotify_help, dest="specify_spotify")

    args = parser.parse_args()

    if args.specify_token:
        _input_token()
    if args.specify_lavalink:
        _input_lavalink_creds()
    if args.specify_spotify:
        _input_spotify_creds()

    run_client()


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
