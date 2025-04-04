import discord
import os
import asyncio
from discord.ext import commands
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
from functools import partial
from dotenv import load_dotenv

load_dotenv()

# Initialize the bot with appropriate intents
intents = discord.Intents.default()
intents.voice_states = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Set up logging to capture errors and warnings for yt-dlp
class loggerOutputs:
    def error(msg):
        pass
    def warning(msg):
        pass
    def debug(msg):
        pass

# YouTube-DL and FFmpeg options
YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'quiet': True,
    'extract_flat': 'in_playlist',
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '192',
    }],
    'quiet': True,
    'logger': loggerOutputs,
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

TIMEOUT_DELAY = 60

QUEUE_LOAD_LIMIT = 20
QUEUE_EMBEDDING_SONG_LIMIT = 10

# Dictionary to maintain queues for each guild
currently_playing = {}
queues = {}
waiting_urls = {}
is_playing_audio = {}
audio_lock = {}

async def search_youtube(query: str) -> dict:
    """Asynchronously search YouTube for a single song and return its info."""
    loop = asyncio.get_running_loop()
    with YoutubeDL(YDL_OPTIONS) as ydl:
        try:
            info = await loop.run_in_executor(None, partial(ydl.extract_info, f"ytsearch:{query}", download=False))
            if not info or not info.get('entries'):
                return None
            return info['entries'][0]
        except Exception as e:
            print(f"Search error: {e}")
            return None

async def extract_info(ctx: commands.Context, url: str, playlist: bool = False) -> dict:
    """Asynchronously extract info from a URL."""
    loop = asyncio.get_running_loop()
    opts = YDL_OPTIONS.copy()
    opts['noplaylist'] = not playlist
    with YoutubeDL(opts) as ydl:
        try:
            return await loop.run_in_executor(None, partial(ydl.extract_info, url, download=False))
        except Exception as e:
            waiting_urls[ctx.guild.id]
            return None

# Define the async error handling function
async def playback_error(error: Exception):
    if error:
        print(f'Error in playback: {error}')

# Wrapper to run the async function
def sync_playback_error(error: Exception):
    asyncio.run_coroutine_threadsafe(playback_error(error), bot.loop)

def reset(guild_id: int):
    currently_playing[guild_id] = None
    queues[guild_id] = []
    waiting_urls[guild_id] = []
    is_playing_audio[guild_id] = False
    audio_lock[guild_id] = False

async def player_loop(ctx: commands.Context):
    while queues[ctx.guild.id] and ctx.voice_client:
        if not ctx.voice_client.is_playing():
            next_song = queues[ctx.guild.id].pop(0)
            source = discord.FFmpegOpusAudio(next_song['url'], **FFMPEG_OPTIONS)
            ctx.voice_client.play(source, after=sync_playback_error)
            
            is_playing_audio[ctx.guild.id] = True
            audio_lock[ctx.guild.id] = True
            
            currently_playing[ctx.guild.id] = next_song
        
        # Wait for the audio to finish playing
        while is_playing_audio[ctx.guild.id] and ctx.voice_client:
            if ctx.voice_client.is_playing():
                audio_lock[ctx.guild.id] = False
            
            if not audio_lock[ctx.guild.id] and not ctx.voice_client.is_playing():
                is_playing_audio[ctx.guild.id] = False
            
            await asyncio.sleep(1)
    
    # Disconnect after a timeout
    await asyncio.sleep(TIMEOUT_DELAY)
    if ctx.voice_client:
        if not ctx.voice_client.is_playing():
            await ctx.voice_client.disconnect()

async def extract_playlist_urls(ctx: commands.Context):
    while waiting_urls[ctx.guild.id] and ctx.voice_client:
        if len(queues[ctx.guild.id]) > QUEUE_LOAD_LIMIT:
            await asyncio.sleep(1)
            continue
        
        next_song = waiting_urls[ctx.guild.id].pop(0)
        info = await extract_info(ctx, next_song['url'])
        
        if ctx.voice_client and info:
            print(f"Adding {next_song['url']} to the queue in {ctx.guild.id} guild.")
        
            track = {
                'url': info['url'],
                'title': info.get('title', 'Unknown Title'),
                'duration': info.get('duration', 0),
                'thumbnail': info.get('thumbnail', None),
                'uploader': info.get('uploader', 'Unknown Uploader')
            }
            
            queues[ctx.guild.id].append(track)
            
            if not ctx.voice_client.is_playing():
                ctx.bot.loop.create_task(player_loop(ctx))
    
    await asyncio.sleep(0.5)

@bot.command()
async def play(ctx: commands.Context, *, search: str):
    """Play a song or an entire playlist from YouTube."""
    if not ctx.author.voice:
        return await ctx.send("You need to be in a voice channel!")

    # Connect if not already connected
    if not ctx.voice_client:
        await ctx.author.voice.channel.connect()

    # Initialize queue for the guild if not present
    if ctx.guild.id not in queues:
        queues[ctx.guild.id] = []
    
    if ctx.guild.id not in waiting_urls:
        waiting_urls[ctx.guild.id] = []

    # Check if the search query is a playlist URL (using 'list=' as a hint)
    if "list=" in search:
        info = await extract_info(ctx, search, playlist=True)
        if not info or 'entries' not in info:
            return await ctx.send("Couldn't retrieve playlist info.")
        
        # Queue all playlist entries
        for entry in info['entries']:
            if entry is None or 'url' not in entry:
                continue
            
            if "youtube.com/watch" in entry['url']:
                waiting_urls[ctx.guild.id].append({'url': entry['url']})
            
        embed = discord.Embed(
            title="Playlist Added to Queue",
            description=f"{len(info['entries'])} songs found in the playlist.",
            color=discord.Color.green()
        )
        
        embed.add_field(name="Playlist URL", value=f"[Click Here]({search})", inline=False)
        embed.add_field(name="Requested by", value=ctx.author.mention, inline=True)
        await ctx.send(embed=embed)
    else:
        # If it's a URL or a search term for a single song
        if "youtube.com/watch" in search:
            waiting_urls[ctx.guild.id].append({'url': search})
        else:
            info = await search_youtube(search)
        if not info:
            return await ctx.send("No results found!")
        
        waiting_urls[ctx.guild.id].append({'url': info['url']})
        
        print(f"Adding {info['url']} to the queue in {ctx.guild.id} guild.")
        
        # Create an embed for the song being added to the waiting list
        embed = discord.Embed(
            title="Song Added to Queue",
            description=f"[{info['title']}]({info['url']})",
            color=discord.Color.green()
        )
        
        embed.set_thumbnail(url=info.get('thumbnail', ''))
        embed.add_field(name="Duration", value=f"{info.get('duration', 0)} seconds", inline=True)
        embed.add_field(name="Uploader", value=info.get('uploader', 'Unknown Uploader'), inline=True)
        embed.add_field(name="Position in Queue", value=len(waiting_urls[ctx.guild.id]), inline=True)
        embed.add_field(name="Requested by", value=ctx.author.mention, inline=True)
        
        await ctx.send(embed=embed)
        
    await extract_playlist_urls(ctx)

@bot.command()
async def stop(ctx: commands.Context):
    """Stop playback and disconnect."""
    if ctx.voice_client:
        if ctx.voice_client.is_playing():
            ctx.voice_client.stop()
        
        await ctx.voice_client.disconnect()
    else:
        await ctx.send("Not in a voice channel!")

@bot.command()
async def skip(ctx: commands.Context):
    """Skip the currently playing song."""
    if ctx.voice_client:
        if ctx.voice_client.is_playing():
            ctx.voice_client.stop()
        await ctx.send(f"Skipping {currently_playing[ctx.guild.id]['title']}...")
    else:
        await ctx.send("Not in a voice channel!")

@bot.command()
async def queue(ctx: commands.Context):
    """Display the current queue with an embedded message for a fancy look."""
    if ctx.guild.id in queues and queues[ctx.guild.id]:
        queue_string = "\n".join([f"{i}. {song['title']}" for i, song in enumerate(queues[ctx.guild.id][:QUEUE_EMBEDDING_SONG_LIMIT], start=1)])
        
        if len(queues[ctx.guild.id]) > QUEUE_EMBEDDING_SONG_LIMIT:
            queue_string += "\n...and more!"
        
        embed = discord.Embed(title="Current Queue", description=queue_string, color=discord.Color.blue())
        embed.set_footer(text="Use !skip to skip the current song.")
        embed.add_field(name="Total Songs", value=len(queues[ctx.guild.id]), inline=True)
        
        await ctx.send(embed=embed)
    else:
        embed = discord.Embed(title="Queue is empty", description="Add some songs to get started!", color=discord.Color.red())
        await ctx.send(embed=embed)

@bot.command()
async def okul(ctx: commands.Context):
    """Play the Okul song."""
    await play(ctx, search="https://www.youtube.com/shorts/CevwOKeDFDQ")

@bot.event
async def on_voice_state_update(member, before, after):
    # Reset the queues if the bot leaves the voice channel
    if member == bot.user and before.channel and not after.channel:
        reset(member.guild.id)

@bot.event
async def on_ready():
    print(f"Application Started: {bot.user}")

if __name__ == "__main__":
    bot.run(os.getenv("DISCORD_BOT_TOKEN"))
