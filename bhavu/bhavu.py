import os
import json
import time
import uuid
import boto3
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
from delta_rest_client import DeltaRestClient

SECRET_NAME = "Bhavu"
AWS_REGION = "ap-south-1"

BASE_URL = "https://api.india.delta.exchange"
SYMBOL = "ETHUSD"
PRODUCT_ID = 3136

BOT_PREFIX = "BHV-"
POLL_SECONDS = 2
STATE_FILE = Path(__file__).resolve().parent / "bhavu_state.json"


def log(message):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), "|", message, flush=True)


def required_secret(data, key):
    value = data.get(key)
    if value is None or str(value).strip() == "":
        raise RuntimeError(f"Missing required AWS secret key: {key}")
    return str(value).strip()


def parse_lots(value, key):
    text = str(value).strip().lower().replace("lots", "").strip()
    try:
        number = int(Decimal(text))
    except Exception:
        raise RuntimeError(f"{key} must be a whole number of lots")

    if number <= 0:
        raise RuntimeError(f"{key} must be greater than zero")

    return number


def parse_money(value, key):
    text = str(value).strip().replace("$", "").replace(",", "")

    try:
        number = Decimal(text)
    except Exception:
        raise RuntimeError(f"{key} must be a valid number")

    if number <= 0:
        raise RuntimeError(f"{key} must be greater than zero")

    return number


def load_config():
    session = boto3.session.Session(region_name=AWS_REGION)
    secrets = session.client("secretsmanager")

    response = secrets.get_secret_value(SecretId=SECRET_NAME)
    raw = response.get("SecretString")

    if not raw:
        raise RuntimeError("AWS secret Bhavu has no SecretString")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"AWS secret Bhavu is not valid JSON: {exc}")

    config = {
        "api_key": required_secret(data, "API_Key"),
        "api_secret": required_secret(data, "API-Secret_Key"),
        "max_position": parse_lots(
            required_secret(data, "Max_Position"),
            "Max_Position",
        ),
        "order_size": parse_lots(
            required_secret(data, "Order_Size"),
            "Order_Size",
        ),
        "step": parse_money(
            required_secret(data, "Step"),
            "Step",
        ),
        "counter_step": parse_money(
            required_secret(data, "Counter_Step"),
            "Counter_Step",
        ),
    }

    if config["order_size"] > config["max_position"]:
        raise RuntimeError("Order_Size cannot be greater than Max_Position")

    return config


CONFIG = load_config()

API_KEY = CONFIG["api_key"]
API_SECRET = CONFIG["api_secret"]
MAX_POSITION = CONFIG["max_position"]
ORDER_SIZE = CONFIG["order_size"]
STEP = CONFIG["step"]
COUNTER_STEP = CONFIG["counter_step"]


client = DeltaRestClient(
    base_url=BASE_URL,
    api_key=API_KEY,
    api_secret=API_SECRET,
    raise_for_status=True,
)


def default_state():
    return {
        "version": 2,
        "bot_position": 0,
        "next_sequence": 1,
        "orders": {},
    }


def load_state():
    if not STATE_FILE.exists():
        return default_state()

    try:
        with STATE_FILE.open("r") as file:
            state = json.load(file)

        if not isinstance(state, dict):
            raise ValueError("state is not an object")

        state.setdefault("version", 2)
        state.setdefault("bot_position", 0)
        state.setdefault("next_sequence", 1)
        state.setdefault("orders", {})

        return state

    except Exception as exc:
        log(f"STATE LOAD ERROR: {exc}")
        log("Using clean in-memory state")
        return default_state()


def save_state():
    temporary = STATE_FILE.with_suffix(".tmp")

    with temporary.open("w") as file:
        json.dump(STATE, file, indent=2)

    os.replace(temporary, STATE_FILE)


STATE = load_state()


def decimal_price(value):
    return Decimal(str(value))


def grid_anchor(value):
    return decimal_price(value).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )


def price_string(value):
    price = decimal_price(value).normalize()

    if price == price.to_integral_value():
        return str(int(price))

    return format(price, "f")


def new_client_order_id(role, parent_id=None):
    sequence = STATE["next_sequence"]
    STATE["next_sequence"] += 1

    if parent_id is None:
        client_id = (
            f"{BOT_PREFIX}{role}-"
            f"{sequence}-"
            f"{uuid.uuid4().hex[:6]}"
        )
    else:
        client_id = (
            f"{BOT_PREFIX}{role}-"
            f"{parent_id}-"
            f"{sequence}"
        )

    return client_id[:32]


def get_cmp():
    ticker = client.get_ticker(SYMBOL)

    if isinstance(ticker, dict):
        for key in (
            "last_price",
            "close",
            "mark_price",
            "spot_price",
        ):
            value = ticker.get(key)

            if value is not None:
                return decimal_price(value)

    raise RuntimeError(
        f"Unable to read CMP from ticker: {ticker}"
    )


def get_live_orders():
    orders = client.get_live_orders()

    if orders is None:
        return []

    if isinstance(orders, dict):
        orders = orders.get(
            "result",
            orders.get("orders", []),
        )

    if not isinstance(orders, list):
        return []

    return [
        order
        for order in orders
        if int(order.get("product_id", -1)) == PRODUCT_ID
    ]


def get_live_bhavu_orders():
    result = {}

    for order in get_live_orders():
        client_id = str(order.get("client_order_id", ""))

        if client_id.startswith(BOT_PREFIX):
            result[str(order["id"])] = order

    return result


def recover_live_orders():
    live = get_live_bhavu_orders()
    recovered = 0

    for order_id, order in live.items():

        if order_id in STATE["orders"]:
            continue

        side = str(order.get("side", "")).lower()

        if side not in ("buy", "sell"):
            continue

        size = int(float(order.get("size", ORDER_SIZE)))
        price = decimal_price(
            order.get("limit_price", 0)
        )

        STATE["orders"][order_id] = {
            "side": side,
            "role": "RECOVERED",
            "price": price_string(price),
            "size": size,
            "client_order_id": order.get(
                "client_order_id",
                "",
            ),
            "handled": False,
        }

        recovered += 1

    if recovered:
        save_state()

    log(
        f"STARTUP RECOVERY | LIVE BHAVU={len(live)} | "
        f"NEWLY RECOVERED={recovered}"
    )

    return live


def counter_order_exists(parent_id, role):
    prefix = f"{BOT_PREFIX}{role}-{parent_id}-"

    for order in get_live_bhavu_orders().values():
        client_id = str(
            order.get("client_order_id", "")
        )

        if client_id.startswith(prefix):
            return True

    return False


def place_limit(side, price, role, parent_id=None):
    price = decimal_price(price)

    client_id = new_client_order_id(
        role,
        parent_id,
    )

    payload = {
        "product_id": PRODUCT_ID,
        "size": int(ORDER_SIZE),
        "side": side,
        "order_type": "limit_order",
        "limit_price": price_string(price),
        "time_in_force": "gtc",
        "post_only": False,
        "reduce_only": False,
        "client_order_id": client_id,
    }

    log(
        f"PLACING {side.upper()} | "
        f"{ORDER_SIZE} LOTS | "
        f"PRICE {price_string(price)}"
    )

    result = client.create_order(payload)

    if not isinstance(result, dict):
        raise RuntimeError(
            f"Unexpected order response: {result}"
        )

    order_id = result.get("id")

    if order_id is None:
        raise RuntimeError(
            f"Delta returned no order ID: {result}"
        )

    STATE["orders"][str(order_id)] = {
        "side": side,
        "role": role,
        "price": price_string(price),
        "size": int(ORDER_SIZE),
        "client_order_id": client_id,
        "parent_id": (
            str(parent_id)
            if parent_id
            else None
        ),
        "handled": False,
    }

    save_state()

    log(
        f"ORDER CREATED | ID={order_id} | "
        f"{side.upper()} {price_string(price)} | "
        f"{client_id}"
    )

    return result


def create_fresh_grid():
    live = get_live_bhavu_orders()

    if live:
        log(
            f"FRESH GRID SKIPPED | "
            f"{len(live)} LIVE BHAVU ORDERS EXIST"
        )
        return

    cmp = get_cmp()
    anchor = grid_anchor(cmp)
    levels = MAX_POSITION // ORDER_SIZE

    log("==========================================")
    log("CREATING FRESH BHAVU GRID")
    log("==========================================")
    log(f"SYMBOL: {SYMBOL}")
    log(f"PRODUCT ID: {PRODUCT_ID}")
    log(f"CMP: {price_string(cmp)}")
    log(f"GRID ANCHOR: {price_string(anchor)}")
    log(f"STEP: {price_string(STEP)}")
    log(
        f"COUNTER STEP: "
        f"{price_string(COUNTER_STEP)}"
    )
    log(f"ORDER SIZE: {ORDER_SIZE} LOTS")
    log(f"MAX POSITION: {MAX_POSITION} LOTS")
    log(f"GRID LEVELS: {levels}")

    for level in range(levels):

        price = anchor - (
            STEP * Decimal(level)
        )

        place_limit(
            side="buy",
            price=price,
            role="B",
        )

        time.sleep(0.15)

    log(
        f"FRESH GRID READY | "
        f"{levels} BUY ORDERS"
    )


def get_order_status(order_id):
    try:
        return client.get_order_by_id(
            int(order_id)
        )

    except Exception as exc:
        log(
            f"ORDER STATUS ERROR | ID={order_id} | "
            f"{type(exc).__name__}: {exc}"
        )
        return None


def handle_buy_fill(order_id, info, order):

    fill_price = decimal_price(
        order.get(
            "average_fill_price",
            order.get(
                "limit_price",
                info["price"],
            ),
        )
    )

    if not counter_order_exists(
        order_id,
        "S",
    ):

        sell_price = (
            fill_price + COUNTER_STEP
        )

        place_limit(
            side="sell",
            price=sell_price,
            role="S",
            parent_id=order_id,
        )

    else:

        log(
            f"BUY COUNTER ALREADY EXISTS | "
            f"BUY ID={order_id}"
        )

    STATE["bot_position"] += int(
        info["size"]
    )

    info["fill_price"] = price_string(
        fill_price
    )

    info["handled"] = True

    save_state()

    log(
        f"BUY FILLED | ID={order_id} | "
        f"PRICE {price_string(fill_price)} | "
        f"BOT POSITION={STATE['bot_position']}"
    )


def handle_sell_fill(order_id, info, order):

    fill_price = decimal_price(
        order.get(
            "average_fill_price",
            order.get(
                "limit_price",
                info["price"],
            ),
        )
    )

    new_position = max(
        0,
        STATE["bot_position"]
        - int(info["size"]),
    )

    if (
        new_position + ORDER_SIZE
        <= MAX_POSITION
    ):

        if not counter_order_exists(
            order_id,
            "B",
        ):

            buy_price = (
                fill_price - COUNTER_STEP
            )

            place_limit(
                side="buy",
                price=buy_price,
                role="B",
                parent_id=order_id,
            )

        else:

            log(
                f"SELL COUNTER ALREADY EXISTS | "
                f"SELL ID={order_id}"
            )

    else:

        log(
            "MAX POSITION REACHED | "
            "NO REBUY CREATED"
        )

    STATE["bot_position"] = new_position

    info["fill_price"] = price_string(
        fill_price
    )

    info["handled"] = True

    save_state()

    log(
        f"SELL FILLED | ID={order_id} | "
        f"PRICE {price_string(fill_price)} | "
        f"BOT POSITION={STATE['bot_position']}"
    )


def process_tracked_orders():

    for order_id, info in list(
        STATE["orders"].items()
    ):

        if info.get("handled"):
            continue

        order = get_order_status(
            order_id
        )

        if not order:
            continue

        state = str(
            order.get("state", "")
        ).lower()

        if state in (
            "open",
            "pending",
        ):
            continue

        if state == "closed":

            side = str(
                info.get("side", "")
            ).lower()

            if side == "buy":

                handle_buy_fill(
                    order_id,
                    info,
                    order,
                )

            elif side == "sell":

                handle_sell_fill(
                    order_id,
                    info,
                    order,
                )

        elif state in (
            "cancelled",
            "canceled",
        ):

            info["handled"] = True

            save_state()

            log(
                f"BOT ORDER CANCELLED | "
                f"ID={order_id}"
            )


def main():

    log("==========================================")
    log("STARTING BHAVU")
    log("==========================================")

    log(
        f"AWS SECRET: {SECRET_NAME} | "
        f"READ ONCE"
    )

    log(f"SYMBOL: {SYMBOL}")
    log(f"PRODUCT ID: {PRODUCT_ID}")
    log(
        f"MAX POSITION: "
        f"{MAX_POSITION} LOTS"
    )
    log(
        f"ORDER SIZE: "
        f"{ORDER_SIZE} LOTS"
    )
    log(
        f"STEP: "
        f"{price_string(STEP)}"
    )
    log(
        f"COUNTER STEP: "
        f"{price_string(COUNTER_STEP)}"
    )

    cmp = get_cmp()

    log(
        f"DELTA API OK | "
        f"CMP={price_string(cmp)}"
    )

    live_orders = recover_live_orders()

    if not live_orders:

        create_fresh_grid()

    else:

        log(
            "EXISTING BHAVU GRID FOUND | "
            "NO DUPLICATE GRID CREATED"
        )

    log("BHAVU RUNNING")

    while True:

        try:

            process_tracked_orders()

            time.sleep(
                POLL_SECONDS
            )

        except KeyboardInterrupt:

            log("BHAVU STOPPED")
            break

        except Exception as exc:

            log(
                f"MAIN LOOP ERROR | "
                f"{type(exc).__name__}: {exc}"
            )

            time.sleep(5)


if __name__ == "__main__":
    main()
