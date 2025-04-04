# Copyright (C) 2018 - 2020 MrYacha. All rights reserved. Source code available under the AGPL.
# Copyright (C) 2021 TeamAfrozi
# Copyright (C) 2020 Inuka Asith

# This file is part of Daisy (Telegram Bot)

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import pickle
from dataclasses import dataclass
from typing import Optional

from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.dispatcher.handler import CancelHandler
from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.types import ChatType, InlineKeyboardMarkup
from aiogram.types.callback_query import CallbackQuery
from aiogram.types.inline_keyboard import InlineKeyboardButton
from aiogram.types.message import ContentType, Message
from aiogram.utils.callback_data import CallbackData
from babel.dates import format_timedelta

from Afrozi import dp
from Afrozi.decorator import register
from Afrozi.modules.utils.connections import chat_connection
from Afrozi.modules.utils.language import get_strings, get_strings_dec
from Afrozi.modules.utils.message import (
    InvalidTimeUnit,
    convert_time,
    get_args,
    need_args_dec,
)
from Afrozi.modules.utils.restrictions import ban_user, kick_user, mute_user
from Afrozi.modules.utils.user_details import get_user_link, is_user_admin
from Afrozi.services.mongo import db
from Afrozi.services.redis import bredis, redis
from Afrozi.utils.cached import cached
from Afrozi.utils.logger import log

cancel_state = CallbackData("cancel_state", "user_id")


class AntiFloodConfigState(StatesGroup):
    expiration_proc = State()


class AntiFloodActionState(StatesGroup):
    set_time_proc = State()


@dataclass
class CacheModel:
    count: int


class AntifloodEnforcer(BaseMiddleware):
    state_cache_key = "floodstate:{chat_id}"

    async def enforcer(self, message: Message, database: dict):
        if (not (data := self.get_flood(message))) or int(
            self.get_state(message)
        ) != message.from_user.id:
            to_set = CacheModel(count=1)
            self.insert_flood(to_set, message, database)
            self.set_state(message)
            return False  # we aint banning anybody

        # update count
        data.count += 1

        # check exceeding
        if data.count >= database["count"]:
            if await self.do_action(message, database):
                self.reset_flood(message)
                return True

        self.insert_flood(data, message, database)
        return False

    @classmethod
    def is_message_valid(cls, message) -> bool:
        _pre = [ContentType.NEW_CHAT_MEMBERS, ContentType.LEFT_CHAT_MEMBER]
        if message.content_type in _pre:
            return False
        elif message.chat.type in (ChatType.PRIVATE,):
            return False
        return True

    def get_flood(self, message) -> Optional[CacheModel]:
        if data := bredis.get(self.cache_key(message)):
            data = pickle.loads(data)
            return data
        return None

    def insert_flood(self, data: CacheModel, message: Message, database: dict):
        ex = (
            convert_time(database["time"])
            if database.get("time", None) is not None
            else None
        )
        return bredis.set(self.cache_key(message), pickle.dumps(data), ex=ex)

    def reset_flood(self, message):
        return bredis.delete(self.cache_key(message))

    def check_flood(self, message):
        return bredis.exists(self.cache_key(message))

    def set_state(self, message: Message):
        return bredis.set(
            self.state_cache_key.format(chat_id=message.chat.id), message.from_user.id
        )

    def get_state(self, message: Message):
        return bredis.get(self.state_cache_key.format(chat_id=message.chat.id))

    @classmethod
    def cache_key(cls, message: Message):
        return f"antiflood:{message.chat.id}:{message.from_user.id}"

    @classmethod
    async def do_action(cls, message: Message, database: dict):
        action = database["action"] if "action" in database else "ban"

        if action == "ban":
            return await ban_user(message.chat.id, message.from_user.id)
        elif action == "kick":
            return await kick_user(message.chat.id, message.from_user.id)
        elif action == "mute":
            return await mute_user(message.chat.id, message.from_user.id)
        elif action.startswith("t"):
            time = database.get("time", None)
            if not time:
                return False
            if action == "tmute":
                return await mute_user(
                    message.chat.id, message.from_user.id, until_date=convert_time(time)
                )
            elif action == "tban":
                return await ban_user(
                    message.chat.id, message.from_user.id, until_date=convert_time(time)
                )
        else:
            return False

    async def on_pre_process_message(self, message: Message, _):
        log.debug(
            f"Enforcing flood control on {message.from_user.id} in {message.chat.id}"
        )
        if self.is_message_valid(message):
            if await is_user_admin(message.chat.id, message.from_user.id):
                return self.set_state(message)
            if (database := await get_data(message.chat.id)) is None:
                return

            if await self.enforcer(message, database):
                await message.delete()
                strings = await get_strings(message.chat.id, "antiflood")
                await message.answer(
                    strings["flood_exceeded"].format(
                        action=(
                            strings[database["action"]]
                            if "action" in database
                            else "banned"
                        ).capitalize(),
                        user=await get_user_link(message.from_user.id),
                    )
                )
                raise CancelHandler


@register(
    cmds=["setflood"], user_can_restrict_members=True, bot_can_restrict_members=True
)
@need_args_dec()
@chat_connection()
@get_strings_dec("antiflood")
async def setflood_command(message: Message, chat: dict, strings: dict):
    try:
        args = int(get_args(message)[0])
    except ValueError:
        return await message.reply(strings["invalid_args:setflood"])
    if args > 200:
        return await message.reply(strings["overflowed_count"])

    await AntiFloodConfigState.expiration_proc.set()
    redis.set(f"antiflood_setup:{chat['chat_id']}", args)
    await message.reply(
        strings["config_proc_1"],
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton(
                text=strings["cancel"],
                callback_data=cancel_state.new(user_id=message.from_user.id),
            )
        ),
    )


@register(
    state=AntiFloodConfigState.expiration_proc,
    content_types=ContentType.TEXT,
    allow_kwargs=True,
)
@chat_connection()
@get_strings_dec("antiflood")
async def antiflood_expire_proc(
    message: Message, chat: dict, strings: dict, state, **_
):
    try:
        if (time := message.text) not in (0, "0"):
            parsed_time = convert_time(time)  # just call for making sure its valid
        else:
            time, parsed_time = None, None
    except (TypeError, ValueError):
        await message.reply(strings["invalid_time"])
    else:
        if not (data := redis.get(f'antiflood_setup:{chat["chat_id"]}')):
            await message.reply(strings["setup_corrupted"])
        else:
            await db.antiflood.update_one(
                {"chat_id": chat["chat_id"]},
                {"$set": {"time": time, "count": int(data)}},
                upsert=True,
            )
            await get_data.reset_cache(chat["chat_id"])
            kw = {"count": data}
            if time is not None:
                kw.update(
                    {
                        "time": format_timedelta(
                            parsed_time, locale=strings["language_info"]["babel"]
                        )
                    }
                )
            await message.reply(
                strings[
                    "setup_success" if time is not None else "setup_success:no_exp"
                ].format(**kw)
            )
    finally:
        await state.finish()


@register(cmds=["antiflood", "flood"], is_admin=True)
@chat_connection(admin=True)
@get_strings_dec("antiflood")
async def antiflood(message: Message, chat: dict, strings: dict):
    if not (data := await get_data(chat["chat_id"])):
        return await message.reply(strings["not_configured"])

    if message.get_args().lower() in ("off", "0", "no"):
        await db.antiflood.delete_one({"chat_id": chat["chat_id"]})
        await get_data.reset_cache(chat["chat_id"])
        return await message.reply(
            strings["turned_off"].format(chat_title=chat["chat_title"])
        )

    if data["time"] is None:
        return await message.reply(
            strings["configuration_info"].format(
                action=strings[data["action"]] if "action" in data else strings["ban"],
                count=data["count"],
            )
        )
    return await message.reply(
        strings["configuration_info:with_time"].format(
            action=strings[data["action"]] if "action" in data else strings["ban"],
            count=data["count"],
            time=format_timedelta(
                convert_time(data["time"]), locale=strings["language_info"]["babel"]
            ),
        )
    )


@register(cmds=["setfloodaction"], user_can_restrict_members=True)
@need_args_dec()
@chat_connection(admin=True)
@get_strings_dec("antiflood")
async def setfloodaction(message: Message, chat: dict, strings: dict):
    SUPPORTED_ACTIONS = ["kick", "ban", "mute", "tmute", "tban"]  # noqa
    if (action := message.get_args().lower()) not in SUPPORTED_ACTIONS:
        return await message.reply(
            strings["invalid_args"].format(
                supported_actions=", ".join(SUPPORTED_ACTIONS)
            )
        )

    if action.startswith("t"):
        await message.reply(
            "Send a time for t action", allow_sending_without_reply=True
        )
        redis.set(f"floodactionstate:{chat['chat_id']}", action)
        return await AntiFloodActionState.set_time_proc.set()

    await db.antiflood.update_one(
        {"chat_id": chat["chat_id"]}, {"$set": {"action": action}}, upsert=True
    )
    await get_data.reset_cache(message.chat.id)
    return await message.reply(strings["setfloodaction_success"].format(action=action))


@register(
    state=AntiFloodActionState.set_time_proc,
    user_can_restrict_members=True,
    allow_kwargs=True,
)
@chat_connection(admin=True)
@get_strings_dec("antiflood")
async def set_time_config(
    message: Message, chat: dict, strings: dict, state: FSMContext, **_
):
    if not (action := redis.get(f"floodactionstate:{chat['chat_id']}")):
        await message.reply("setup_corrupted", allow_sending_without_reply=True)
        return await state.finish()
    try:
        parsed_time = convert_time(
            time := message.text.lower()
        )  # just call for making sure its valid
    except (TypeError, ValueError, InvalidTimeUnit):
        await message.reply("Invalid time")
    else:
        await db.antiflood.update_one(
            {"chat_id": chat["chat_id"]},
            {"$set": {"action": action, "time": time}},
            upsert=True,
        )
        await get_data.reset_cache(chat["chat_id"])
        text = strings["setfloodaction_success"].format(action=action)
        text += f" ({format_timedelta(parsed_time, locale=strings['language_info']['babel'])})"
        await message.reply(text, allow_sending_without_reply=True)
    finally:
        await state.finish()


async def __before_serving__(_):
    dp.middleware.setup(AntifloodEnforcer())


@register(cancel_state.filter(), f="cb")
async def cancel_state_cb(event: CallbackQuery):
    await event.message.delete()


@cached()
async def get_data(chat_id: int):
    return await db.antiflood.find_one({"chat_id": chat_id})


async def __export__(chat_id: int):
    data = await get_data(chat_id)
    if not data:
        return

    del data["_id"], data["chat_id"]
    return data


async def __import__(chat_id: int, data: dict):  # noqa
    await db.antiflood.update_one({"chat_id": chat_id}, {"$set": data})


__mod_name__ = "AntiFlood"

__help__ = """
You know how sometimes, people join, send 100 messages, and ruin your chat? With antiflood, that happens no more!

Antiflood allows you to take action on users that send more than x messages in a row.

<b>Admins only:</b>
- /antiflood: Gives you current configuration of antiflood in the chat
- /antiflood off: Disables Antiflood
- /setflood (limit): Sets flood limit

Replace (limit) with any integer, should be less than 200. When setting up, Daisy would ask you to send expiration time, if you dont understand what this expiration time for? User who sends specified limit of messages consecutively within this TIME, would be kicked, banned whatever the action is. if you dont want this TIME, wants to take action against those who exceeds specified limit without mattering TIME INTERVAL between the messages. you can reply to question with 0

<b>Configuring the time:</b>
<code>2m</code> = 2 minutes
<code>2h</code> = 2 hours
<code>2d</code> = 2 days

<b>Example:</b>
Me: <code>/setflood 10</code>
Daisy: <code>Please send expiration time [...]</code>
Me: <code>5m</code> (5 minutes)
DONE!

- /setfloodaction (action): Sets the action to taken when user exceeds flood limit

<b>Currently supported actions:</b>
<code>ban</code>
<code>mute</code>
<code>kick</code>
<i>More soon™</i>
"""
