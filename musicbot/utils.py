from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Coroutine
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Concatenate, NamedTuple, ParamSpec, Self, TypeVar

import discord
import wavelink
from discord import app_commands


if TYPE_CHECKING:
    from .bot import MusicBot
else:
    MusicBot = discord.AutoShardedClient


P = ParamSpec("P")
T = TypeVar("T")
UnboundCommandCallback = Callable[Concatenate[discord.Interaction[Any], P], Coroutine[Any, Any, T]]

__all__ = (
    "MusicBotError",
    "NotInVoiceChannel",
    "NotInBotVoiceChannel",
    "InvalidShortTimeFormat",
    "WavelinkSearchError",
    "LavalinkCreds",
    "ShortTime",
    "WavelinkSearchTransformer",
    "MusicQueue",
    "MusicPlayer",
    "MusicQueueView",
    "resolve_path_with_links",
    "create_track_embed",
    "generate_tracks_add_notification",
    "ensure_voice_hook",
    "is_in_bot_vc",
)

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

    async def transform(self, itx: discord.Interaction, value: str, /) -> wavelink.Playable | wavelink.Playlist:
        # Searching can take a while sometimes.
        await itx.response.defer()

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
        self.pages = [pages_content[i : (i + per)] for i in range(0, len(pages_content), per)]
        self.page_index: int = 1

        # Activate the right buttons on instantiation.
        self.clear_items().add_page_buttons()
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
        """Removes all buttons when the view times out."""

        self.clear_items()
        await self.message.edit(view=self)
        self.stop()

    def add_page_buttons(self) -> Self:
        """Only adds the necessary page buttons based on how many pages there are.

        This function returns the class instance to allow for fluent-style chaining.
        """

        if self.total_pages > 2:
            (
                self.add_item(self.turn_to_first)
                .add_item(self.turn_to_previous)
                .add_item(self.enter_page)
                .add_item(self.turn_to_next)
                .add_item(self.turn_to_last)
            )
        elif self.total_pages > 1:
            self.add_item(self.turn_to_previous).add_item(self.turn_to_next)

        self.add_item(self.quit_view)

        return self

    def disable_page_buttons(self) -> None:
        """Enables and disables page-turning buttons based on page count, position, and movement."""

        if self.total_pages <= 1:
            self.turn_to_next.disabled = self.turn_to_last.disabled = True
            self.turn_to_previous.disabled = self.turn_to_first.disabled = True
            self.enter_page.disabled = True
        else:
            self.turn_to_previous.disabled = self.turn_to_first.disabled = self.page_index == 0
            self.turn_to_next.disabled = self.turn_to_last.disabled = self.page_index == self.total_pages - 1
            self.enter_page.disabled = False

    def format_page(self) -> discord.Embed:
        """Makes the embed 'page' that the user will see."""

        embed_page = discord.Embed(color=0x149CDF, title="Music Queue")

        if self.total_pages == 0:
            embed_page.description = "The queue is empty."
            embed_page.set_footer(text="Page 0/0")
        else:
            # Expected page size of 10
            content = self.pages[self.page_index]
            organized = (f"{i + (self.page_index) * 10}. {track}" for i, track in enumerate(content, 1))
            embed_page.description = "\n".join(organized)
            embed_page.set_footer(text=f"Page {self.page_index + 1}/{self.total_pages}")

        return embed_page

    def get_first_page(self) -> discord.Embed:
        """Get the embed of the first page."""

        temp = self.page_index
        self.page_index = 0
        embed = self.format_page()
        self.page_index = temp
        return embed

    async def update_page(self, interaction: discord.Interaction) -> None:
        """Update and display the view for the given page."""

        embed_page = self.format_page()
        self.disable_page_buttons()
        await interaction.response.edit_message(embed=embed_page, view=self)

    @discord.ui.button(label="\N{MUCH LESS-THAN}", style=discord.ButtonStyle.blurple, disabled=True)
    async def turn_to_first(self, interaction: discord.Interaction, _: discord.ui.Button[Self]) -> None:
        """Skips to the first page of the view."""

        self.page_index = 0
        await self.update_page(interaction)

    @discord.ui.button(label="<", style=discord.ButtonStyle.blurple, disabled=True, custom_id="page_view:prev")
    async def turn_to_previous(self, interaction: discord.Interaction, _: discord.ui.Button[Self]) -> None:
        """Turns to the previous page of the view."""

        self.page_index -= 1
        await self.update_page(interaction)

    @discord.ui.button(label="\N{BOOK}", style=discord.ButtonStyle.green, disabled=True, custom_id="page_view:enter")
    async def enter_page(self, interaction: discord.Interaction, _: discord.ui.Button[Self]) -> None:
        """Sends a modal that a user to enter their own page number into."""

        # Get page number from a modal.
        modal = PageNumEntryModal()
        await interaction.response.send_modal(modal)
        modal_timed_out = await modal.wait()

        if modal_timed_out or self.is_finished():
            return

        assert modal.interaction is not None  # The modal had to be submitted to reach this point.

        # Validate the input.
        try:
            temp_new_page = int(modal.input_page_num.value)
        except ValueError:
            return

        temp_new_page -= 1

        if temp_new_page >= self.total_pages or temp_new_page < 0 or self.page_index == temp_new_page:
            return

        self.page_index = temp_new_page
        await self.update_page(modal.interaction)

    @discord.ui.button(label=">", style=discord.ButtonStyle.blurple)
    async def turn_to_next(self, interaction: discord.Interaction, _: discord.ui.Button[Self]) -> None:
        """Turns to the next page of the view."""

        self.page_index += 1
        await self.update_page(interaction)

    @discord.ui.button(label="\N{MUCH GREATER-THAN}", style=discord.ButtonStyle.blurple)
    async def turn_to_last(self, interaction: discord.Interaction, _: discord.ui.Button[Self]) -> None:
        """Skips to the last page of the view."""

        self.page_index = self.total_pages - 1
        await self.update_page(interaction)

    @discord.ui.button(label="\N{MULTIPLICATION X}", style=discord.ButtonStyle.red)
    async def quit_view(self, interaction: discord.Interaction, _: discord.ui.Button[Self]) -> None:
        """Deletes the original message with the view after a slight delay."""

        self.stop()
        await interaction.response.defer()
        await asyncio.sleep(0.25)
        await interaction.delete_original_response()


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

    embed = discord.Embed(color=0x76C3A2, title=title, description=description)

    if track.artwork:
        embed.set_thumbnail(url=track.artwork)

    if track.album.name:
        embed.add_field(name="Album", value=track.album.name)

    if requester := getattr(track, "requester", None):
        embed.add_field(name="Requested By", value=requester)

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

        if vc is None:
            if itx.user.voice:
                # Not sure in what circumstances a member would have a voice state without being in a valid channel.
                assert itx.user.voice.channel
                await itx.user.voice.channel.connect(cls=MusicPlayer)
            else:
                raise NotInVoiceChannel
        return await func(itx, *args, **kwargs)

    return callback


def is_in_bot_vc() -> Callable[[T], T]:
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

    def predicate(itx: discord.Interaction) -> bool:
        if not itx.guild or not isinstance(itx.user, discord.Member):
            raise app_commands.NoPrivateMessage

        vc = itx.guild.voice_client

        if not (
            itx.user.guild_permissions.administrator
            or (vc and itx.user.voice and (itx.user.voice.channel == vc.channel))
        ):
            raise NotInBotVoiceChannel
        return True

    return app_commands.check(predicate)
