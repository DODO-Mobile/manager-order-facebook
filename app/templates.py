TEMPLATE_VERSION = "v1"

DEFAULT_TEMPLATE = (
    "Dạ shop gửi bạn thông tin đơn hàng nhé ❤️\n"
    "{lines}\n"
    "Tổng: {total} (chưa bao gồm phí vận chuyển).\n"
    "Bạn kiểm tra và phản hồi XÁC NHẬN để shop hỗ trợ nhé!"
)


def money(v: int) -> str:
    return f"{v:,}".replace(",", ".") + "đ"


def render(items: list[dict], total: int, template: str | None = None) -> str:
    lines = []
    for i in items:
        label = " ".join(x for x in [str(i["qty"]), i["name"], i.get("color"), i.get("size")] if x)
        lines.append(f"• {label}: {money(i['subtotal'])}")
    return (template or DEFAULT_TEMPLATE).format(lines="\n".join(lines), total=money(total))
