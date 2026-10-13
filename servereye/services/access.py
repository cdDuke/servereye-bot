"""Контроль доступа: владелец, одноразовые приглашения, журнал.

Модель угроз и как она закрыта:
* подбор ссылки — 256 бит случайности, лимит неудачных попыток;
* утечка базы — хранится только SHA-256 токена, ссылку из базы не восстановить;
* повторное использование — атомарный UPDATE ... WHERE status='active';
  гонка двух одновременных входов даёт ровно одного победителя;
* пересылка ещё не использованной ссылки — срок жизни + режим
  «с подтверждением»: владелец видит, кто пришёл на самом деле;
* заблокированный пользователь с новой ссылкой — не пускается;
* владелец, нажавший свою же ссылку, её не сжигает.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import timedelta

from aiogram.types import User as TgUser
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..database import repository as repo
from ..database.models import (
    ACCESS_ACTIVE,
    ACCESS_BLOCKED,
    ACCESS_DENIED,
    ACCESS_PENDING,
    INVITE_ACTIVE,
    INVITE_APPROVE,
    INVITE_INSTANT,
    INVITE_REVOKED,
    INVITE_USED,
    ROLE_ADMIN,
    ROLE_OWNER,
    ROLE_USER,
    AccessEvent,
    Invite,
    User,
)
from ..utils import utcnow

log = logging.getLogger(__name__)
settings = get_settings()

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
MAX_ACTIVE_INVITES = 20
INVITE_TTL_CHOICES = (1, 24, 24 * 7)  # часов

# Ограничение неудачных попыток входа по ссылке
FAIL_LIMIT = 5
FAIL_WINDOW = 3600  # секунд
_failures: dict[int, deque[float]] = defaultdict(deque)

# События журнала
EV_JOINED = "joined"
EV_REQUESTED = "requested"
EV_APPROVED = "approved"
EV_DENIED = "denied"
EV_BLOCKED = "blocked"
EV_UNBLOCKED = "unblocked"
EV_REMOVED = "removed"
EV_STRANGER = "stranger"
EV_INVALID = "invalid_link"
EV_REUSED = "reused_link"
EV_EXPIRED = "expired_link"
EV_BLOCKED_TRY = "blocked_try"
EV_LOCKED = "locked_out"
EV_OWNER_GRANTED = "owner_granted"
NOTICE_PENDING = "уведомление не отправлено"
NOTICE_SENT = "уведомлён"
EV_OWNER_REVOKED = "owner_revoked"
EV_ADMIN_GRANTED = "admin_granted"
EV_ADMIN_REVOKED = "admin_revoked"
EV_INVITE_CREATED = "invite_created"
EV_INVITE_REVOKED = "invite_revoked"

EVENT_TITLES = {
    EV_JOINED: "✅ вошёл по приглашению",
    EV_REQUESTED: "⏳ запросил доступ",
    EV_APPROVED: "✅ доступ подтверждён",
    EV_DENIED: "❌ в доступе отказано",
    EV_BLOCKED: "🚫 заблокирован",
    EV_UNBLOCKED: "♻️ разблокирован",
    EV_REMOVED: "🗑 удалён",
    EV_STRANGER: "👤 посторонний без приглашения",
    EV_INVALID: "❓ неверная ссылка",
    EV_REUSED: "⚠️ чужая использованная ссылка",
    EV_EXPIRED: "⌛️ просроченная/отозванная ссылка",
    EV_BLOCKED_TRY: "🚫 попытка заблокированного",
    EV_LOCKED: "🔒 слишком много попыток",
    EV_OWNER_GRANTED: "👑 стал совладельцем",
    EV_OWNER_REVOKED: "👑 больше не совладелец",
    EV_ADMIN_GRANTED: "⭐ назначен админом",
    EV_ADMIN_REVOKED: "⭐ снят с админов",
    EV_INVITE_CREATED: "🔗 создано приглашение",
    EV_INVITE_REVOKED: "🗑 приглашение отозвано",
}

# Итоги попытки входа по ссылке
R_JOINED = "joined"
R_REQUESTED = "requested"
R_INVALID = "invalid"
R_REUSED = "reused"
R_EXPIRED = "expired"
R_BLOCKED = "blocked"
R_LOCKED = "locked"
R_ALREADY = "already"


# ------------------------------------------------------------------- базовое ---
def owner_ids() -> set[int]:
    return settings.admin_ids


def is_owner(user_id: int | None) -> bool:
    return user_id is not None and user_id in settings.admin_ids


def is_admin(user: User | None) -> bool:
    """Назначенный админ с действующим доступом (не владелец)."""
    return (
        user is not None
        and user.role == ROLE_ADMIN
        and user.access_status == ACCESS_ACTIVE
        and not is_owner(user.id)
    )


def is_staff(user: User | None) -> bool:
    """Владелец или админ — доступ к админ-панели."""
    return user is not None and (is_owner(user.id) or is_admin(user))


async def staff_by_id(session: AsyncSession, user_id: int | None) -> User | None:
    """Пользователь, если он владелец или админ; иначе None."""
    if user_id is None:
        return None
    user = await session.get(User, user_id)
    return user if is_staff(user) else None


async def staff_ids(session: AsyncSession) -> set[int]:
    """Кому слать уведомления о доступе: владельцы и админы."""
    ids = set(owner_ids())
    rows = await session.execute(
        select(User.id).where(User.role == ROLE_ADMIN, User.access_status == ACCESS_ACTIVE)
    )
    ids.update(r[0] for r in rows)
    return ids


def can_manage(server, user: User | None) -> bool:
    """Кто может менять и удалять сервер.

    * автор сервера;
    * владелец бота — любой сервер;
    * админ — любой сервер, кроме домашнего (машины, на которой работает бот).
    Смотреть, проверять и получать тревоги могут все участники.
    """
    if user is None:
        return False
    if server.user_id == user.id or is_owner(user.id):
        return True
    return is_admin(user) and not server.is_local_host


def can_moderate(actor: User | None, target_id: int, target: User | None) -> bool:
    """Может ли actor блокировать/удалять/решать по target."""
    if actor is None or not is_staff(actor) or is_owner(target_id):
        return False
    if is_owner(actor.id):
        return True
    # админ не трогает других админов и себя
    return target_id != actor.id and not (target is not None and target.role == ROLE_ADMIN)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def full_name(tg: TgUser) -> str:
    return " ".join(x for x in (tg.first_name, tg.last_name) if x) or "без имени"


async def ensure_owners(session: AsyncSession) -> list[int]:
    """Владельцы из ADMIN_IDS всегда существуют и всегда активны.

    Возвращает ID тех, кто только что стал владельцем (уже был в боте
    с другой ролью), — им стоит сообщить о новых правах.
    """
    promoted: list[int] = []
    for owner_id in owner_ids():
        user = await session.get(User, owner_id)
        if user is None:
            user = User(id=owner_id)
            session.add(user)
            await session.flush()
            await repo.get_settings_for(session, owner_id)
        elif user.role != ROLE_OWNER:
            # повышение участника (а не первичная регистрация владельца)
            if user.access_status == ACCESS_ACTIVE and user.role in (ROLE_USER, ROLE_ADMIN):
                promoted.append(owner_id)
                await log_event(session, owner_id, EV_OWNER_GRANTED,
                                username=user.username, name=user.display_name,
                                detail=NOTICE_PENDING)
        user.access_status = ACCESS_ACTIVE
        user.role = ROLE_OWNER
    # кого убрали из ADMIN_IDS — становится обычным участником
    rows = await session.execute(select(User).where(User.role == ROLE_OWNER))
    for user in rows.scalars():
        if not is_owner(user.id):
            user.role = ROLE_USER
            await log_event(session, user.id, EV_OWNER_REVOKED,
                            username=user.username, name=user.display_name)
    await session.flush()
    return promoted


async def pending_owner_notices(session: AsyncSession) -> list[AccessEvent]:
    """Повышения до совладельца, о которых человеку ещё не сообщили.

    Повысить может любой процесс (бот или установщик), а сообщает всегда
    бот при старте — так уведомление не теряется из-за гонки.
    """
    stmt = select(AccessEvent).where(
        AccessEvent.event == EV_OWNER_GRANTED, AccessEvent.detail == NOTICE_PENDING
    )
    return [ev for ev in (await session.execute(stmt)).scalars() if is_owner(ev.tg_id)]


async def log_event(
    session: AsyncSession,
    tg_id: int,
    event: str,
    *,
    username: str | None = None,
    name: str | None = None,
    invite_id: int | None = None,
    detail: str | None = None,
) -> None:
    session.add(
        AccessEvent(
            tg_id=tg_id,
            username=username,
            full_name=(name or "")[:128] or None,
            event=event,
            invite_id=invite_id,
            detail=(detail or "")[:255] or None,
        )
    )
    await session.flush()


async def log_tg(session: AsyncSession, tg: TgUser, event: str, **kwargs) -> None:
    await log_event(session, tg.id, event, username=tg.username, name=full_name(tg), **kwargs)


async def recently_logged(session: AsyncSession, tg_id: int, event: str, hours: float = 24) -> bool:
    since = utcnow() - timedelta(hours=hours)
    count = await session.scalar(
        select(func.count(AccessEvent.id)).where(
            AccessEvent.tg_id == tg_id,
            AccessEvent.event == event,
            AccessEvent.created_at >= since,
        )
    )
    return bool(count)


def register_failure(tg_id: int) -> bool:
    """Учитывает неудачную попытку. True — лимит только что превышен."""
    now = time.monotonic()
    bucket = _failures[tg_id]
    while bucket and now - bucket[0] > FAIL_WINDOW:
        bucket.popleft()
    bucket.append(now)
    return len(bucket) == FAIL_LIMIT + 1


def is_locked(tg_id: int) -> bool:
    now = time.monotonic()
    bucket = _failures.get(tg_id)
    if not bucket:
        return False
    while bucket and now - bucket[0] > FAIL_WINDOW:
        bucket.popleft()
    return len(bucket) > FAIL_LIMIT


# --------------------------------------------------------------- приглашения ---
async def count_active_invites(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count(Invite.id)).where(
                Invite.status == INVITE_ACTIVE, Invite.expires_at > utcnow()
            )
        )
        or 0
    )


async def create_invite(
    session: AsyncSession, owner_id: int, mode: str, ttl_hours: int, note: str | None = None
) -> tuple[Invite, str]:
    if await staff_by_id(session, owner_id) is None:
        raise PermissionError("Создавать приглашения может только владелец или админ")
    if mode not in (INVITE_APPROVE, INVITE_INSTANT):
        raise ValueError("Неизвестный режим приглашения")
    if ttl_hours not in INVITE_TTL_CHOICES:
        raise ValueError("Недопустимый срок действия")
    if await count_active_invites(session) >= MAX_ACTIVE_INVITES:
        raise ValueError(f"Уже {MAX_ACTIVE_INVITES} активных приглашений — отзовите ненужные")

    token = secrets.token_urlsafe(32)  # 43 символа, 256 бит
    invite = Invite(
        token_hash=hash_token(token),
        created_by=owner_id,
        expires_at=utcnow() + timedelta(hours=ttl_hours),
        mode=mode,
        note=(note or "").strip()[:64] or None,
        status=INVITE_ACTIVE,
    )
    session.add(invite)
    await session.flush()
    await log_event(session, owner_id, EV_INVITE_CREATED, invite_id=invite.id,
                    detail=f"{mode}, {ttl_hours} ч, {invite.note or ''}")
    return invite, token


def invite_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?start={token}"


async def active_invites(session: AsyncSession) -> list[Invite]:
    stmt = (
        select(Invite)
        .where(Invite.status == INVITE_ACTIVE, Invite.expires_at > utcnow())
        .order_by(Invite.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def revoke_invite(session: AsyncSession, owner_id: int, invite_id: int) -> bool:
    if await staff_by_id(session, owner_id) is None:
        return False
    result = await session.execute(
        update(Invite)
        .where(Invite.id == invite_id, Invite.status == INVITE_ACTIVE)
        .values(status=INVITE_REVOKED)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount:
        await log_event(session, owner_id, EV_INVITE_REVOKED, invite_id=invite_id)
        return True
    return False


@dataclass
class RedeemResult:
    kind: str
    invite: Invite | None = None
    user: User | None = None
    previous_user: User | None = None
    notify_owner: bool = False


async def redeem(session: AsyncSession, tg: TgUser, token: str) -> RedeemResult:
    """Попытка войти по ссылке. Вызывается только для тех, у кого нет доступа."""
    if is_locked(tg.id):
        return RedeemResult(R_LOCKED)

    if not TOKEN_RE.match(token or ""):
        return await _fail(session, tg, R_INVALID, EV_INVALID, detail="неверный формат")

    invite = (
        await session.execute(select(Invite).where(Invite.token_hash == hash_token(token)))
    ).scalar_one_or_none()
    if invite is None:
        return await _fail(session, tg, R_INVALID, EV_INVALID, detail="нет такой ссылки")

    user = await session.get(User, tg.id)
    now = utcnow()

    if user is not None and user.access_status == ACCESS_BLOCKED:
        notify = not await recently_logged(session, tg.id, EV_BLOCKED_TRY)
        await log_tg(session, tg, EV_BLOCKED_TRY, invite_id=invite.id)
        return RedeemResult(R_BLOCKED, invite, user, notify_owner=notify)

    if invite.status == INVITE_USED:
        if invite.used_by == tg.id:
            # тот же человек нажал ссылку ещё раз — ничего страшного
            return RedeemResult(R_ALREADY, invite, user)
        previous = await session.get(User, invite.used_by) if invite.used_by else None
        register_failure(tg.id)
        await log_tg(session, tg, EV_REUSED, invite_id=invite.id,
                     detail=f"использовал ранее id{invite.used_by}")
        return RedeemResult(R_REUSED, invite, user, previous, notify_owner=True)

    if invite.status != INVITE_ACTIVE or invite.expires_at <= now:
        register_failure(tg.id)
        notify = not await recently_logged(session, tg.id, EV_EXPIRED)
        await log_tg(session, tg, EV_EXPIRED, invite_id=invite.id, detail=invite.status)
        return RedeemResult(R_EXPIRED, invite, user, notify_owner=notify)

    # Атомарное погашение: выиграет ровно одна попытка
    result = await session.execute(
        update(Invite)
        .where(
            Invite.id == invite.id,
            Invite.status == INVITE_ACTIVE,
            Invite.expires_at > now,
        )
        .values(status=INVITE_USED, used_by=tg.id, used_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.refresh(invite)
    if result.rowcount != 1:
        previous = await session.get(User, invite.used_by) if invite.used_by else None
        await log_tg(session, tg, EV_REUSED, invite_id=invite.id, detail="проиграл гонку")
        return RedeemResult(R_REUSED, invite, user, previous, notify_owner=True)

    status = ACCESS_ACTIVE if invite.mode == INVITE_INSTANT else ACCESS_PENDING
    if user is None:
        user = User(id=tg.id)
        session.add(user)
    user.username = tg.username
    user.first_name = tg.first_name
    user.last_name = tg.last_name
    user.language_code = tg.language_code
    user.last_seen_at = now
    user.is_active = True
    user.role = ROLE_USER
    user.access_status = status
    user.invite_id = invite.id
    user.access_changed_at = now
    await session.flush()
    await repo.get_settings_for(session, user.id)

    event = EV_JOINED if status == ACCESS_ACTIVE else EV_REQUESTED
    await log_tg(session, tg, event, invite_id=invite.id)
    return RedeemResult(
        R_JOINED if status == ACCESS_ACTIVE else R_REQUESTED, invite, user, notify_owner=True
    )


async def _fail(session, tg: TgUser, kind: str, event: str, detail: str) -> RedeemResult:
    just_locked = register_failure(tg.id)
    notify = just_locked or not await recently_logged(session, tg.id, event)
    await log_tg(session, tg, event, detail=detail)
    if just_locked:
        await log_tg(session, tg, EV_LOCKED)
        return RedeemResult(R_LOCKED, notify_owner=True)
    return RedeemResult(kind, notify_owner=notify)


# -------------------------------------------------------------- пользователи ---
async def set_access(
    session: AsyncSession, actor_id: int, user_id: int, status: str, event: str
) -> User | None:
    actor = await session.get(User, actor_id)
    user = await session.get(User, user_id)
    if user is None or not can_moderate(actor, user_id, user):
        return None
    user.access_status = status
    user.access_changed_at = utcnow()
    if status != ACCESS_ACTIVE and user.role == ROLE_ADMIN:
        user.role = ROLE_USER  # заблокированный или отклонённый — больше не админ
    await log_event(session, user_id, event, username=user.username,
                    name=user.display_name, detail=f"кем: {actor_id}")
    return user


async def block_user(session: AsyncSession, actor_id: int, tg_id: int) -> User | None:
    """Блокировка, в том числе постороннего, которого ещё нет в базе."""
    actor = await session.get(User, actor_id)
    user = await session.get(User, tg_id)
    if not can_moderate(actor, tg_id, user):
        return None
    if user is None:
        last = (
            await session.execute(
                select(AccessEvent)
                .where(AccessEvent.tg_id == tg_id)
                .order_by(AccessEvent.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        user = User(id=tg_id, role=ROLE_USER, access_status=ACCESS_BLOCKED, is_active=False)
        if last is not None:
            user.username = last.username
            user.first_name = last.full_name
        session.add(user)
        await session.flush()
    return await set_access(session, actor_id, tg_id, ACCESS_BLOCKED, EV_BLOCKED)


async def unblock_user(session: AsyncSession, actor_id: int, tg_id: int) -> User | None:
    """Снятие блокировки. Кто никогда не входил по приглашению — доступа не получает."""
    user = await session.get(User, tg_id)
    if user is None or user.access_status != ACCESS_BLOCKED:
        return None
    status = ACCESS_ACTIVE if user.invite_id else ACCESS_DENIED
    return await set_access(session, actor_id, tg_id, status, EV_UNBLOCKED)


async def decide_request(
    session: AsyncSession, actor_id: int, tg_id: int, approve: bool
) -> User | None:
    user = await session.get(User, tg_id)
    if user is None or user.access_status not in (ACCESS_PENDING, ACCESS_DENIED):
        return None
    if approve:
        return await set_access(session, actor_id, tg_id, ACCESS_ACTIVE, EV_APPROVED)
    return await set_access(session, actor_id, tg_id, ACCESS_DENIED, EV_DENIED)


async def set_admin(session: AsyncSession, owner_id: int, tg_id: int, grant: bool) -> User | None:
    """Выдать или снять админку. Только владелец и только участнику с доступом."""
    if not is_owner(owner_id) or is_owner(tg_id):
        return None
    user = await session.get(User, tg_id)
    if user is None or user.access_status != ACCESS_ACTIVE:
        return None
    target_role = ROLE_ADMIN if grant else ROLE_USER
    if user.role == target_role:
        return None
    user.role = target_role
    user.access_changed_at = utcnow()
    await log_event(session, tg_id, EV_ADMIN_GRANTED if grant else EV_ADMIN_REVOKED,
                    username=user.username, name=user.display_name, detail=f"кем: {owner_id}")
    return user


async def remove_user(session: AsyncSession, actor_id: int, user_id: int) -> bool:
    """Удаляет пользователя вместе с его серверами и историей."""
    actor = await session.get(User, actor_id)
    user = await session.get(User, user_id)
    if user is None or not can_moderate(actor, user_id, user):
        return False
    await log_event(session, user_id, EV_REMOVED, username=user.username,
                    name=user.display_name, detail=f"кем: {actor_id}")
    await session.delete(user)
    await session.flush()
    return True


async def list_users(session: AsyncSession, status: str | None = None) -> list[User]:
    stmt = select(User).order_by(User.last_seen_at.desc())
    if status is not None:
        stmt = stmt.where(User.access_status == status)
    return list((await session.execute(stmt)).scalars().all())


async def recent_events(session: AsyncSession, limit: int = 25) -> list[AccessEvent]:
    stmt = select(AccessEvent).order_by(AccessEvent.created_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def cleanup(session: AsyncSession, days: int) -> None:
    cutoff = utcnow() - timedelta(days=days)
    await session.execute(
        delete(Invite).where(Invite.created_at < cutoff, Invite.status != INVITE_ACTIVE)
    )
    await session.execute(
        delete(Invite).where(Invite.status == INVITE_ACTIVE, Invite.expires_at < cutoff)
    )
    # журнал доступа храним минимум полгода — это аудит безопасности
    events_cutoff = utcnow() - timedelta(days=max(days, 180))
    await session.execute(delete(AccessEvent).where(AccessEvent.created_at < events_cutoff))
