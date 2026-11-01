import asyncio
import os
import shutil
from pathlib import Path
import discord
import yt_dlp

from discord import app_commands
from discord.ext import commands


# =========================
# НАСТРОЙКИ
# =========================

TOKEN = os.getenv("DISCORD_TOKEN")


# =========================
# НАСТРОЙКА БОТА
# =========================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# Очереди для серверов
queues = {}
guild_volumes = {}
guild_repeat = {}


# =========================
# YOUTUBE / YT-DLP
# =========================

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "remote_components": ["ejs:github"],
    "extractor_args": {
        "youtube": {
            "player_client": ["android"]
        }
    },
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


def resolve_ffmpeg_executable():
    configured_path = os.getenv("FFMPEG_EXECUTABLE")
    if configured_path and Path(configured_path).is_file():
        return configured_path

    path_executable = shutil.which("ffmpeg")
    if path_executable:
        return path_executable

    winget_packages = Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    installed_executables = sorted(
        winget_packages.glob("Gyan.FFmpeg.Shared_*/*/bin/ffmpeg.exe")
    )
    if installed_executables:
        return str(installed_executables[0])

    return "ffmpeg"


FFMPEG_EXECUTABLE = resolve_ffmpeg_executable()


async def get_audio(query):
    loop = asyncio.get_running_loop()

    def extract():
        with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
            info = ydl.extract_info(query, download=False)

            if "entries" in info:
                info = info["entries"][0]

            return {
                "title": info["title"],
                "url": info["url"],
                "webpage": info.get("webpage_url", "")
            }

    return await loop.run_in_executor(None, extract)


# =========================
# ПРОИГРЫВАНИЕ
# =========================

async def play_next(guild_id, voice_client):

    if guild_id not in queues:
        return

    if len(queues[guild_id]) == 0:
        return

    song = queues[guild_id].pop(0)

    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(
            song["url"],
            executable=FFMPEG_EXECUTABLE,
            **FFMPEG_OPTIONS
        ),
        volume=guild_volumes.get(guild_id, 1.0)
    )

    def after_playing(error):
        if error:
            print("Ошибка воспроизведения:", error)

        if guild_repeat.get(guild_id, False):
            queues[guild_id].append(song)

        asyncio.run_coroutine_threadsafe(
            play_next(guild_id, voice_client),
            bot.loop
        )

    voice_client.play(
        source,
        after=after_playing
    )

    print("Сейчас играет:", song["title"])


# =========================
# READY
# =========================

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Зарегистрировано команд: {len(synced)}")
    except Exception as e:
        print("Ошибка синхронизации:", e)


# =========================
# /PLAY
# =========================

@bot.tree.command(
    name="play",
    description="Включить музыку"
)
@app_commands.describe(
    query="Название песни или ссылка"
)
async def play(
    interaction: discord.Interaction,
    query: str
):

    await interaction.response.defer()

    # Проверяем голосовой канал пользователя

    if not interaction.user.voice:
        await interaction.followup.send(
            "❌ Сначала зайди в голосовой канал."
        )
        return

    channel = interaction.user.voice.channel

    bot_member = interaction.guild.me
    channel_permissions = channel.permissions_for(bot_member)
    if not channel_permissions.connect or not channel_permissions.speak:
        await interaction.followup.send(
            "❌ У бота нет прав **Подключаться** и/или **Говорить** в этом голосовом канале."
        )
        return

    # Получаем voice client

    voice_client = interaction.guild.voice_client

    try:
        if voice_client is None:
            voice_client = await channel.connect(timeout=60, reconnect=True)

        elif voice_client.channel != channel:
            await voice_client.move_to(channel)

    except asyncio.TimeoutError:
        if voice_client and voice_client.is_connected():
            await voice_client.disconnect(force=True)

        await interaction.followup.send(
            "❌ Не удалось подключиться к голосовому каналу. "
            "Проверьте, что Discord не блокируется брандмауэром или VPN."
        )
        return

    # Ищем песню

    try:
        song = await get_audio(query)

    except Exception as e:

        print(e)

        await interaction.followup.send(
            "❌ Не удалось найти эту песню."
        )

        return

    guild_id = interaction.guild.id

    if guild_id not in queues:
        queues[guild_id] = []

    # Если что-то уже играет
    if voice_client.is_playing():

        queues[guild_id].append(song)

        await interaction.followup.send(
            f"➕ Добавлено в очередь:\n**{song['title']}**"
        )

    else:

        queues[guild_id].append(song)

        try:
            await play_next(guild_id, voice_client)
        except Exception as error:
            queues[guild_id].insert(0, song)
            print("Ошибка запуска FFmpeg:", error)
            await interaction.followup.send(
                "❌ Не удалось запустить аудио. Проверьте установку FFmpeg и права бота **Говорить**."
            )
            return

        embed = discord.Embed(
            title="🎵 Сейчас играет",
            description=f"**{song['title']}**"
        )

        await interaction.followup.send(
            embed=embed
        )


# =========================
# /SKIP
# =========================

@bot.tree.command(
    name="skip",
    description="Пропустить текущую песню"
)
async def skip(interaction: discord.Interaction):

    voice_client = interaction.guild.voice_client

    if voice_client is None:

        await interaction.response.send_message(
            "❌ Бот не находится в голосовом канале."
        )

        return

    if not voice_client.is_playing():

        await interaction.response.send_message(
            "❌ Сейчас ничего не играет."
        )

        return

    voice_client.stop()

    await interaction.response.send_message(
        "⏭️ Песня пропущена."
    )


# =========================
# /STOP
# =========================

@bot.tree.command(
    name="stop",
    description="Остановить музыку и очистить очередь"
)
async def stop(interaction: discord.Interaction):

    guild_id = interaction.guild.id

    if guild_id in queues:
        queues[guild_id].clear()

    guild_repeat[guild_id] = False

    voice_client = interaction.guild.voice_client

    if voice_client:

        voice_client.stop()

    await interaction.response.send_message(
        "⏹️ Музыка остановлена, очередь очищена."
    )


# =========================
# /PAUSE
# =========================

@bot.tree.command(
    name="pause",
    description="Поставить музыку на паузу"
)
async def pause(interaction: discord.Interaction):

    voice_client = interaction.guild.voice_client

    if not voice_client or not voice_client.is_playing():

        await interaction.response.send_message(
            "❌ Сейчас ничего не играет."
        )

        return

    if voice_client.is_paused():
        await interaction.response.send_message(
            "❌ Музыка уже стоит на паузе."
        )
        return

    voice_client.pause()

    await interaction.response.send_message(
        "⏸️ Музыка поставлена на паузу."
    )


# =========================
# /RESUME
# =========================

@bot.tree.command(
    name="resume",
    description="Продолжить музыку"
)
async def resume(interaction: discord.Interaction):

    voice_client = interaction.guild.voice_client

    if not voice_client:

        await interaction.response.send_message(
            "❌ Бот не находится в голосовом канале."
        )

        return

    if not voice_client.is_paused():
        await interaction.response.send_message(
            "❌ Музыка не стоит на паузе. Сначала используйте `/pause`."
        )
        return

    voice_client.resume()

    await interaction.response.send_message(
        "▶️ Музыка продолжена."
    )


# =========================
# /QUEUE
# =========================

@bot.tree.command(
    name="queue",
    description="Показать очередь"
)
async def queue(interaction: discord.Interaction):

    guild_id = interaction.guild.id

    if guild_id not in queues or not queues[guild_id]:

        await interaction.response.send_message(
            "📭 Очередь пустая."
        )

        return

    text = ""

    for i, song in enumerate(queues[guild_id], 1):

        text += f"**{i}.** {song['title']}\n"

    embed = discord.Embed(
        title="🎵 Очередь",
        description=text
    )

    await interaction.response.send_message(
        embed=embed
    )


# =========================
# /LEAVE
# =========================

@bot.tree.command(
    name="leave",
    description="Отключить бота от голосового канала"
)
async def leave(interaction: discord.Interaction):

    voice_client = interaction.guild.voice_client

    if not voice_client:

        await interaction.response.send_message(
            "❌ Бот не находится в голосовом канале."
        )

        return

    guild_id = interaction.guild.id

    if guild_id in queues:
        queues[guild_id].clear()

    guild_repeat[guild_id] = False

    await voice_client.disconnect()

    await interaction.response.send_message(
        "👋 Я вышел из голосового канала."
    )


# =========================
# /LOOP
# =========================

@bot.tree.command(
    name="loop",
    description="Повторять текущую песню после окончания"
)
async def loop(interaction: discord.Interaction):

    guild_id = interaction.guild.id
    voice_client = interaction.guild.voice_client

    if not voice_client or (not voice_client.is_playing() and not voice_client.is_paused()):
        await interaction.response.send_message(
            "❌ Сейчас ничего не играет."
        )
        return

    guild_repeat[guild_id] = not guild_repeat.get(guild_id, False)
    status = "включен" if guild_repeat[guild_id] else "выключен"
    await interaction.response.send_message(
        f"🔁 Повтор песни {status}."
    )


# =========================
# /VOLUME и /VOLME
# =========================

async def set_volume(interaction: discord.Interaction, volume: int):

    guild_id = interaction.guild.id
    guild_volumes[guild_id] = volume / 100
    voice_client = interaction.guild.voice_client

    if voice_client and isinstance(voice_client.source, discord.PCMVolumeTransformer):
        voice_client.source.volume = volume / 100

    await interaction.response.send_message(
        f"🔊 Громкость установлена: **{volume}%**."
    )


@bot.tree.command(
    name="volume",
    description="Установить громкость музыки от 0 до 100"
)
@app_commands.describe(volume="Громкость в процентах")
async def volume(interaction: discord.Interaction, volume: app_commands.Range[int, 0, 100]):
    await set_volume(interaction, volume)


@bot.tree.command(
    name="volme",
    description="Установить громкость музыки от 0 до 100"
)
@app_commands.describe(volume="Громкость в процентах")
async def volme(interaction: discord.Interaction, volume: app_commands.Range[int, 0, 100]):
    await set_volume(interaction, volume)


# =========================
# ЗАПУСК
# =========================

if not TOKEN:
    raise RuntimeError(
        "Установите токен бота в переменную окружения DISCORD_TOKEN."
    )

bot.run(TOKEN)