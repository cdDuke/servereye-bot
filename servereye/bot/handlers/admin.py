"""👑 Админ-панель: пользователи, админы, заявки, приглашения, журнал.

Роли:
* владелец (ADMIN_IDS) — всё, включая выдачу админки;
* админ — заявки, приглашения, журнал, модерация обычных участников;
* участник — в панель не попадает.
Каждый обработчик заново проверяет роль нажавшего.
"""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommandScopeChat, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import repository as repo
from ...database.models import (
    ACCESS_ACTIVE,
    ACCESS_PENDING,
    INVITE_APPROVE,
    INVITE_INSTANT,
    ROLE_ADMIN,
    Invite,
    User,
)
from ...services import access
from ...services.notifier import Notifier
from ...utils import clean_name, esc, fmt_time, utcnow
from .. import texts
from ..keyboards import inline as kb
from .helpers import edit

log = logging.getLogger(__name__)
router = Router(name="admin")


class InviteNew(StatesGroup):
    note = State()


# ------------------------------------------------------------------- защита ---
async def _guard(call: CallbackQuery, session: AsyncSession) -> User | None:
    """Возвращает нажавшего, если он владелец или админ. Иначе — отказ и тревога."""
    actor = await access.staff_by_id(session, call.from_user.id)
    if actor is not None:
        return actor
    tg = call.from_user
    log.warning("Подделка админской кнопки: %s (%s) → %s", tg.id, tg.username, call.data)
    await call.answer("🔒 Нет доступа", show_alert=True)
    if not await access.recently_logged(session, tg.id, "admin_forgery", hours=1):
        await access.log_tg(session, tg, "admin_forgery", detail=(call.data or "")[:100])
        notifier = Notifier(call.bot)
        for staff_id in await access.staff_ids(session):
            await notifier.send(
                session, staff_id,
                "\n".join([
                    "🚨 <b>Попытка открыть админ-панель</b>",
                    "",
                    *texts.person_lines(tg),
                    "",
                    f"Кнопка: <code>{esc((call.data or '')[:60])}</code>",
                    "Пользователь без прав нажал админскую кнопку — "
                    "скорее всего, подделанную. Действие не выполнено.",
                ]),
                type_="access_alert", severity="critical", force=True,
                reply_markup=kb.access_alert_actions(tg.id),
            )
        await session.commit()
    return None


def _uid(call: CallbackQuery, index: int = 2) -> int | None:
    parts = (call.data or "").split(":")
    if len(parts) > index and parts[index].lstrip("-").isdigit():
        return int(parts[index])
    return None


async def _tell(bot: Bot, user_id: int, text: str, markup=None) -> None:
    try:
        await bot.send_message(user_id, text, reply_markup=markup)
    except Exception:  # noqa: BLE001 — человек мог заблокировать бота
        log.info("Не удалось написать пользователю %s", user_id)


async def _report_to_owner(session: AsyncSession, bot: Bot, actor: User, action: str) -> None:
    """Действия админов с людьми дублируются владельцу."""
    if access.is_owner(actor.id):
        return
    notifier = Notifier(bot)
    for owner_id in access.owner_ids():
        await notifier.send(
            session, owner_id,
            f"⭐ <b>Админ {esc(actor.display_name)}</b> (<code>{actor.id}</code>)\n{action}",
            type_="access_alert", severity="info", force=True,
            reply_markup=kb.back_main([("👑 Админ-панель", "adm:main")]),
        )
    await session.commit()


async def _set_commands(bot: Bot, user_id: int, with_admin: bool) -> None:
    from ...main import COMMANDS
    from aiogram.types import BotCommand

    try:
        if with_admin:
            await bot.set_my_commands(
                COMMANDS + [BotCommand(command="admin", description="👑 Админ-панель")],
                scope=BotCommandScopeChat(chat_id=user_id),
            )
        else:
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=user_id))
    except Exception:  # noqa: BLE001
        log.info("Не удалось обновить меню команд для %s", user_id)


def _who(user: User | None, uid: int) -> str:
    return f"{esc(user.display_name)} (<code>{uid}</code>)" if user else f"<code>{uid}</code>"


# ------------------------------------------------------------------ главная ---
async def _panel(session: AsyncSession, actor: User) -> tuple[str, object]:
    users = await access.list_users(session)
    pending = sum(1 for u in users if u.access_status == ACCESS_PENDING)
    invites = await access.count_active_invites(session)
    return (
        texts.admin_panel(users, pending, invites, access.is_owner(actor.id)),
        kb.admin_panel(pending),
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message, session: AsyncSession, state: FSMContext) -> None:
    actor = await access.staff_by_id(session, message.from_user.id)
    if actor is None:
        return  # для остальных команды не существует
    await state.clear()
    text, markup = await _panel(session, actor)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "adm:main")
async def panel(call: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    actor = await _guard(call, session)
    if actor is None:
        return
    await state.clear()
    text, markup = await _panel(session, actor)
    await edit(call, text, markup)
    await call.answer()


# ------------------------------------------------------------- пользователи ---
async def _users_view(call: CallbackQuery, session: AsyncSession) -> None:
    users = await access.list_users(session)
    await edit(call, texts.admin_users(users, access.owner_ids()), kb.admin_users(users))


@router.callback_query(F.data == "adm:users")
async def users_list(call: CallbackQuery, session: AsyncSession) -> None:
    if await _guard(call, session) is None:
        return
    await _users_view(call, session)
    await call.answer()


@router.callback_query(F.data == "adm:requests")
async def requests_list(call: CallbackQuery, session: AsyncSession) -> None:
    if await _guard(call, session) is None:
        return
    pending = await access.list_users(session, ACCESS_PENDING)
    if pending:
        text = "⏳ <b>Заявки на доступ</b>\n\n" + "\n".join(
            f"• {esc(u.display_name)} (<code>{u.id}</code>) · {fmt_time(u.access_changed_at, texts._TZ)}"
            for u in pending
        ) + "\n\nНажмите на человека, чтобы решить."
    else:
        text = "⏳ <b>Заявки на доступ</b>\n\nНовых заявок нет."
    await edit(call, text, kb.admin_users(pending))
    await call.answer()


async def _render_user(call: CallbackQuery, session: AsyncSession, actor: User, uid: int) -> None:
    user = await session.get(User, uid)
    if user is None:
        await edit(call, "Пользователь удалён или ещё не заходил.", kb.admin_users([]))
        return
    servers = await repo.count_servers(session, uid)
    invite = await session.get(Invite, user.invite_id) if user.invite_id else None
    await edit(
        call,
        texts.admin_user_card(user, servers, invite, access.is_owner(uid)),
        kb.admin_user(
            user,
            target_is_owner=access.is_owner(uid),
            can_moderate=access.can_moderate(actor, uid, user),
            can_set_admin=access.is_owner(actor.id) and user.access_status == ACCESS_ACTIVE,
        ),
    )


@router.callback_query(F.data.startswith("adm:user:"))
async def user_card(call: CallbackQuery, session: AsyncSession) -> None:
    actor = await _guard(call, session)
    if actor is None:
        return
    uid = _uid(call)
    if uid is None:
        await call.answer("Некорректный запрос", show_alert=True)
        return
    await _render_user(call, session, actor, uid)
    await call.answer()


async def _mark_notification(call: CallbackQuery, result_line: str) -> bool:
    """Если кнопку нажали прямо в уведомлении — дописываем в него итог."""
    message = call.message
    if message is None or not message.text:
        return False
    head = (message.html_text or "").split("\n")[0]
    if not head.startswith(("🔔", "⚠️", "🚫", "❓", "⌛️", "🔒", "🚨")):
        return False
    await edit(call, f"{message.html_text}\n\n{result_line}", None)
    return True


@router.callback_query(F.data.startswith("adm:approve:") | F.data.startswith("adm:deny:"))
async def decide(call: CallbackQuery, session: AsyncSession) -> None:
    actor = await _guard(call, session)
    if actor is None:
        return
    uid = _uid(call)
    approve = call.data.startswith("adm:approve:")
    user = await access.decide_request(session, actor.id, uid, approve) if uid else None
    if user is None:
        await call.answer("Заявка уже обработана или нет прав", show_alert=True)
        if uid is not None:
            await _render_user(call, session, actor, uid)
        return
    await session.commit()
    stamp = fmt_time(utcnow(), texts._TZ)
    by = "вами" if access.is_owner(actor.id) else f"админом {esc(actor.display_name)}"
    if approve:
        await _tell(
            call.bot, uid,
            "✅ <b>Доступ открыт!</b>\n\nВам подтвердили заявку. Добро пожаловать в ServerEye.\n\n"
            "Серверы в боте общие: откройте «🖥 Мои серверы» — там уже есть серверы владельца.",
            kb.main_menu(),
        )
        line = f"✅ <b>Пущен(-а) {by} в {stamp}</b>"
        await _report_to_owner(session, call.bot, actor, f"✅ пустил(-а) {_who(user, uid)}")
        await call.answer("Доступ выдан")
    else:
        await _tell(call.bot, uid, "❌ Доступ к боту не подтверждён.")
        line = f"❌ <b>Отказано {by} в {stamp}</b>"
        await _report_to_owner(session, call.bot, actor, f"❌ отказал(-а) {_who(user, uid)}")
        await call.answer("Отказано")
    if not await _mark_notification(call, line):
        await _render_user(call, session, actor, uid)


@router.callback_query(F.data.startswith("adm:block:"))
async def block(call: CallbackQuery, session: AsyncSession) -> None:
    actor = await _guard(call, session)
    if actor is None:
        return
    uid = _uid(call)
    if uid is None or access.is_owner(uid):
        await call.answer("Владельца заблокировать нельзя", show_alert=True)
        return
    target = await session.get(User, uid)
    was_admin = target is not None and target.role == ROLE_ADMIN
    user = await access.block_user(session, actor.id, uid)
    await session.commit()
    if user is None:
        await call.answer("🔒 Нет прав: админов может блокировать только владелец", show_alert=True)
        return
    await _tell(call.bot, uid, "🚫 Доступ к боту закрыт.")
    if was_admin:
        await _set_commands(call.bot, uid, with_admin=False)
    await _report_to_owner(session, call.bot, actor, f"🚫 заблокировал(-а) {_who(user, uid)}")
    await call.answer("Заблокирован")
    if not await _mark_notification(call, f"🚫 <b>Заблокирован(-а) в {fmt_time(utcnow(), texts._TZ)}</b>"):
        await _render_user(call, session, actor, uid)


@router.callback_query(F.data.startswith("adm:unblock:"))
async def unblock(call: CallbackQuery, session: AsyncSession) -> None:
    actor = await _guard(call, session)
    if actor is None:
        return
    uid = _uid(call)
    user = await access.unblock_user(session, actor.id, uid) if uid else None
    await session.commit()
    if user is None:
        await call.answer("Не заблокирован или нет прав", show_alert=True)
    else:
        await _report_to_owner(session, call.bot, actor, f"♻️ разблокировал(-а) {_who(user, uid)}")
        if user.access_status == ACCESS_ACTIVE:
            await _tell(call.bot, uid, "♻️ Ваш доступ к боту восстановлен.", kb.main_menu())
            await call.answer("Доступ восстановлен")
        else:
            await call.answer("Разблокирован. Войти сможет только по новому приглашению", show_alert=True)
    if uid is not None:
        await _render_user(call, session, actor, uid)


@router.callback_query(F.data.startswith("adm:rm:"))
async def ask_remove(call: CallbackQuery, session: AsyncSession) -> None:
    actor = await _guard(call, session)
    if actor is None:
        return
    uid = _uid(call)
    user = await session.get(User, uid) if uid else None
    if user is None or not access.can_moderate(actor, uid, user):
        await call.answer("🔒 Нет прав", show_alert=True)
        return
    servers = await repo.count_servers(session, uid)
    await edit(
        call,
        f"🗑 Удалить <b>{esc(user.display_name)}</b> (<code>{uid}</code>)?\n\n"
        f"Будут удалены доступ и добавленные им серверы ({servers}) — они пропадут "
        "из общего списка у всех. Вернуться человек сможет только по новому приглашению.",
        kb.admin_confirm_remove(uid),
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm:rmok:"))
async def do_remove(call: CallbackQuery, session: AsyncSession) -> None:
    actor = await _guard(call, session)
    if actor is None:
        return
    uid = _uid(call)
    user = await session.get(User, uid) if uid else None
    who = _who(user, uid or 0)
    ok = await access.remove_user(session, actor.id, uid) if uid else False
    await session.commit()
    if ok:
        await _tell(call.bot, uid, "🚫 Доступ к боту закрыт.")
        await _set_commands(call.bot, uid, with_admin=False)
        await _report_to_owner(session, call.bot, actor, f"🗑 удалил(-а) {who}")
    await call.answer("Удалён" if ok else "Нет прав или не найден", show_alert=not ok)
    await _users_view(call, session)


# -------------------------------------------------------------------- админы ---
@router.callback_query(F.data.startswith("adm:mkadm:"))
async def ask_make_admin(call: CallbackQuery, session: AsyncSession) -> None:
    actor = await _guard(call, session)
    if actor is None:
        return
    uid = _uid(call)
    user = await session.get(User, uid) if uid else None
    if not access.is_owner(actor.id) or user is None or user.access_status != ACCESS_ACTIVE:
        await call.answer("🔒 Выдавать админку может только владелец", show_alert=True)
        return
    await edit(
        call,
        "\n".join([
            f"⭐ Сделать <b>{esc(user.display_name)}</b> админом?",
            "",
            "Админ сможет:",
            "• пускать и отклонять заявки, создавать и отзывать приглашения;",
            "• блокировать и удалять обычных участников;",
            "• менять и удалять любые серверы, кроме домашнего;",
            "• смотреть журнал доступа.",
            "",
            "Админ <b>не</b> сможет: трогать вас и других админов, выдавать админку, "
            "выключать домашний сервер.",
            "О каждом его действии с людьми вам придёт уведомление.",
        ]),
        kb.admin_confirm_grant(uid),
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm:mkadmok:") | F.data.startswith("adm:rmadm:"))
async def set_admin(call: CallbackQuery, session: AsyncSession) -> None:
    actor = await _guard(call, session)
    if actor is None:
        return
    uid = _uid(call)
    grant = call.data.startswith("adm:mkadmok:")
    if not access.is_owner(actor.id):
        await call.answer("🔒 Выдавать и снимать админку может только владелец", show_alert=True)
        return
    user = await access.set_admin(session, actor.id, uid, grant) if uid else None
    await session.commit()
    if user is None:
        await call.answer("Не получилось: пользователь без доступа или роль уже такая", show_alert=True)
    elif grant:
        await _tell(
            call.bot, uid,
            "⭐ <b>Вам выданы права администратора</b>\n\n"
            "В главном меню появилась «👑 Админ-панель»: заявки, приглашения, участники и журнал.",
            kb.main_menu(True),
        )
        await _set_commands(call.bot, uid, with_admin=True)
        await call.answer("Админка выдана")
    else:
        await _tell(call.bot, uid, "Права администратора сняты.", kb.main_menu(False))
        await _set_commands(call.bot, uid, with_admin=False)
        await call.answer("Админка снята")
    if uid is not None:
        await _render_user(call, session, actor, uid)


# -------------------------------------------------------------- приглашения ---
@router.callback_query(F.data == "adm:inv:new")
async def invite_new(call: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if await _guard(call, session) is None:
        return
    await state.clear()
    await edit(
        call,
        "➕ <b>Новое приглашение</b>\n\n"
        "<b>Шаг 1 из 3.</b> Как впускать по ссылке?\n\n"
        "✋ <b>С подтверждением</b> — человек отправит заявку, а вы решите, пускать ли его. "
        "Защищает от пересылки ссылки: вы увидите, кто пришёл на самом деле.\n\n"
        "⚡ <b>Сразу</b> — доступ открывается мгновенно, вам придёт уведомление.",
        kb.invite_mode(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm:inv:mode:"))
async def invite_mode(call: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if await _guard(call, session) is None:
        return
    mode = call.data.split(":")[3]
    if mode not in (INVITE_APPROVE, INVITE_INSTANT):
        await call.answer("Некорректный запрос", show_alert=True)
        return
    await state.update_data(invite_mode=mode)
    await edit(
        call,
        "➕ <b>Новое приглашение</b>\n\n<b>Шаг 2 из 3.</b> Сколько действует ссылка?\n\n"
        "Чем короче срок, тем меньше шанс, что ссылкой воспользуется кто-то чужой.",
        kb.invite_ttl(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm:inv:ttl:"))
async def invite_ttl(call: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if await _guard(call, session) is None:
        return
    ttl = call.data.split(":")[3]
    if not ttl.isdigit() or int(ttl) not in access.INVITE_TTL_CHOICES:
        await call.answer("Некорректный запрос", show_alert=True)
        return
    await state.update_data(invite_ttl=int(ttl))
    await state.set_state(InviteNew.note)
    await edit(
        call,
        "➕ <b>Новое приглашение</b>\n\n<b>Шаг 3 из 3.</b> Для кого эта ссылка?\n\n"
        "Напишите подпись, например <code>Костя</code> — она будет в уведомлении о входе. "
        "Или нажмите «Без подписи».",
        kb.invite_note(),
    )
    await call.answer()


async def _create(bot: Bot, session: AsyncSession, state: FSMContext, actor: User, note: str | None):
    data = await state.get_data()
    await state.clear()
    mode = data.get("invite_mode", INVITE_APPROVE)
    ttl = data.get("invite_ttl", 24)
    try:
        invite, token = await access.create_invite(session, actor.id, mode, ttl, note)
    except (ValueError, PermissionError) as exc:
        return f"❌ {esc(str(exc))}", kb.back_main([("👑 Админ-панель", "adm:main")])
    await session.commit()
    await _report_to_owner(
        session, bot, actor,
        f"🔗 создал(-а) приглашение #{invite.id}{' «' + esc(invite.note) + '»' if invite.note else ''}",
    )
    me = await bot.me()
    link = access.invite_link(me.username, token)
    return texts.invite_created(invite, link), kb.invite_created(invite.id)


@router.callback_query(F.data == "adm:inv:nonote")
async def invite_no_note(call: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    actor = await _guard(call, session)
    if actor is None:
        return
    text, markup = await _create(call.bot, session, state, actor, None)
    await edit(call, text, markup)
    await call.answer("Приглашение создано")


@router.message(InviteNew.note)
async def invite_with_note(message: Message, session: AsyncSession, state: FSMContext) -> None:
    actor = await access.staff_by_id(session, message.from_user.id)
    if actor is None:
        await state.clear()
        return
    note = clean_name(message.text or "", limit=40)
    text, markup = await _create(message.bot, session, state, actor, note or None)
    await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


@router.callback_query(F.data == "adm:inv:list")
async def invites(call: CallbackQuery, session: AsyncSession) -> None:
    if await _guard(call, session) is None:
        return
    items = await access.active_invites(session)
    await edit(call, texts.invites_list(items), kb.invites_list(items))
    await call.answer()


@router.callback_query(F.data.startswith("adm:inv:rv:"))
async def revoke(call: CallbackQuery, session: AsyncSession) -> None:
    actor = await _guard(call, session)
    if actor is None:
        return
    parts = call.data.split(":")
    invite_id = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
    ok = await access.revoke_invite(session, actor.id, invite_id) if invite_id else False
    await session.commit()
    if ok:
        await _report_to_owner(session, call.bot, actor, f"🗑 отозвал(-а) приглашение #{invite_id}")
    await call.answer("Приглашение отозвано" if ok else "Уже использовано или отозвано", show_alert=not ok)
    items = await access.active_invites(session)
    await edit(call, texts.invites_list(items), kb.invites_list(items))


# ------------------------------------------------------------------- журнал ---
@router.callback_query(F.data == "adm:log")
async def access_log(call: CallbackQuery, session: AsyncSession) -> None:
    if await _guard(call, session) is None:
        return
    events = await access.recent_events(session, limit=20)
    await edit(call, texts.access_log(events), kb.admin_log())
    await call.answer()
