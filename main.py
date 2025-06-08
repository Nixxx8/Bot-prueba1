import discord
from discord import app_commands, ui
from discord.ext import commands
import os
import asyncio
from dotenv import load_dotenv
from datetime import datetime
import sqlite3

# Configuración inicial
load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

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
async def agregar_infraccion(user_id: int, guild_id: int, motivo: str):
    cursor.execute('''
    INSERT INTO infracciones (user_id, guild_id, motivo, fecha)
    VALUES (?, ?, ?, ?)
    ''', (user_id, guild_id, motivo, datetime.now().isoformat()))
    conn.commit()

async def obtener_infracciones(user_id: int, guild_id: int) -> int:
    cursor.execute('''
    SELECT COUNT(*) FROM infracciones 
    WHERE user_id = ? AND guild_id = ?
    ''', (user_id, guild_id))
    return cursor.fetchone()[0]

async def limpiar_infracciones(user_id: int, guild_id: int):
    cursor.execute('''
    DELETE FROM infracciones 
    WHERE user_id = ? AND guild_id = ?
    ''', (user_id, guild_id))
    conn.commit()

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

# Panel de Moderación
class ModPanelView(ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=None)
        self.author_id = author_id
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id or not await is_staff(interaction.user):
            await interaction.response.send_message("❌ No tienes permiso para usar este panel.", ephemeral=True)
            return False
        return True
    
    @ui.button(label="⚠️ Advertencia", style=discord.ButtonStyle.grey, custom_id="mod:warn")
    async def warn_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(WarnModal())
    
    @ui.button(label="🔇 Mute", style=discord.ButtonStyle.blurple, custom_id="mod:mute")
    async def mute_button(self, interaction: discord.Interaction, button: ui.Button):
        view = ui.View()
        view.add_item(MuteUserDropdown())
        await interaction.response.send_message(
            "Selecciona un usuario para mutear:",
            view=view,
            ephemeral=True
        )
    
    @ui.button(label="🔨 Ban", style=discord.ButtonStyle.red, custom_id="mod:ban")
    async def ban_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BanModal())
    
    @ui.button(label="🧹 Limpiar", style=discord.ButtonStyle.green, custom_id="mod:clear")
    async def clear_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(ClearModal())
    
    @ui.button(label="🔊 Desmutear", style=discord.ButtonStyle.blurple, custom_id="mod:unmute")
    async def unmute_button(self, interaction: discord.Interaction, button: ui.Button):
        view = ui.View()
        view.add_item(UnmuteUserDropdown())
        await interaction.response.send_message(
            "Selecciona un usuario para desmutear:",
            view=view,
            ephemeral=True
        )

# Componentes de moderación
class MuteUserDropdown(ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Selecciona un usuario...", custom_id="mod:mute_user")

    async def callback(self, interaction: discord.Interaction):
        view = ui.View()
        view.add_item(MuteDurationDropdown(self.values[0].id))
        await interaction.response.edit_message(
            content=f"Selecciona duración para {self.values[0].mention}:",
            view=view
        )

class MuteDurationDropdown(ui.Select):
    def __init__(self, user_id: int):
        options = [
            discord.SelectOption(label="5 minutos", value="300"),
            discord.SelectOption(label="1 hora", value="3600"),
            discord.SelectOption(label="1 día", value="86400"),
            discord.SelectOption(label="1 semana", value="604800")
        ]
        super().__init__(
            placeholder="Selecciona duración...",
            custom_id=f"mod:mute_duration:{user_id}",
            options=options
        )
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        user = interaction.guild.get_member(self.user_id)
        if not user:
            return await interaction.response.send_message("❌ Usuario no encontrado.", ephemeral=True)
        
        mute_role = discord.utils.get(interaction.guild.roles, name=MUTE_ROLE_NAME)
        if not mute_role:
            return await interaction.response.send_message(f"❌ No hay un rol '{MUTE_ROLE_NAME}' configurado.", ephemeral=True)
        
        try:
            await user.add_roles(mute_role)
            duration = int(self.values[0])
            
            embed = discord.Embed(
                title="🔇 Usuario muteado",
                description=f"**Usuario:** {user.mention}\n**Duración:** {self.options[0].label}",
                color=0x7289da
            )
            await interaction.response.send_message(embed=embed)
            
            await asyncio.sleep(duration)
            await user.remove_roles(mute_role)
            
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

class UnmuteUserDropdown(ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Selecciona un usuario...", custom_id="mod:unmute_user")

    async def callback(self, interaction: discord.Interaction):
        user = self.values[0]
        mute_role = discord.utils.get(interaction.guild.roles, name=MUTE_ROLE_NAME)
        
        if not mute_role:
            return await interaction.response.send_message(f"❌ No hay un rol '{MUTE_ROLE_NAME}' configurado.", ephemeral=True)
        
        if mute_role not in user.roles:
            return await interaction.response.send_message(f"❌ {user.mention} no está muteado.", ephemeral=True)
        
        try:
            await user.remove_roles(mute_role)
            embed = discord.Embed(
                title="🔊 Usuario desmuteado",
                description=f"Se ha removido el mute de {user.mention}",
                color=0x00ff00
            )
            await interaction.response.send_message(embed=embed)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

# Modales de moderación
class WarnModal(ui.Modal, title="Advertencia"):
    user = ui.TextInput(label="ID o Mención del Usuario", custom_id="warn_user")
    reason = ui.TextInput(label="Motivo", style=discord.TextStyle.paragraph, custom_id="warn_reason")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            user = await commands.MemberConverter().convert(interaction, self.user.value)
            await agregar_infraccion(user.id, interaction.guild.id, self.reason.value)
            
            embed = discord.Embed(
                title="⚠️ Advertencia Registrada",
                description=f"**Usuario:** {user.mention}\n**Motivo:** {self.reason.value}",
                color=0xffcc00
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
            try:
                await user.send(f"⚠️ Has recibido una advertencia en **{interaction.guild.name}**:\n**Motivo:** {self.reason.value}")
            except discord.HTTPException:
                pass
            
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

class BanModal(ui.Modal, title="Banear Usuario"):
    user = ui.TextInput(label="ID o Mención del Usuario", custom_id="ban_user")
    reason = ui.TextInput(label="Motivo", style=discord.TextStyle.paragraph, custom_id="ban_reason")
    delete_days = ui.TextInput(label="Borrar mensajes (0-7 días)", default="0", required=False, custom_id="ban_days")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            user = await commands.MemberConverter().convert(interaction, self.user.value)
            delete_days = min(7, max(0, int(self.delete_days.value or "0")))
            
            await user.ban(
                reason=self.reason.value,
                delete_message_days=delete_days
            )
            
            embed = discord.Embed(
                title="🔨 Usuario baneado",
                description=f"**Usuario:** {user.mention}\n**Motivo:** {self.reason.value}\n**Mensajes borrados:** {delete_days} días",
                color=0xff0000
            )
            await interaction.response.send_message(embed=embed)
            
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

class ClearModal(ui.Modal, title="Limpiar Mensajes"):
    cantidad = ui.TextInput(label="Número de mensajes (1-100)", custom_id="clear_amount")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = min(100, max(1, int(self.cantidad.value)))
            await interaction.channel.purge(limit=amount)
            await interaction.response.send_message(
                f"🧹 Se borraron {amount} mensajes.",
                ephemeral=True
            )
        except ValueError:
            await interaction.response.send_message("❌ Por favor ingresa un número válido.", ephemeral=True)

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
        view=ModPanelView(interaction.user.id),
        ephemeral=True
    )

@bot.tree.command(name="limpiar", description="Borra mensajes en el canal")
@app_commands.describe(cantidad="Número de mensajes a borrar (1-100)")
@app_commands.default_permissions(manage_messages=True)
async def limpiar(interaction: discord.Interaction, cantidad: int):
    if not await is_staff(interaction.user):
        return await interaction.response.send_message("❌ Solo el staff puede usar este comando.", ephemeral=True)
    
    cantidad = min(100, max(1, cantidad))
    await interaction.channel.purge(limit=cantidad)
    await interaction.response.send_message(
        f"🧹 Se borraron {cantidad} mensajes.", 
        ephemeral=True
    )

@bot.tree.command(name="desmutear", description="Remueve el mute de un usuario")
@app_commands.describe(usuario="Usuario a desmutear")
@app_commands.default_permissions(manage_messages=True)
async def desmutear(interaction: discord.Interaction, usuario: discord.Member):
    if not await is_staff(interaction.user):
        return await interaction.response.send_message("❌ Solo el staff puede usar este comando.", ephemeral=True)
    
    mute_role = discord.utils.get(interaction.guild.roles, name=MUTE_ROLE_NAME)
    if not mute_role:
        return await interaction.response.send_message(f"❌ No hay un rol '{MUTE_ROLE_NAME}' configurado.", ephemeral=True)
    
    if mute_role not in usuario.roles:
        return await interaction.response.send_message(f"❌ {usuario.mention} no está muteado.", ephemeral=True)
    
    try:
        await usuario.remove_roles(mute_role)
        embed = discord.Embed(
            title="🔊 Usuario desmuteado",
            description=f"Se ha removido el mute de {usuario.mention}",
            color=0x00ff00
        )
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

# Eventos
@bot.event
async def on_ready():
    bot.add_view(TicketView())
    bot.add_view(ModPanelView(author_id=0))  # Vista base para persistencia
    await bot.tree.sync()
    print(f"✅ Bot listo como {bot.user}")

# Ejecutar el bot
bot.run(os.getenv("TOKEN"))
