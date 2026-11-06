# World Simulation Bot

A Discord bot that turns your server into an endless world simulation. Every player runs a nation and competes for prestige through economy, technology, espionage and war.

## Features

- **Nations:** found a nation, pick an ideology and manage population, gold, army, technology, economy and happiness
- **Daily income** and balanced growth
- **Investments:** industry, finance and infrastructure
- **Army training and technology research**
- **Espionage:** send spies to learn about other nations
- **Wars:** declare war and resolve battles from relative strength
- **Random world events** with an event log
- **Seasons:** a new season every 30 days with bonuses, plus manual season end for admins
- **Generated world map:** PNG map drawn with Pillow
- **Prestige leaderboard**
- SQLite storage with persistent state

## Tech Stack

- Python 3.10+
- discord.py 2.x
- aiosqlite
- Pillow

## Getting Started

### Prerequisites

- Python 3.10+
- A Discord bot token

### Installation

```bash
git clone https://github.com/Exo-Studios-Code/world-simulation-bot.git
cd world-simulation-bot
pip install -r requirements.txt
```

### Configuration

Linux/macOS:

```bash
export DISCORD_TOKEN=your_bot_token
export GUILD_ID=your_server_id     # optional, instant command sync
export WSIM_DB=wsim.db             # optional, database path
```

Windows PowerShell:

```powershell
$env:DISCORD_TOKEN="your_bot_token"
$env:GUILD_ID="your_server_id"
```

### Run

```bash
python bot.py
```

## Commands

| Command | Description |
|---------|-------------|
| `/create_nation`, `/set_ideology`, `/nation` | Found and view your nation |
| `/invest`, `/train`, `/research` | Grow your economy, army and tech |
| `/spy`, `/war` | Espionage and warfare |
| `/leaderboard`, `/eventlog`, `/map` | Rankings, events and world map |
| `/season_end`, `/admin_give_gold` | Admin tools |
