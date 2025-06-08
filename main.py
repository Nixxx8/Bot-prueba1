import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
import os
import asyncio
from dotenv import load_dotenv
from datetime import datetime
import sqlite3
import yt_dlp
from collections import deque
import time
import traceback
import subprocess
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from spotipy.exceptions import SpotifyException
import re

import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
import os
import asyncio
from dotenv import load_dotenv
from datetime import datetime
import sqlite3
import yt_dlp
from collections import deque
import time
import traceback
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from spotipy.exceptions import SpotifyException
from typing import Dict, Deque, Optional, List

# --------------------------
# Configuración Inicial
# --------------------------

# Carga de variables de entorno
load_dotenv()

# Configuración de intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# --------------------------
# Constantes de Configuración
# --------------------------

# Configuración general
DISCONNECT_AFTER = 60
MUTE_ROLE_NAME = "Muted"
MAX_ADVERTENCIAS = 7
ALERTA_ADVERTENCIAS = 5

# IDs de roles y canales
STAFF_ROLES = [1380930376343752704, 1380930523668549703, 1380930573899665538, 1380930606191607949]
LOG_CHANNEL_ID = 1381026786368032819
TICKET_CATEGORY_ID = 1380982177344520287

# Configuración de audio
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -probesize 32M -analyzeduration 32M',
    'options': '-vn -c:a libopus -b:a 128k -ar 48000 -ac 2 -filter:a "volume=0.8"',
    'executable': 'ffmpeg',
}

AUDIO_QUALITIES = {
    'low': {'bitrate': '64k', 'options': '-vn -af "volume=0.9"'},
    'medium': {'bitrate': '128k', 'options': '-vn -af "dynaudnorm=f=150:g=15"'},
    'high': {'bitrate': '192k', 'options': '-vn -ar 48000 -ac 2 -af "dynaudnorm=f=150:g=15"'}
}

# --------------------------
# Base de Datos
# --------------------------

def setup_database():
    conn = sqlite3.connect('moderacion.db')
    cursor = conn.cursor()
    
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS infracciones (
        user_id INTEGER,
        guild_id INTEGER,
        motivo TEXT,
        fecha TEXT,
        PRIMARY KEY (user_id, guild_id, fecha)
    )
    ''')
    conn.commit()
    return conn, cursor

db_conn, db_cursor = setup_database()


# Sistema de colas por servidor
music_queues = {}
current_playing = {}  # Trackea la canción actual por servidor


# --------------------------
# Clases Principales
# --------------------------

class MusicQueue:
    def __init__(self):
        self.queues: Dict[int, Deque] = {}
        self.current: Dict[int, Dict] = {}
        self.disconnect_timers: Dict[int, asyncio.Task] = {}

    def get_queue(self, guild_id: int) -> Deque:
        if guild_id not in self.queues:
            self.queues[guild_id] = deque()
        return self.queues[guild_id]

    def clear(self, guild_id: int):
        if guild_id in self.queues:
            self.queues[guild_id].clear()
        if guild_id in self.current:
            del self.current[guild_id]

    async def cancel_disconnect_timer(self, guild_id: int):
        if guild_id in self.disconnect_timers:
            self.disconnect_timers[guild_id].cancel()
            del self.disconnect_timers[guild_id]

music_queue = MusicQueue()

class MusicPlayer:
    YDL_OPTIONS = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'extractaudio': True,
        'audioformat': 'mp3',
        'noplaylist': True,
        'socket_timeout': 5,
        'source_address': '0.0.0.0',
        'force-ipv4': True,
        'cachedir': False,
        'extractor_args': {
            'youtube': {
                'player_skip': ['configs', 'webpage'],
                'skip': ['hls', 'dash', 'translated_subs']
            }
        },
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    }

    @classmethod
    async def get_audio_source(cls, query: str) -> Optional[Dict]:
        try:
            with yt_dlp.YoutubeDL(cls.YDL_OPTIONS) as ydl:
                if not query.startswith(('http://', 'https://')):
                    query = f"ytsearch:{query}"
                
                info = await bot.loop.run_in_executor(None, lambda: ydl.extract_info(query, download=False))
                
                if 'entries' in info:
                    info = info['entries'][0]
                
                return {
                    'url': info['url'],
                    'title': info.get('title', 'Audio desconocido'),
                    'duration': info.get('duration', 0)
                }
        except Exception:
            print(f"Error al obtener audio: {traceback.format_exc()}")
            return None

async def play_next(guild_id: int, error=None):
    voice_client = discord.utils.get(bot.voice_clients, guild=bot.get_guild(guild_id))
    
    if error:
        print(f"Error en reproducción: {error}")
    
    if not voice_client or not voice_client.is_connected():
        return

    # Limpiar canción actual
    if guild_id in music_queue.current:
        del music_queue.current[guild_id]
    
    # Cancelar cualquier temporizador de desconexión existente
    await music_queue.cancel_disconnect_timer(guild_id)
    
    # Obtener la cola de reproducción
    queue = music_queue.get_queue(guild_id)
    
    # Si no hay más canciones en la cola
    if not queue:
        channel = voice_client.channel
        
        # Solo iniciar temporizador si realmente no hay nada más en cola
        if guild_id not in music_queue.queues or not music_queue.queues[guild_id]:
            await channel.send(f"🛑 No hay más canciones en la cola. Me desconectaré en {DISCONNECT_AFTER} segundos...")
            
            async def disconnect_task():
                try:
                    await asyncio.sleep(DISCONNECT_AFTER)
                    
                    # Verificar nuevamente que no hayan añadido canciones
                    if guild_id not in music_queue.queues or not music_queue.queues[guild_id]:
                        if voice_client.is_connected():
                            await channel.send("🔌 Desconectando por inactividad...")
                            await voice_client.disconnect()
                except Exception as e:
                    print(f"Error en desconexión automática: {e}")
                finally:
                    if guild_id in music_queue.disconnect_timers:
                        del music_queue.disconnect_timers[guild_id]
            
            music_queue.disconnect_timers[guild_id] = asyncio.create_task(disconnect_task())
        return
    
    # Reproducir siguiente canción
    next_song = queue.popleft()
    music_queue.current[guild_id] = next_song
    
    try:
        # Configuración adaptativa de calidad
        adaptive_options = FFMPEG_OPTIONS.copy()
        if voice_client.latency > 0.3:
            adaptive_options['options'] = '-vn -b:a 96k'
            
        try:
            source = await discord.FFmpegOpusAudio.from_probe(
                next_song['url'],
                **adaptive_options,
                method='fallback'
            )
        except:
            source = discord.FFmpegPCMAudio(
                next_song['url'],
                **adaptive_options
            )
        
        if hasattr(source, '_player'):
            source._player.opus_encoder.set_bitrate(128000)
            source._player.buffer_size = 960 * 5
            
        voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(guild_id, e), bot.loop))
        
        await bot.change_presence(activity=discord.Activity(
            type=discord.ActivityType.listening,
            name=next_song['title'][:50]
        ))
        
    except Exception:
        print(f"Error al reproducir: {traceback.format_exc()}")
        await asyncio.sleep(2)
        await play_next(guild_id)

# ------------------------------------------
# Comandos de Música
# ------------------------------------------

@bot.command(name="play", aliases=["p"])
async def play(ctx, *, query: str):
    """Reproduce música desde YouTube o la añade a la cola"""
    if not ctx.author.voice:
        return await ctx.send("🚨 Debes estar en un canal de voz para usar este comando!")

    try:
        # Cancelar cualquier temporizador de desconexión existente
        await music_queue.cancel_disconnect_timer(ctx.guild.id)
        
        data = await MusicPlayer.get_audio_source(query)
        if not data:
            return await ctx.send("❌ No se pudo encontrar el video o la canción")

        voice_client = ctx.voice_client or await ctx.author.voice.channel.connect()
        
        queue = music_queue.get_queue(ctx.guild.id)
        queue.append(data)

        if not voice_client.is_playing() and ctx.guild.id not in music_queue.current:
            await play_next(ctx.guild.id)
            await ctx.send(f"🎶 **Reproduciendo:** {data['title']}")
        else:
            await ctx.send(f"🎵 **Añadido a la cola:** {data['title']}")

    except Exception:
        await ctx.send("❌ Error al reproducir")
        print(f"Error en play: {traceback.format_exc()}")

@bot.command(name="skip")
async def skip(ctx):
    """Salta la canción actual"""
    voice_client = ctx.voice_client
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()
        await ctx.send("⏭️ Canción saltada")
        await play_next(ctx.guild.id)
    else:
        await ctx.send("❌ No hay música reproduciéndose")

@bot.command(name="stop")
async def stop(ctx):
    """Detiene la música y limpia la cola"""
    voice_client = ctx.voice_client
    if voice_client:
        music_queue.clear(ctx.guild.id)
        if voice_client.is_playing():
            voice_client.stop()
        await voice_client.disconnect()
        await ctx.send("⏹️ Música detenida y bot desconectado")
    else:
        await ctx.send("❌ No estoy conectado a un canal de voz")


@bot.command(name="queue", aliases=["q"])
async def queue(ctx):
    """Muestra la cola de reproducción"""
    queue_list = []
    
    if ctx.guild.id in music_queue.current:
        queue_list.append(f"**Reproduciendo ahora:**\n1. {music_queue.current[ctx.guild.id]['title']}")
    
    queue = music_queue.get_queue(ctx.guild.id)
    if queue:
        queue_list.append("\n**En cola:**")
        start = 2 if ctx.guild.id in music_queue.current else 1
        for i, song in enumerate(list(queue)[:10], start=start):
            queue_list.append(f"{i}. {song['title']}")
    
    await ctx.send("\n".join(queue_list) if queue_list else "❌ No hay música en la cola")


@bot.command(name="quality")
async def set_quality(ctx, quality: str = 'medium'):
    """Ajusta la calidad de audio (low/medium/high)"""
    if quality not in AUDIO_QUALITIES:
        return await ctx.send("❌ Calidad no válida. Usa low/medium/high")
    
    FFMPEG_OPTIONS['options'] = AUDIO_QUALITIES[quality]['options']
    await ctx.send(f"✅ Calidad establecida a **{quality}** (Bitrate: {AUDIO_QUALITIES[quality]['bitrate']})")

@bot.command(name="pause")
async def pause(ctx):
    """Pausa la reproducción actual"""
    voice = ctx.voice_client
    if voice and voice.is_playing():
        voice.pause()
        await ctx.send("⏸️ Música pausada")
    else:
        await ctx.send("❌ No hay música reproduciéndose")

@bot.command(name="resume")
async def resume(ctx):
    """Reanuda la reproducción pausada"""
    voice = ctx.voice_client
    if voice and voice.is_paused():
        voice.resume()
        await ctx.send("▶️ Música reanudada")
    else:
        await ctx.send("❌ No hay música pausada")

@bot.command(name="nowplaying", aliases=["np"])
async def nowplaying(ctx):
    """Muestra la canción actual"""
    if ctx.guild.id in current_playing:
        await ctx.send(f"🎶 Reproduciendo ahora: {current_playing[ctx.guild.id]['title']}")
    else:
        await ctx.send("❌ No hay música reproduciéndose")

@bot.command()
async def latency(ctx):
    """Mide la latencia del bot"""
    before = time.monotonic()
    message = await ctx.send("🏓 Probando latencia...")
    ping = (time.monotonic() - before) * 1000
    content = f"🏓 Latencia: {int(ping)}ms"
    if ctx.voice_client:
        content += f" | Voz: {int(ctx.voice_client.latency*1000)}ms"
    await message.edit(content=content)
    
    
# Constantes de configuración
STAFF_ROLES = [1380930376343752704, 1380930523668549703, 1380930573899665538, 1380930606191607949]
LOG_CHANNEL_ID = 1381026786368032819
TICKET_CATEGORY_ID = 1380982177344520287
MAX_ADVERTENCIAS = 7
ALERTA_ADVERTENCIAS = 5
MUTE_ROLE_NAME = "Muted"

# Base de datos
conn = sqlite3.connect('moderacion.db')
cursor = conn.cursor()

# Tabla de infracciones
cursor.execute('''
CREATE TABLE IF NOT EXISTS infracciones (
    user_id INTEGER,
    guild_id INTEGER,
    motivo TEXT,
    fecha TEXT,
    PRIMARY KEY (user_id, guild_id, fecha)
)
''')
conn.commit()

# Funciones de utilidad
async def add_infraction(user_id: int, guild_id: int, reason: str):
    db_cursor.execute('''
    INSERT INTO infracciones (user_id, guild_id, motivo, fecha)
    VALUES (?, ?, ?, ?)
    ''', (user_id, guild_id, reason, datetime.now().isoformat()))
    db_conn.commit()

async def get_infractions(user_id: int, guild_id: int) -> int:
    db_cursor.execute('''
    SELECT COUNT(*) FROM infracciones 
    WHERE user_id = ? AND guild_id = ?
    ''', (user_id, guild_id))
    return db_cursor.fetchone()[0]

async def clear_infractions(user_id: int, guild_id: int):
    db_cursor.execute('''
    DELETE FROM infracciones 
    WHERE user_id = ? AND guild_id = ?
    ''', (user_id, guild_id))
    db_conn.commit()

async def is_staff(member: discord.Member) -> bool:
    return any(role.id in STAFF_ROLES for role in member.roles)

# Sistema de Tickets
class CloseTicketModal(ui.Modal, title="Cerrar Ticket"):
    motivo = ui.TextInput(label="Motivo del cierre", style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        creator_id = interaction.channel.topic.split("Creador: ")[1] if "Creador: " in interaction.channel.topic else "Desconocido"
        creator = interaction.guild.get_member(int(creator_id)) if creator_id.isdigit() else None
        
        # Registrar en logs
        if log_channel:
            embed = discord.Embed(
                title="🔒 Ticket Cerrado",
                description=(
                    f"**Ticket:** #{interaction.channel.name}\n"
                    f"**Creador:** {creator.mention if creator else creator_id}\n"
                    f"**Cerrado por:** {interaction.user.mention}\n"
                    f"**Motivo:** {self.motivo.value}"
                ),
                color=discord.Color.red()
            )
            await log_channel.send(embed=embed)
        
        # Notificar al usuario
        if creator:
            try:
                await creator.send(
                    f"📌 Tu ticket en **{interaction.guild.name}** ha sido cerrado\n"
                    f"**Motivo:** {self.motivo.value}"
                )
            except discord.HTTPException:
                pass
        
        await interaction.response.send_message("Cerrando ticket en 5 segundos...")
        await asyncio.sleep(5)
        await interaction.channel.delete()

class TicketView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @ui.button(label="Reclamar Ticket", style=discord.ButtonStyle.blurple, custom_id="ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: ui.Button):
        if not await is_staff(interaction.user):
            return await interaction.response.send_message("❌ Solo el staff puede reclamar tickets.", ephemeral=True)
        
        button.disabled = True
        button.label = f"Reclamado por {interaction.user.name}"
        await interaction.message.edit(view=self)
        
        await interaction.channel.set_permissions(
            interaction.user,
            read_messages=True,
            send_messages=True
        )
        
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(
                f"📌 Ticket reclamado: {interaction.channel.mention}\n"
                f"🛠️ Staff: {interaction.user.mention}"
            )
        await interaction.response.send_message(f"✅ Ticket reclamado por {interaction.user.mention}")

    @ui.button(label="Cerrar Ticket", style=discord.ButtonStyle.red, custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: ui.Button):
        if not await is_staff(interaction.user):
            return await interaction.response.send_message("❌ Solo el staff puede cerrar tickets.", ephemeral=True)
        await interaction.response.send_modal(CloseTicketModal())

class TicketModal(ui.Modal, title="Nuevo Ticket"):
    motivo = ui.TextInput(label="Motivo", style=discord.TextStyle.short)
    descripcion = ui.TextInput(label="Descripción detallada", style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        category = bot.get_channel(TICKET_CATEGORY_ID)
        if not category:
            return await interaction.response.send_message("❌ No se encontró la categoría para tickets.", ephemeral=True)
        
        ticket_channel = await category.create_text_channel(
            name=f"ticket-{interaction.user.name}",
            topic=f"Motivo: {self.motivo}\nCreador: {interaction.user.id}"
        )
        
        await ticket_channel.set_permissions(
            interaction.guild.default_role,
            read_messages=False
        )
        await ticket_channel.set_permissions(
            interaction.user,
            read_messages=True,
            send_messages=True
        )
        
        embed = discord.Embed(
            title=f"Ticket de {interaction.user.name}",
            description=f"**Motivo:** {self.motivo}\n**Descripción:** {self.descripcion}",
            color=discord.Color.blue()
        )
        
        await ticket_channel.send(
            content=f"{interaction.user.mention} | <@&{STAFF_ROLES[0]}>",
            embed=embed,
            view=TicketView()
        )
        
        await interaction.response.send_message(f"✅ Ticket creado en {ticket_channel.mention}", ephemeral=True)

# --------------------------
# Comandos de Moderación Mejorados
# --------------------------

@bot.tree.command(name="advertir", description="Envía una advertencia a un usuario")
@app_commands.describe(
    usuario="Usuario a advertir",
    motivo="Motivo de la advertencia"
)
@app_commands.default_permissions(manage_messages=True)
async def advertir(interaction: discord.Interaction, usuario: discord.Member, motivo: str):
    """Sistema de advertencias mejorado con registro en DB y notificaciones"""
    if not await is_staff(interaction.user):
        return await interaction.response.send_message("❌ No tienes permisos para usar este comando.", ephemeral=True)
    
    if usuario.top_role.position >= interaction.user.top_role.position:
        return await interaction.response.send_message("❌ No puedes advertir a alguien con igual o mayor rango.", ephemeral=True)
    
    # Registrar infracción
    await add_infraction(usuario.id, interaction.guild.id, motivo)
    total = await get_infractions(usuario.id, interaction.guild.id)
    
    # Crear embed de respuesta
    embed = discord.Embed(
        title="⚠️ Advertencia Registrada",
        description=f"**Usuario:** {usuario.mention}\n**Moderador:** {interaction.user.mention}",
        color=discord.Color.gold()
    )
    embed.add_field(name="Motivo", value=motivo, inline=False)
    embed.add_field(name="Advertencias totales", value=f"{total}/{MAX_ADVERTENCIAS}", inline=True)
    
    if total >= ALERTA_ADVERTENCIAS:
        embed.color = discord.Color.orange()
        embed.set_footer(text=f"¡Alerta! Este usuario tiene {total} advertencias")
    
    await interaction.response.send_message(embed=embed)
    
    # Notificar al usuario
    try:
        user_embed = discord.Embed(
            title=f"⚠️ Has recibido una advertencia en {interaction.guild.name}",
            description=f"**Motivo:** {motivo}\n**Advertencias totales:** {total}",
            color=discord.Color.gold()
        )
        await usuario.send(embed=user_embed)
    except discord.HTTPException:
        pass

@bot.tree.command(name="mutear", description="Silencia a un usuario por un tiempo determinado")
@app_commands.describe(
    usuario="Usuario a mutear",
    duracion="Duración del mute",
    motivo="Motivo del mute (opcional)"
)
@app_commands.choices(duracion=[
    app_commands.Choice(name="5 minutos", value="300"),
    app_commands.Choice(name="1 hora", value="3600"),
    app_commands.Choice(name="1 día", value="86400"),
    app_commands.Choice(name="1 semana", value="604800")
])
@app_commands.default_permissions(manage_messages=True)
async def mutear(interaction: discord.Interaction, usuario: discord.Member, 
                duracion: app_commands.Choice[str], motivo: str = "No especificado"):
    """Sistema de muteo con temporizador automático"""
    if not await is_staff(interaction.user):
        return await interaction.response.send_message("❌ No tienes permisos para usar este comando.", ephemeral=True)
    
    if usuario.top_role.position >= interaction.user.top_role.position:
        return await interaction.response.send_message("❌ No puedes mutear a alguien con igual o mayor rango.", ephemeral=True)
    
    mute_role = discord.utils.get(interaction.guild.roles, name=MUTE_ROLE_NAME)
    if not mute_role:
        return await interaction.response.send_message(f"❌ No existe el rol '{MUTE_ROLE_NAME}'.", ephemeral=True)
    
    try:
        # Aplicar mute
        await usuario.add_roles(mute_role, reason=motivo)
        
        # Crear embed de respuesta
        embed = discord.Embed(
            title="🔇 Usuario muteado",
            description=f"**Usuario:** {usuario.mention}\n**Moderador:** {interaction.user.mention}",
            color=discord.Color.blue()
        )
        embed.add_field(name="Duración", value=duracion.name, inline=True)
        embed.add_field(name="Motivo", value=motivo, inline=True)
        
        await interaction.response.send_message(embed=embed)
        
        # Notificar al usuario
        try:
            user_embed = discord.Embed(
                title=f"🔇 Has sido muteado en {interaction.guild.name}",
                description=f"**Duración:** {duracion.name}\n**Motivo:** {motivo}",
                color=discord.Color.blue()
            )
            await usuario.send(embed=user_embed)
        except discord.HTTPException:
            pass
        
        # Temporizador para auto-desmutear
        await asyncio.sleep(int(duracion.value))
        if mute_role in usuario.roles:
            await usuario.remove_roles(mute_role)
            
    except Exception as e:
        await interaction.response.send_message(f"❌ Error al mutear: {str(e)}", ephemeral=True)

@bot.tree.command(name="banear", description="Expulsa permanentemente a un usuario del servidor")
@app_commands.describe(
    usuario="Usuario a banear",
    motivo="Motivo del ban (opcional)",
    borrar_mensajes="Días de mensajes a borrar (0-7)"
)
@app_commands.default_permissions(ban_members=True)
async def banear(interaction: discord.Interaction, usuario: discord.Member, 
                motivo: str = "No especificado", borrar_mensajes: int = 0):
    """Sistema de ban mejorado con opción de purga de mensajes"""
    if not await is_staff(interaction.user):
        return await interaction.response.send_message("❌ No tienes permisos para usar este comando.", ephemeral=True)
    
    if usuario.top_role.position >= interaction.user.top_role.position:
        return await interaction.response.send_message("❌ No puedes banear a alguien con igual o mayor rango.", ephemeral=True)
    
    borrar_mensajes = min(7, max(0, borrar_mensajes))
    
    try:
        await usuario.ban(reason=motivo, delete_message_days=borrar_mensajes)
        
        embed = discord.Embed(
            title="🔨 Usuario baneado",
            description=f"**Usuario:** {usuario.mention}\n**Moderador:** {interaction.user.mention}",
            color=discord.Color.red()
        )
        embed.add_field(name="Motivo", value=motivo, inline=True)
        embed.add_field(name="Mensajes borrados", value=f"{borrar_mensajes} días", inline=True)
        
        await interaction.response.send_message(embed=embed)
        
    except Exception as e:
        await interaction.response.send_message(f"❌ Error al banear: {str(e)}", ephemeral=True)

@bot.tree.command(name="desmutear", description="Remueve el mute de un usuario")
@app_commands.describe(usuario="Usuario a desmutear")
@app_commands.default_permissions(manage_messages=True)
async def desmutear(interaction: discord.Interaction, usuario: discord.Member):
    """Comando para remover mute de un usuario"""
    if not await is_staff(interaction.user):
        return await interaction.response.send_message("❌ No tienes permisos para usar este comando.", ephemeral=True)
    
    mute_role = discord.utils.get(interaction.guild.roles, name=MUTE_ROLE_NAME)
    if not mute_role:
        return await interaction.response.send_message(f"❌ No existe el rol '{MUTE_ROLE_NAME}'.", ephemeral=True)
    
    if mute_role not in usuario.roles:
        return await interaction.response.send_message(f"❌ {usuario.mention} no está muteado.", ephemeral=True)
    
    try:
        await usuario.remove_roles(mute_role)
        
        embed = discord.Embed(
            title="🔊 Usuario desmuteado",
            description=f"**Usuario:** {usuario.mention}\n**Moderador:** {interaction.user.mention}",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed)
        
    except Exception as e:
        await interaction.response.send_message(f"❌ Error al desmutear: {str(e)}", ephemeral=True)

@bot.tree.command(name="infracciones", description="Muestra las infracciones de un usuario")
@app_commands.describe(usuario="Usuario a consultar")
@app_commands.default_permissions(manage_messages=True)
async def infracciones(interaction: discord.Interaction, usuario: discord.Member):
    """Sistema de consulta de infracciones con paginación"""
    if not await is_staff(interaction.user):
        return await interaction.response.send_message("❌ No tienes permisos para usar este comando.", ephemeral=True)
    
    total = await get_infractions(usuario.id, interaction.guild.id)
    db_cursor.execute('''
    SELECT motivo, fecha FROM infracciones 
    WHERE user_id = ? AND guild_id = ?
    ORDER BY fecha DESC LIMIT 5
    ''', (usuario.id, interaction.guild.id))
    infracciones = db_cursor.fetchall()
    
    embed = discord.Embed(
        title=f"📝 Infracciones de {usuario.display_name}",
        description=f"Total: **{total}** infracciones",
        color=discord.Color.orange()
    )
    
    for i, (motivo, fecha) in enumerate(infracciones, 1):
        fecha_obj = datetime.fromisoformat(fecha)
        embed.add_field(
            name=f"Infracción #{i} - {fecha_obj.strftime('%d/%m/%Y')}",
            value=f"**Motivo:** {motivo}",
            inline=False
        )
    
    if total > 5:
        embed.set_footer(text=f"Mostrando las 5 más recientes de {total} infracciones")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="limpiar_infracciones", description="Borra todas las infracciones de un usuario")
@app_commands.describe(usuario="Usuario a limpiar")
@app_commands.default_permissions(manage_messages=True)
async def limpiar_infracciones(interaction: discord.Interaction, usuario: discord.Member):
    """Comando para resetear el historial de infracciones de un usuario"""
    if not await is_staff(interaction.user):
        return await interaction.response.send_message("❌ No tienes permisos para usar este comando.", ephemeral=True)
    
    if usuario.top_role.position >= interaction.user.top_role.position:
        return await interaction.response.send_message("❌ No puedes limpiar infracciones de alguien con igual o mayor rango.", ephemeral=True)
    
    await clear_infractions(usuario.id, interaction.guild.id)
    
    embed = discord.Embed(
        title="🧹 Infracciones limpiadas",
        description=f"Se han eliminado todas las infracciones de {usuario.mention}",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed)

# Comandos de aplicación
@bot.tree.command(name="ticket", description="Crea un nuevo ticket de soporte")
async def ticket(interaction: discord.Interaction):
    await interaction.response.send_modal(TicketModal())

@bot.tree.command(name="modpanel", description="Muestra el panel de moderación")
@app_commands.default_permissions(manage_messages=True)
async def modpanel(interaction: discord.Interaction):
    if not await is_staff(interaction.user):
        return await interaction.response.send_message("❌ Solo el staff puede usar este comando.", ephemeral=True)
    
    embed = discord.Embed(
        title="🛠️ Panel de Moderación",
        description="Utiliza los botones para realizar acciones de moderación.",
        color=0x2b2d31
    )
    await interaction.response.send_message(
        embed=embed, 
        ephemeral=True
    )

@bot.tree.command(name="limpiar", description="Borra mensajes en el canal")
@app_commands.describe(cantidad="Número de mensajes a borrar (1-100)")
@app_commands.default_permissions(manage_messages=True)
async def limpiar(interaction: discord.Interaction, cantidad: int):
    if not await is_staff(interaction.user):
        return await interaction.response.send_message("❌ Solo el staff puede usar este comando.", ephemeral=True)
    
    cantidad = min(100, max(1, cantidad))
    
    # Primero respondemos a la interacción
    await interaction.response.defer(ephemeral=True)
    
    # Luego purgamos los mensajes
    deleted = await interaction.channel.purge(limit=cantidad)
    
    # Finalmente enviamos la confirmación
    await interaction.followup.send(
        f"🧹 Se borraron {len(deleted)} mensajes.",
        ephemeral=True
    )



# --------------------------
# Eventos
# --------------------------

@bot.event
async def on_voice_state_update(member, before, after):
    if member != bot.user:
        return
    
    if before.channel and not after.channel:
        music_queue.clear(before.channel.guild.id)
    elif before.channel and after.channel and before.channel != after.channel:
        await after.channel.send("🔊 Me han movido a este canal de voz")

@bot.event
async def on_ready():
    bot.add_view(TicketView())
    await bot.tree.sync()
    print(f"✅ Bot listo como {bot.user}")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.listening,
        name="!help"
    ))

# --------------------------
# Ejecución del Bot
# --------------------------

bot.run(os.getenv("TOKEN"))
bot.run(os.getenv("TOKEN"))
