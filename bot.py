#!/usr/bin/env python3

import traceback
import functools
import asyncio
import re
import shelve
import yaml
import random
from steam import WebAPI
from cachetools.keys import hashkey
from cachetools import TTLCache, cached
from telethon.errors.common import AlreadyInConversationError
from telethon import TelegramClient, events
import logging
logging.basicConfig(
    format='[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s', level=logging.WARNING)


# From https://github.com/tkem/cachetools/compare/wip/async

def cachedasync(cache, key=hashkey):
    """Decorator to wrap a coroutine function with a memorizing function
    that saves results in a cache.
    """
    def decorator(func):
        if cache is None:
            async def wrapper(*args, **kwargs):
                return await func(*args, **kwargs)
        else:
            async def wrapper(*args, **kwargs):
                k = key(*args, **kwargs)
                try:
                    return cache[k]
                except KeyError:
                    pass  # key not found
                v = await func(*args, **kwargs)
                try:
                    cache[k] = v
                except ValueError:
                    pass  # value too large
                return v
        return functools.update_wrapper(wrapper, func)
    return decorator


CONFIG_FILE = "config.yaml"
DB_FILE = "data.db"
CACHE_SIZE = 2048
CACHE_TTL = 259200
CACHE_SAVE_INTERNAL = 600
PARTY_TIMEOUT = 600

with open(CONFIG_FILE) as f:
    config = yaml.load(f, yaml.FullLoader)

TG_API_ID = config['tg_api_id']
TG_API_HASH = config['tg_api_hash']
TG_BOT_TOKEN = config['tg_bot_token']
STEAM_API_KEY = config['steam_api_key']

bot = TelegramClient('steam-party-bot', TG_API_ID,
                     TG_API_HASH).start(bot_token=TG_BOT_TOKEN)
db = shelve.open(DB_FILE)
steam_api = WebAPI(STEAM_API_KEY)
cache = db.get("cache", TTLCache(CACHE_SIZE, CACHE_TTL))
my_name = asyncio.get_event_loop().run_until_complete(bot.get_me()).username


def run_async(func, *args, **kwargs):
    return asyncio.get_running_loop().run_in_executor(None, functools.partial(func, *args, **kwargs))


@cachedasync(cache)
async def get_owned_games(steam_id):
    resp = await run_async(steam_api.call, 'IPlayerService.GetOwnedGames',
                           steamid=steam_id,
                           include_appinfo=True,
                           include_played_free_games=True,
                           appids_filter=[])
    return resp.get('response', None)


@bot.on(events.NewMessage(pattern=re.compile(rf'^/start(@{my_name})?$')))
async def start(event):
    await event.respond("Hi there. Steam Party Bot standby.")


@bot.on(events.NewMessage(pattern=re.compile(rf'^/register(@{my_name})?')))
async def register(event):
    args = event.text.split()
    if len(args) < 2:
        await event.reply("Usage: /register <Your Steam Numerical ID>\n"
                          "You can find your numerical ID [here](https://steamdb.info/calculator/)")
        return
    steam_id = args[1]
    db[str(event.sender_id)] = steam_id
    db.sync()
    await event.reply("Your Steam ID has been registered.")


@bot.on(events.NewMessage(pattern=re.compile(rf'^/unregister(@{my_name})?$')))
async def unregister(event):
    if str(event.sender_id) not in db:
        await event.reply("You are not registered yet.")
        return
    del db[str(event.sender_id)]
    db.sync()
    await event.reply("Your Steam ID has been unregistered.")


@bot.on(events.NewMessage(pattern=re.compile(rf'^/flushCache(@{my_name})?$')))
async def flush_cache(event):
    cache.clear()
    await event.reply("Steam API cache flushed.")


async def my_games_impl(event, full):
    if str(event.sender_id) not in db:
        await event.reply("You have not registered!\n"
                          "Use /register to register and /unregister to unregister.")
        return
    steam_id = db[str(event.sender_id)]
    game_results = await get_owned_games(steam_id)
    if game_results is None:
        await event.reply("Error in accessing steam API")
        return
    game_list = [
        f'[{g["name"]}](https://store.steampowered.com/app/{g["appid"]}/)' for g in game_results.get('games', [])]
    game_names = "\n".join(game_list)
    game_count = game_results.get('game_count', 0)
    if game_count == 0:
        await event.reply("Can't find any of your games.\n"
                          "This could be a steam privacy issue.\n"
                          "Set your steam profile as public so we can see it.\n"
                          "If you recently changed your privacy settings,"
                          " you may need to /flushCache before try again.")
        return
    msg = f"List of games owned:(Total: {game_count})\n{game_names}"
    if len(msg) > 4096:
        if full:
            msg = f"You have too many games, {game_count} in total.\n"\
                "You certainly don't have a life."
        else:
            msg = f"You have too many games, {game_count} in total.\n"\
                "You certainly don't have a life.\n"\
                "Displaying 10 random games only\n"\
                "Use /myGamesFull for all"
            game_list = random.sample(game_list, 10)
        await event.reply(msg)
        for m in truncate_msg(game_list):
            await event.reply(m)
    else:
        await event.reply(msg)


@bot.on(events.NewMessage(pattern=re.compile(rf'^/myGames(@{my_name})?$')))
async def my_games(event):
    return await my_games_impl(event, False)


@bot.on(events.NewMessage(pattern=re.compile(rf'^/myGamesFull(@{my_name})?$')))
async def my_games_full(event):
    return await my_games_impl(event, True)


async def generate_report(party_members):
    party_members = list(party_members)
    steam_ids = map(db.get, map(str, party_members))
    game_infos = await asyncio.gather(*[get_owned_games(i) for i in steam_ids])
    game_stat_dict = {}
    for member_id, game_info in zip(party_members, game_infos):
        for game in game_info.get('games', []):
            appid = game['appid']
            name = game['name']
            if appid not in game_stat_dict:
                game_stat_dict[appid] = {
                    "name": name,
                    "owners": {member_id}
                }
            else:
                game_stat_dict[appid]["owners"].add(member_id)
    report = [(g, game_stat_dict[g], len(game_stat_dict[g]["owners"]))
              for g in game_stat_dict]
    report = sorted(report, key=lambda x: x[2], reverse=True)
    return report

# Do not preserve order


async def parse_ids(list_of_ats):
    name_ats = [a for a in list_of_ats if a.startswith("@")]
    name_ats = await asyncio.gather(*[bot.get_peer_id(a[1:]) for a in name_ats])

    id_ats = [a for a in list_of_ats if not a.startswith("@")]
    id_ats = [re.match(r'^\[.*\]\(tg:\/\/user\?id=(\d+)\)$', a)
              for a in id_ats]
    id_ats = [int(a.group(1)) for a in id_ats if a]
    return name_ats + id_ats


def get_display_name(user):
    if user.first_name:
        display_name = user.first_name
        if user.last_name:
            display_name += " " + user.last_name
    else:
        if user.last_name:
            display_name = user.last_name
        else:
            display_name = user.username
    return display_name


@bot.on(events.NewMessage(pattern=re.compile(rf'^/party(@{my_name})?$')))
async def party(event):
    try:
        async with bot.conversation(await event.get_input_chat(), timeout=PARTY_TIMEOUT) as conv:
            party_msg = await conv.send_message("Here comes a new party! Let's find some common games we have.\n\n"
                                                "/join to join party.\n"
                                                "/leave to leave party\n"
                                                "/add <list of at's> to add people to party\n"
                                                "/kick <list of at's> to kick people from party\n"
                                                "/members to show current members\n"
                                                "/games [Number of difference tolerance] to find common games\n"
                                                "/stop to stop party")
            party_members = set()
            try:
                while True:
                    reply = await conv.get_response(party_msg)

                    if reply.text == '/join' or reply.text == f'/join@{my_name}':
                        if reply.sender_id in party_members:
                            await reply.reply("You are already in the party!")
                            continue
                        if str(reply.sender_id) not in db:
                            await reply.reply("You have not registered!\n"
                                              "Use /register to register and /unregister to unregister.")
                            return
                        party_members.add(reply.sender_id)
                        await reply.reply("You are in the party now!")
                        continue

                    elif reply.text == '/leave' or reply.text == f'/leave@{my_name}':
                        if reply.sender_id not in party_members:
                            await reply.reply("You are not in the party!")
                            continue
                        party_members.remove(reply.sender_id)
                        await reply.reply("You are not in the party now!")
                        continue

                    elif reply.text.startswith("/add") or reply.text.startswith(f'/add@{my_name}'):
                        users = await parse_ids(reply.text.split()[1:])
                        if len(users) == 0:
                            await reply.reply("Usage: /add <at's of users>")
                            continue
                        added = 0
                        for uid in users:
                            if uid in party_members or str(uid) not in db:
                                continue
                            party_members.add(uid)
                            added += 1
                        await reply.reply(f"Added {added} users")

                    elif reply.text.startswith("/kick") or reply.text.startswith(f'/kick@{my_name}'):
                        users = await parse_ids(reply.text.split()[1:])
                        if len(users) == 0:
                            await reply.reply("Usage: /kick <at's of users>")
                            continue
                        kicked = 0
                        for uid in users:
                            if uid not in party_members:
                                continue
                            party_members.remove(uid)
                            kicked += 1
                        await reply.reply(f"Kicked {kicked} users")

                    elif reply.text == '/members' or reply.text == f'/members@{my_name}':
                        users = await asyncio.gather(*[bot.get_entity(u) for u in party_members])
                        names = [f'{get_display_name(u)}' for u in users]
                        names = "\n".join(names)
                        names = f'Members in Party:(total: {len(users)})\n{names}'
                        await reply.reply(names)
                        continue

                    elif reply.text.startswith("/games") or reply.text.startswith(f'/games@{my_name}'):
                        args = reply.text.split()[1:]
                        if len(args) < 1:
                            tolerance = 0
                        else:
                            tolerance = convert_to_int(args[0])
                        threshold = len(party_members) - tolerance
                        report = await generate_report(party_members)
                        report = [r for r in report if r[2] >= threshold]
                        report = [
                            f'{r[2]}: [{r[1]["name"]}](https://store.steampowered.com/app/{r[0]}/)' for r in report]
                        if not report:
                            await reply.reply("No common games found!")
                            continue
                        for msg in truncate_msg(report):
                            await reply.reply(msg)

                    elif reply.text == '/stop' or reply.text == f'/stop@{my_name}':
                        await reply.reply("Party now ends.")
                        break
            except (asyncio.TimeoutError, asyncio.CancelledError, ValueError):
                logging.getLogger().warning(traceback.format_exc())
            finally:
                await party_msg.edit("Party is no longer active.")
    except AlreadyInConversationError:
        await event.reply("This chat already has an running party!")


async def save_cache():
    while True:
        db['cache'] = cache
        db.sync()
        await asyncio.sleep(CACHE_SAVE_INTERNAL)


def truncate_msg(lines, length=4096):
    msg = ""
    for line in lines:
        if len(line + "\n") > length:
            raise Exception("Line too large to truncate")
        if len(msg + line + "\n") < length:
            msg += line + "\n"
        else:
            yield msg
            msg = line + "\n"
    yield msg


def convert_to_int(s):
    try:
        return int(s)
    except ValueError:
        return 0


try:
    asyncio.get_event_loop().create_task(save_cache())
    bot.run_until_disconnected()
finally:
    db['cache'] = cache
    db.close()
