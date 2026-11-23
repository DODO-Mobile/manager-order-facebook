import re
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .db import FacebookCustomer, MessageLog, MessengerOrderCustomer, Order, utcnow


def text_value(value):
    return value.strip() or None if isinstance(value, str) else None


def actor(value):
    if not isinstance(value, Mapping):
        return None, None
    identifier = value.get("id")
    if type(identifier) is int:
        identifier = str(identifier)
    return text_value(identifier), text_value(value.get("name"))


def supplied_email(message):
    if not isinstance(message, str):
        return None
    address = re.sub(r"^e-?mail\s*:\s*", "", message.strip(), flags=re.IGNORECASE)
    if len(address) <= 254 and re.fullmatch(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", address):
        return address
    return None


def _insert(session, model):
    insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    return insert(model)


def save_customer(session, page_id, identity_key, source, **fields):
    values = {key: value for key, value in fields.items() if value is not None}
    statement = _insert(session, FacebookCustomer).values(
        page_id=page_id, identity_key=identity_key, source=source, collected_at=utcnow(), **values)
    return session.execute(statement.on_conflict_do_update(
        index_elements=["page_id", "identity_key"],
        set_={**values, "collected_at": statement.excluded.collected_at},
    ).returning(FacebookCustomer.id)).scalar_one()


def link_messenger_reply(session, page_id, reply_mid, customer_id):
    if not reply_mid:
        return False
    order_ids = session.scalars(select(Order.id).join(MessageLog, (
        (MessageLog.page_id == Order.page_id) & (MessageLog.comment_id == Order.comment_id)
    )).where(MessageLog.page_id == page_id, MessageLog.meta_message_id == reply_mid,
             MessageLog.action == "private_reply", MessageLog.status == "SENT").limit(2)).all()
    if len(order_ids) != 1:
        return False
    session.execute(_insert(session, MessengerOrderCustomer).values(
        order_id=order_ids[0], customer_id=customer_id).on_conflict_do_nothing())
    return True
