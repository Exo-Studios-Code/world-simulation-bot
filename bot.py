#!/usr/bin/env python3

from __future__ import annotations
import os
import random
import math
import asyncio
import datetime as dt
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiosqlite

# grafika
from PIL import Image, ImageDraw, ImageFont

DB_PATH = os.getenv("WSIM_DB", "wsim.db")
DISCORD_TOKEN = os.getenv("zde token")
GUILD_ID = os.getenv("GUILD_ID")
TIMEZONE_OFFSET = 0

INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.guilds = True
INTENTS.message_content = False

# --- Balancování ---
START_POP = 1_000_000
START_GOLD = 150
START_ARMY = 12
START_TECH = 1
START_ECON = 60
START_HAPPY = 65

DAILY_INCOME_BASE = 10
INVEST_ECON_GAIN = (6, 14)
INVEST_INDUSTRY_GAIN = (5, 12)
INVEST_FINANCE_GAIN = (8, 18)
TRAIN_ARMY_GAIN = (4, 10)
RESEARCH_TECH_GAIN = (1, 3)
BUILD_INFRA_COST = 40
SPY_COST = 25

HAPPINESS_CLAMP = (0, 100)
ACTION_COOLDOWN_DAYS = 1
SEASON_LENGTH_DAYS = 30

IDEOLOGIES = {
    "demokracie": {"desc": "Vyšší štěstí a obchod.","happy_bonus": 6,"trade_bonus": 0.06},
    "diktatura": {"desc": "Silnější armáda, méně štěstí.","army_bonus": 0.12,"happy_bonus": -6},
    "teokracie": {"desc": "Stabilní populace.","pop_stability": 0.03},
    "technokracie": {"desc": "Rychlejší výzkum.","tech_bonus": 0.18},
}

REGIONS = [
    "Nordia", "Zelený poloostrov", "Prachové pláně", "Safírové pobřeží",
    "Železné hory", "Stínový les", "Lávové pole", "Mlžné údolí",
]

WORLD_EVENTS = [
    ("Piráti","Obchodní lodě byly napadeny piráty.", lambda: {"gold_pct": -0.05}),
    ("Úroda","Mimořádně dobrá úroda.", lambda: {"happy": +6, "gold_flat": +15}),
    ("Sopka","Sopka snížila plodiny v několika regionech.", lambda: {"pop_pct": -0.025}),
    ("Technický průlom","Nové poznatky – výzkum zrychlen.", lambda: {"tech_flat": +1}),
    ("Zlatá žíla","K nalezení bohatství – trhy kvetou.", lambda: {"gold_flat": +30}),
    ("Mor","Náhlé onemocnění zasáhlo určité populace.", lambda: {"pop_pct": -0.04, "happy": -5}),
    ("Mír","Dlouhé období klidu podpořilo obchod.", lambda: {"gold_pct": +0.08, "happy": +3}),
]

# --- DB ---
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS nations (
    user_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    region TEXT NOT NULL,
    population INTEGER NOT NULL,
    gold INTEGER NOT NULL,
    army INTEGER NOT NULL,
    tech INTEGER NOT NULL,
    economy INTEGER NOT NULL,
    happiness INTEGER NOT NULL,
    ideology TEXT,
    prestige INTEGER NOT NULL,
    last_action_day TEXT,
    season INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS diplomacy (
    a INTEGER NOT NULL,
    b INTEGER NOT NULL,
    type TEXT NOT NULL,
    UNIQUE(a, b, type)
);

CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seasons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_day TEXT NOT NULL,
    end_day TEXT,
    note TEXT
);
"""

# --- Helpers ---

def today_str() -> str:
    tz = dt.timezone(dt.timedelta(hours=TIMEZONE_OFFSET))
    return dt.datetime.now(tz=tz).date().isoformat()


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))

@dataclass
class Nation:
    user_id: int
    name: str
    region: str
    population: int
    gold: int
    army: int
    tech: int
    economy: int
    happiness: int
    ideology: Optional[str]
    prestige: int
    last_action_day: Optional[str]
    season: int = 0

    @property
    def power(self) -> int:
        tech_mult = 1 + (self.tech * 0.035)
        econ_mult = 1 + (self.economy * 0.0025)
        ideol = 1.0
        if self.ideology == "diktatura":
            ideol += IDEOLOGIES["diktatura"].get("army_bonus", 0)
        if self.ideology == "demokracie":
            ideol += IDEOLOGIES["demokracie"].get("trade_bonus", 0)
        return int(self.army * tech_mult * econ_mult * ideol)

    @property
    def income(self) -> int:
        base = DAILY_INCOME_BASE + int(self.economy * 0.25)
        if self.ideology == "demokracie":
            base = int(base * (1 + IDEOLOGIES["demokracie"].get("trade_bonus", 0)))
        return max(0, base)

# --- Bot ---
class WorldSim(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned_or("!"), intents=INTENTS)
        self.tree = app_commands.CommandTree(self)
        self.db: Optional[aiosqlite.Connection] = None
        self._guild: Optional[discord.Object] = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None
        self.world_tick_task = self.world_tick

    async def setup_hook(self) -> None:
        self.db = await aiosqlite.connect(DB_PATH)
        await self.db.executescript(CREATE_SQL)
        await self.db.commit()
        if self._guild:
            await self.tree.sync(guild=self._guild)
        else:
            await self.tree.sync()
        self.world_tick.start()

    async def close(self):
        if self.world_tick.is_running():
            self.world_tick.cancel()
        if self.db:
            await self.db.close()
        await super().close()

    # DB helpers
    async def get_nation(self, user_id: int) -> Optional[Nation]:
        assert self.db
        cur = await self.db.execute("SELECT user_id,name,region,population,gold,army,tech,economy,happiness,ideology,prestige,last_action_day,season FROM nations WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        await cur.close()
        if not row:
            return None
        return Nation(*row)

    async def upsert_nation(self, n: Nation) -> None:
        assert self.db
        await self.db.execute("REPLACE INTO nations (user_id,name,region,population,gold,army,tech,economy,happiness,ideology,prestige,last_action_day,season) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                              (n.user_id, n.name, n.region, n.population, n.gold, n.army, n.tech, n.economy, n.happiness, n.ideology, n.prestige, n.last_action_day, n.season))
        await self.db.commit()

    async def all_nations(self) -> List[Nation]:
        assert self.db
        cur = await self.db.execute("SELECT user_id,name,region,population,gold,army,tech,economy,happiness,ideology,prestige,last_action_day,season FROM nations")
        rows = await cur.fetchall()
        await cur.close()
        return [Nation(*r) for r in rows]

    async def add_event(self, title: str, description: str) -> None:
        assert self.db
        day = today_str()
        await self.db.execute("INSERT INTO event_log(day,title,description) VALUES(?,?,?)", (day, title, description))
        await self.db.commit()

    async def set_diplomacy(self, a: int, b: int, dtype: str, enable: bool) -> None:
        assert self.db
        if a > b:
            a, b = b, a
        if enable:
            await self.db.execute("INSERT OR IGNORE INTO diplomacy(a,b,type) VALUES(?,?,?)", (a, b, dtype))
        else:
            await self.db.execute("DELETE FROM diplomacy WHERE a=? AND b=? AND type=?", (a, b, dtype))
        await self.db.commit()

    async def get_diplomacy(self, a: int, b: int) -> List[str]:
        assert self.db
        if a > b:
            a, b = b, a
        cur = await self.db.execute("SELECT type FROM diplomacy WHERE a=? AND b=?", (a, b))
        rows = await cur.fetchall()
        await cur.close()
        return [r[0] for r in rows]

    # --- World tick / seasons ---
    @tasks.loop(hours=24)
    async def world_tick(self):
        await self.wait_until_ready()
        try:
            await self._do_world_tick()
        except Exception as e:
            print("World tick error:", e)

    async def _do_world_tick(self):
        nations = await self.all_nations()
        if not nations:
            return
        # season handling: pokud uplynulo SEASON_LENGTH_DAYS od posledni sezony, spustit end
        cur = await self.db.execute("SELECT id,start_day FROM seasons ORDER BY id DESC LIMIT 1")
        last = await cur.fetchone()
        await cur.close()
        season_age = None
        if last:
            sid, start = last
            start_date = dt.date.fromisoformat(start)
            season_age = (dt.date.today() - start_date).days
        else:
            # založit první sezonu
            await self.db.execute("INSERT INTO seasons(start_day,note) VALUES(?,?)", (today_str(), "Prvni sezona"))
            await self.db.commit()
            season_age = 0

        # světový event
        title, desc, effect_fn = random.choice(WORLD_EVENTS)
        effect = effect_fn()
        await self.add_event(title, desc)

        for n in nations:
            gold_delta = 0
            pop_delta = 0
            tech_delta = 0
            happy_delta = 0

            income = n.income
            gold_delta += income

            # regionální aplikace možných procentních efektů
            if "gold_pct" in effect:
                gold_delta += int(n.gold * effect["gold_pct"]) 
            if "gold_flat" in effect:
                gold_delta += int(effect["gold_flat"]) 
            if "pop_pct" in effect:
                change = int(n.population * effect["pop_pct"]) 
                if n.ideology == "teokracie":
                    change = int(change * (1 - IDEOLOGIES["teokracie"].get("pop_stability", 0)))
                pop_delta += change
            if "tech_flat" in effect:
                tech_delta += int(effect["tech_flat"])
            if "happy" in effect:
                happy_delta += int(effect["happy"])

            # season bonus: pokud je starší sezona -> menší bonusy, jinak odměna pro aktivní národy
            if season_age is not None and season_age < SEASON_LENGTH_DAYS:
                # malý bonus pro aktivitu - přidej prestiž
                n.prestige += 1

            # aplikace
            n.gold = max(0, n.gold + gold_delta)
            n.population = max(1000, n.population + pop_delta)
            n.tech = max(1, n.tech + tech_delta)
            n.happiness = clamp(n.happiness + happy_delta, *HAPPINESS_CLAMP)

            await self.upsert_nation(n)

        # poslat stručné oznámení do guild
        for guild in self.guilds:
            channel = guild.system_channel
            if not channel:
                for ch in guild.text_channels:
                    perms = ch.permissions_for(guild.me)
                    if perms.send_messages:
                        channel = ch
                        break
            if channel:
                try:
                    await channel.send(f"🌍 **Světová událost**: **{title}** — {desc} (Denní tick proveden.)")
                except Exception:
                    pass

    # --- Map generator ---
    def generate_map_image(self, nations: List[Nation]) -> str:
        # jednoduchá mapa: sloupce pro regiony, barva podle počtu obyvatel (tmavší = více)
        width = 1200
        height = 600
        img = Image.new("RGB", (width, height), (20, 20, 30))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 14)
        except Exception:
            font = ImageFont.load_default()

        cols = len(REGIONS)
        col_w = width // cols
        by_region: Dict[str, List[Nation]] = {r: [] for r in REGIONS}
        for n in nations:
            by_region.setdefault(n.region, []).append(n)

        for i, r in enumerate(REGIONS):
            x0 = i * col_w + 10
            x1 = x0 + col_w - 20
            y0 = 50
            y1 = height - 50
            owners = by_region.get(r, [])
            total_pop = sum(o.population for o in owners) if owners else 0
            # map color intensity
            intensity = int(40 + min(215, total_pop // 50000))
            color = (intensity, 100, 120)
            draw.rectangle([x0, y0, x1, y1], outline=(80,80,80), fill=color)
            draw.text((x0+8, y0-28), f"{r} ({len(owners)})", font=font, fill=(240,240,240))
            # vypiš prvnich par jmen
            for j, owner in enumerate(owners[:6]):
                draw.text((x0+8, y0 + 8 + j*18), f"- {owner.name} ({owner.population//1000}k)", font=font, fill=(230,230,230))

        # footer
        draw.text((10, height-30), f"WorldSim map — {today_str()}", font=font, fill=(200,200,200))
        os.makedirs("maps", exist_ok=True)
        path = f"maps/world_map_{today_str()}.png"
        img.save(path)
        return path

# --- Commands ---
bot = WorldSim()

def guild_kw() -> dict:
    return {"guild": bot._guild} if bot._guild else {}

async def ensure_nation(interaction: discord.Interaction) -> Optional[Nation]:
    n = await bot.get_nation(interaction.user.id)
    if not n:
        await interaction.response.send_message("Nemáš ještě založený národ. Použij **/create_nation**.", ephemeral=True)
        return None
    return n

def can_act_today(n: Nation) -> bool:
    return n.last_action_day != today_str()

async def stamp_action(n: Nation) -> None:
    n.last_action_day = today_str()
    await bot.upsert_nation(n)

@bot.tree.command(name="create_nation", description="Založ svůj národ.")
@app_commands.describe(name="Jméno tvého národa")
async def create_nation(interaction: discord.Interaction, name: str):
    if await bot.get_nation(interaction.user.id):
        await interaction.response.send_message("Už máš národ.", ephemeral=True)
        return
    region = random.choice(REGIONS)
    n = Nation(interaction.user.id, name[:40], region, START_POP, START_GOLD, START_ARMY, START_TECH, START_ECON, START_HAPPY, None, 0, None, 0)
    await bot.upsert_nation(n)
    await interaction.response.send_message(f"🇺🇳 Národ **{n.name}** založen v regionu **{n.region}**! Populace: {n.population:,} | Zlato: {n.gold} | Armáda: {n.army}")

@bot.tree.command(name="set_ideology", description="Zvol ideologii svého národa.")
@app_commands.choices(ideologie=[app_commands.Choice(name=k, value=k) for k in IDEOLOGIES.keys()])
async def set_ideology(interaction: discord.Interaction, ideologie: app_commands.Choice[str]):
    n = await ensure_nation(interaction)
    if not n: return
    n.ideology = ideologie.value
    await bot.upsert_nation(n)
    await interaction.response.send_message(f"🧭 Ideologie nastavena: **{n.ideology}** — {IDEOLOGIES[n.ideology]['desc']}")

@bot.tree.command(name="nation", description="Zobraz info o národě.")
async def nation(interaction: discord.Interaction, user: Optional[discord.User] = None):
    target = user or interaction.user
    n = await bot.get_nation(target.id)
    if not n:
        await interaction.response.send_message("Tento hráč nemá národ.", ephemeral=True)
        return
    allies = 0; trades = 0
    for other in await bot.all_nations():
        if other.user_id == n.user_id: continue
        rel = await bot.get_diplomacy(min(n.user_id, other.user_id), max(n.user_id, other.user_id))
        if "ally" in rel: allies += 1
        if "trade" in rel: trades += 1
    embed = discord.Embed(title=f"{n.name}", description=f"Region: {n.region}")
    embed.add_field(name="Populace", value=f"{n.population:,}")
    embed.add_field(name="Zlato", value=str(n.gold))
    embed.add_field(name="Armáda", value=f"{n.army} (síla {n.power})")
    embed.add_field(name="Technologie", value=str(n.tech))
    embed.add_field(name="Ekonomika", value=str(n.economy))
    embed.add_field(name="Štěstí", value=str(n.happiness))
    embed.add_field(name="Ideologie", value=str(n.ideology or "nenastavena"))
    embed.add_field(name="Prestiž", value=str(n.prestige))
    embed.set_footer(text=f"Spojenci: {allies} | Obchodní smlouvy: {trades}")
    await interaction.response.send_message(embed=embed)

# Rozšířené investiční příkazy: industry / finance / infrastructure
@bot.tree.command(name="invest", description="Investuj: industry | finance | infrastructure")
@app_commands.choices(type=[app_commands.Choice(name="industry", value="industry"), app_commands.Choice(name="finance", value="finance"), app_commands.Choice(name="infrastructure", value="infrastructure")])
async def invest(interaction: discord.Interaction, type: app_commands.Choice[str]):
    n = await ensure_nation(interaction)
    if not n: return
    if not can_act_today(n):
        await interaction.response.send_message("Už jsi dnes akci použil.", ephemeral=True)
        return
    if type.value == "industry":
        gain = random.randint(*INVEST_INDUSTRY_GAIN)
        n.economy += gain
        n.gold += max(0, int(gain*0.4))
        n.happiness = clamp(n.happiness + 1, *HAPPINESS_CLAMP)
        await stamp_action(n)
        await interaction.response.send_message(f"🏭 Investice do průmyslu: +{gain} ekonomika, +{int(gain*0.4)} zlata.")
    elif type.value == "finance":
        gain = random.randint(*INVEST_FINANCE_GAIN)
        n.economy += gain
        n.gold += max(0, int(gain*0.7))
        n.happiness = clamp(n.happiness + 2, *HAPPINESS_CLAMP)
        await stamp_action(n)
        await interaction.response.send_message(f"💰 Investice do financí: +{gain} ekonomika, +{int(gain*0.7)} zlata.")
    else:
        # infrastructure costs money but gives stable bonus
        if n.gold < BUILD_INFRA_COST:
            return await interaction.response.send_message(f"Potřebuješ {BUILD_INFRA_COST} zlata na vybudování infrastruktury.", ephemeral=True)
        n.gold -= BUILD_INFRA_COST
        n.economy += random.randint(6, 12)
        n.happiness = clamp(n.happiness + 3, *HAPPINESS_CLAMP)
        await stamp_action(n)
        await interaction.response.send_message(f"🛤️ Vybudovaná infrastruktura. Ekonomika + a štěstí +3.")

@bot.tree.command(name="train", description="Trénuj armádu.")
async def train(interaction: discord.Interaction):
    n = await ensure_nation(interaction)
    if not n: return
    if not can_act_today(n):
        return await interaction.response.send_message("Už jsi dnes akci použil.", ephemeral=True)
    base = random.randint(*TRAIN_ARMY_GAIN)
    if n.ideology == "diktatura":
        base = int(base * (1 + IDEOLOGIES["diktatura"].get("army_bonus", 0)))
        n.happiness = clamp(n.happiness - 2, *HAPPINESS_CLAMP)
    n.army += max(1, base)
    await stamp_action(n)
    await interaction.response.send_message(f"🛡️ Armáda posílena o +{max(1, base)}.")

@bot.tree.command(name="research", description="Výzkum technologií.")
async def research(interaction: discord.Interaction):
    n = await ensure_nation(interaction)
    if not n: return
    if not can_act_today(n):
        return await interaction.response.send_message("Už jsi dnes akci použil.", ephemeral=True)
    base = random.randint(*RESEARCH_TECH_GAIN)
    if n.ideology == "technokracie":
        base = math.ceil(base * (1 + IDEOLOGIES["technokracie"].get("tech_bonus", 0)))
    n.tech += base
    n.happiness = clamp(n.happiness + 1, *HAPPINESS_CLAMP)
    await stamp_action(n)
    await interaction.response.send_message(f"🔬 Technologie zvýšena o +{base}.")

@bot.tree.command(name="spy", description="Pošli špiona: zjistí info o cíli (cena).")
@app_commands.describe(target="Cílový hráč")
async def spy(interaction: discord.Interaction, target: discord.User):
    n = await ensure_nation(interaction)
    t = await bot.get_nation(target.id)
    if not n or not t:
        return await interaction.response.send_message("Oba hráči musí mít národ.", ephemeral=True)
    if n.gold < SPY_COST:
        return await interaction.response.send_message(f"Potřebuješ {SPY_COST} zlata na špionáž.", ephemeral=True)
    if not can_act_today(n):
        return await interaction.response.send_message("Už jsi dnes akci použil.", ephemeral=True)
    n.gold -= SPY_COST
    await stamp_action(n)
    # určitá šance, že špion bude odhalen
    if random.random() < 0.12:
        await interaction.response.send_message(f"🕵️‍♂️ Špion odhalen! Cíl byl upozorněn.")
        # cíl ztratí štěstí
        t.happiness = clamp(t.happiness - 3, *HAPPINESS_CLAMP)
        await bot.upsert_nation(t)
    else:
        # úspěch: pošli základní info
        info = f"{t.name}: pop {t.population:,}, zlato {t.gold}, armáda {t.army}, tech {t.tech}, econ {t.economy}"
        await interaction.response.send_message(f"🕵️‍♀️ Špionáž úspěšná: {info}")

def resolve_battle(attacker: Nation, defender: Nation) -> Tuple[str, Dict[str,int]]:
    a_power = attacker.power * random.uniform(0.82, 1.22)
    d_power = defender.power * random.uniform(0.85, 1.18)
    if attacker.ideology == "demokracie":
        a_power *= 0.98
    if defender.ideology == "diktatura":
        d_power *= 1.05
    if a_power > d_power:
        gold_steal = min(defender.gold, random.randint(15, 55))
        pop_loss = max(0, int(defender.population * 0.012))
        army_loss_def = max(1, int(defender.army * 0.08))
        army_loss_att = max(1, int(attacker.army * 0.04))
        return ("attacker",{"gold_steal":gold_steal,"pop_loss":pop_loss,"army_loss_def":army_loss_def,"army_loss_att":army_loss_att,"prestige":7})
    else:
        gold_steal = min(attacker.gold, random.randint(8, 30))
        pop_loss = max(0, int(attacker.population * 0.01))
        army_loss_att = max(1, int(attacker.army * 0.07))
        army_loss_def = max(1, int(defender.army * 0.04))
        return ("defender",{"gold_steal":gold_steal,"pop_loss":pop_loss,"army_loss_att":army_loss_att,"army_loss_def":army_loss_def,"prestige":5})

@bot.tree.command(name="war", description="Vyhlaš válku cíli a srovnej síly.")
async def war(interaction: discord.Interaction, target: discord.User):
    if target.id == interaction.user.id:
        return await interaction.response.send_message("Nemůžeš útočit sám na sebe.", ephemeral=True)
    attacker = await ensure_nation(interaction)
    defender = await bot.get_nation(target.id)
    if not attacker or not defender:
        return await interaction.response.send_message("Oba hráči musí mít národ.", ephemeral=True)
    rel = await bot.get_diplomacy(attacker.user_id, defender.user_id)
    trade_penalty = 0.6 if "trade" in rel else 1.0
    winner, data = resolve_battle(attacker, defender)
    if winner == "attacker":
        attacker.gold += int(data["gold_steal"]*trade_penalty)
        defender.gold -= int(data["gold_steal"]*trade_penalty)
        defender.population = max(1000, defender.population - data["pop_loss"]) 
        defender.army = max(1, defender.army - data["army_loss_def"]) 
        attacker.army = max(1, attacker.army - data["army_loss_att"]) 
        attacker.prestige += data["prestige"]
        msg = f"⚔️ {interaction.user.mention} zaútočil na {target.mention}. Útočník zvítězil a ukořistil {int(data['gold_steal']*trade_penalty)} zlata."
    else:
        defender.gold += int(data["gold_steal"]*trade_penalty)
        attacker.gold -= int(data["gold_steal"]*trade_penalty)
        attacker.population = max(1000, attacker.population - data["pop_loss"]) 
        attacker.army = max(1, attacker.army - data["army_loss_att"]) 
        defender.army = max(1, defender.army - data["army_loss_def"]) 
        defender.prestige += data["prestige"]
        msg = f"🛡️ {interaction.user.mention} zaútočil na {target.mention}. Obránce odrazil útok a získal {int(data['gold_steal']*trade_penalty)} zlata."
    await bot.upsert_nation(attacker)
    await bot.upsert_nation(defender)
    await interaction.response.send_message(msg)

@bot.tree.command(name="leaderboard", description="Žebříček prestiže.")
async def leaderboard(interaction: discord.Interaction):
    nations = await bot.all_nations()
    if not nations:
        return await interaction.response.send_message("Zatím žádné národy.")
    nations.sort(key=lambda x: x.prestige, reverse=True)
    lines = [
        f"**{i}.** {n.name} — prestiž {n.prestige} | síla {n.power} | tech {n.tech}"
        for i, n in enumerate(nations[:20], start=1)
    ]
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="eventlog", description="Poslední světové události.")
@app_commands.describe(days="Za kolik dnů zpětně (1-30)")
async def eventlog(interaction: discord.Interaction, days: app_commands.Range[int,1,30]=7):
    assert bot.db
    cur = await bot.db.execute(
        "SELECT day,title,description FROM event_log ORDER BY id DESC LIMIT ?",
        (days,)
    )
    rows = await cur.fetchall()
    await cur.close()

    if not rows:
        return await interaction.response.send_message("Žádné události zatím nejsou.")

    lines = [f"{d} — **{t}**: {desc}" for (d, t, desc) in rows]
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="map", description="Vygeneruj grafickou mapu (PNG).")
async def map_cmd(interaction: discord.Interaction):
    nations = await bot.all_nations()
    path = bot.generate_map_image(nations)
    await interaction.response.send_message(file=discord.File(path))

# Admin: end season
@bot.tree.command(name="season_end", description="[Admin] Ukonči právě běžící sezónu a vyhlaš odměny.")
@app_commands.checks.has_permissions(administrator=True)
async def season_end(interaction: discord.Interaction, note: Optional[str] = None):
    # uzavři sezonu
    assert bot.db
    cur = await bot.db.execute("SELECT id,start_day FROM seasons ORDER BY id DESC LIMIT 1")
    last = await cur.fetchone(); await cur.close()
    if not last:
        return await interaction.response.send_message("Žádná sezona nebyla nalezena.")
    sid, start = last
    await bot.db.execute("UPDATE seasons SET end_day=?, note=? WHERE id=?", (today_str(), note or "admin end", sid))
    # nova sezona
    await bot.db.execute("INSERT INTO seasons(start_day,note) VALUES(?,?)", (today_str(), "Nova sezona"))
    await bot.db.commit()
    # reward: top prestiz dostane zlato
    nations = await bot.all_nations()
    if nations:
        nations.sort(key=lambda x: x.prestige, reverse=True)
        top = nations[0]
        top.gold += 200
        await bot.upsert_nation(top)
    await bot.add_event("Konec sezóny", f"Sezóna ukončena. Vítěz: {top.name if nations else 'nikdo' }")
    await interaction.response.send_message("♻️ Sezóna ukončena a nová začala. Odměny rozeslány.")

@bot.tree.command(name="admin_give_gold", description="[Admin] Přidá zlato hráči.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(target="Cílový hráč", amount="Kolik zlata přidat")
async def admin_give_gold(interaction: discord.Interaction, target: discord.User, amount: app_commands.Range[int,1,1_000_000]):
    n = await bot.get_nation(target.id)
    if not n: return await interaction.response.send_message("Hráč nemá národ.", ephemeral=True)
    n.gold += amount; await bot.upsert_nation(n)
    await interaction.response.send_message(f"💰 {amount} zlata přidáno národu {n.name}.")

@bot.event
async def on_ready():
    print(f"Přihlášen jako {bot.user} (id: {bot.user.id})")
    if bot._guild: print(f"Syncnuté příkazy pro GUILD {bot._guild.id}")

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("Nastav DISCORD_TOKEN v proměnných prostředí!")
    bot.run(DISCORD_TOKEN)
