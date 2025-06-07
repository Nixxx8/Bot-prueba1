import discord
from discord import ui, app_commands 
from discord.ext import tasks
import sqlite3
import random
import re
import asyncio
from dotenv import load_dotenv
import os
from datetime import datetime, timedelta
from discord.ext import commands, tasks

# Cargar variables de entorno (token)
load_dotenv()

# Configuración del bot
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.members = True

client = commands.Bot(command_prefix="!", intents=intents, application_id=1380938572810555563)


    # Configuración
TICKET_CATEGORY_ID = 1380982177344520287  # Cambia esto por tu categoría
LOG_CHANNEL_ID = 1380983483660501042     # Canal para logs
STAFF_ROLE_ID = 1380930376343752704      # Rol que puede reclamar/cerrar tickets

class TicketButtons(ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # Timeout=None para que los botones no expiren

    @ui.button(label="Reclamar Ticket", style=discord.ButtonStyle.blurple, custom_id="claim_ticket")
    async def claim(self, interaction: discord.Interaction, button: ui.Button):
        if STAFF_ROLE_ID not in [role.id for role in interaction.user.roles]:
            return await interaction.response.send_message("❌ Solo el staff puede reclamar tickets.", ephemeral=True)
        
        await interaction.channel.send(f"🎫 Ticket reclamado por {interaction.user.mention}")
        button.disabled = True
        button.label = f"Reclamado por {interaction.user.name}"
        await interaction.message.edit(view=self)
        await interaction.response.defer()

    @ui.button(label="Cerrar Ticket", style=discord.ButtonStyle.red, custom_id="close_ticket")
    async def close(self, interaction: discord.Interaction, button: ui.Button):
        if STAFF_ROLE_ID not in [role.id for role in interaction.user.roles]:
            return await interaction.response.send_message("❌ Solo el staff puede cerrar tickets.", ephemeral=True)
        
        # Embed de confirmación
        embed = discord.Embed(
            title="⚠️ Confirmar cierre",
            description="¿Estás seguro de cerrar este ticket?",
            color=discord.Color.orange()
        )
        
        # Botones de confirmación
        confirm_view = ui.View()
        confirm_view.add_item(ui.Button(style=discord.ButtonStyle.green, label="Sí", custom_id="confirm_close"))
        confirm_view.add_item(ui.Button(style=discord.ButtonStyle.red, label="Cancelar", custom_id="cancel_close"))
        
        await interaction.response.send_message(embed=embed, view=confirm_view, ephemeral=True)

class TicketModal(ui.Modal, title="Crear Ticket"):
    motivo = ui.TextInput(label="Motivo", style=discord.TextStyle.short)
    descripcion = ui.TextInput(label="Descripción", style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        category = interaction.guild.get_channel(TICKET_CATEGORY_ID)
        ticket_channel = await category.create_text_channel(
            f"ticket-{interaction.user.name}",
            topic=f"Motivo: {self.motivo}\nCreado por: {interaction.user}"
        )
        
        # Mensaje inicial con botones
        embed = discord.Embed(
            title=f"Ticket de {interaction.user.name}",
            description=f"**Motivo:** {self.motivo}\n**Descripción:** {self.descripcion}",
            color=discord.Color.blue()
        )
        
        await ticket_channel.send(
            content=f"{interaction.user.mention} | <@&{STAFF_ROLE_ID}>",
            embed=embed,
            view=TicketButtons()
        )
        
        # Registrar en logs
        log_channel = interaction.guild.get_channel(LOG_CHANNEL_ID)
        await log_channel.send(f"📝 Ticket creado: {ticket_channel.mention} por {interaction.user.mention}")
        
        await interaction.response.send_message(f"🎫 Ticket creado en {ticket_channel.mention}", ephemeral=True)

@client.tree.command(name="ticket", description="Crea un ticket de soporte")
async def ticket(interaction: discord.Interaction):
    await interaction.response.send_modal(TicketModal())

@client.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.data.get("custom_id") == "confirm_close":
        # Cerrar el ticket
        embed = discord.Embed(
            title="Ticket Cerrado",
            description=f"Cerrado por {interaction.user.mention}",
            color=discord.Color.red()
        )
        
        await interaction.channel.send(embed=embed)
        await interaction.channel.delete(reason="Ticket cerrado")
        
        # Registrar en logs
        log_channel = interaction.guild.get_channel(LOG_CHANNEL_ID)
        await log_channel.send(f"🔒 Ticket cerrado: #{interaction.channel.name} por {interaction.user.mention}")
        
        await interaction.response.defer()



client.run(os.getenv("TOKEN"))