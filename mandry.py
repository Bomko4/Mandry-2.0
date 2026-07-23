import asyncio
import gspread
import socket
import random
import os
from aiohttp import web
from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from zoneinfo import ZoneInfo

socket.setdefaulttimeout(10)
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import BotCommand
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

load_dotenv()

def get_required_env(name: str) -> str:
    value = os.getenv(name)
    if value:
        return value
    raise SystemExit(f"Не задано обов'язкову змінну середовища: {name}")


TOKEN = get_required_env("BOT_TOKEN")
SHEET_NAME = os.getenv("SHEET_NAME", "Мандри бронь")
MENU_URL = os.getenv(
    "MENU_URL",
    "https://mandry-sup.choiceqr.com/menu?fbclid=PAdGRleAQvo3hleHRuA2FlbQIxMQABpyHGzMZ2j68WOIA7gDKCuCqXp30fz7ITK-gRUKSmhmg5-lJYxrIhrYtnu0A0_aem_1Qlu08flvE4mnL3TsIC_pw",
)
WEBAPP_URL = get_required_env("WEBAPP_URL")  # HTTPS URL where index.html is hosted

staff_chat_id_raw = os.getenv("STAFF_CHAT_ID")
if staff_chat_id_raw:
    try:
        STAFF_CHAT_ID = int(staff_chat_id_raw)
        print(f"[INFO] STAFF_CHAT_ID встановлено: {STAFF_CHAT_ID}")
    except ValueError as err:
        raise SystemExit("STAFF_CHAT_ID має бути числом, наприклад -1001234567890") from err
else:
    STAFF_CHAT_ID = None
    print("[WARNING] Змінна STAFF_CHAT_ID не задана. Повідомлення про бронювання не будуть надсилатися в чат персоналу.")

GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")
APP_TIMEZONE = ZoneInfo(os.getenv("BOT_TIMEZONE", "Europe/Kyiv"))
BLACKLIST_SHEET_NAME = os.getenv("BLACKLIST_SHEET_NAME", "Чорний Список")

COLUMNS = [
    "Сап білий", "Сап білий", "Сап білий", "Сап білий", "Сап білий", "Сап білий", "Сап білий", "Сап білий", "Сап білий", "Сап білий",
    "Сап червоний", "Сап червоний", "Сап червоний", "Сап червоний", "Сап червоний", "Сап червоний", "Сап червоний", "Сап червоний",
    "Сап Оранжевий", "Сап Оранжевий",
    "Каяк двомісний", "Каяк одномісний"
]

TIME_SLOTS = [
    "10:15-11:15", "11:30-12:30", "12:45-13:45", "14:00-15:00",
    "15:15-16:15", "16:30-17:30", "17:45-18:45", "19:00-20:00", "20:10-21:10"
]

MORNING_WINDOW = "05:45-08:00"

EQUIPMENT_OPTIONS = [
    ("Сап одномісний", "sup_single"),
    ("Сап двомісний", "sup_double"),
    ("Каяк одномісний", "kayak_single"),
    ("Каяк двомісний", "kayak_double"),
]

EQUIPMENT_LABELS = {
    "sup_single": "Сап одномісний",
    "sup_double": "Сап двомісний",
    "kayak_single": "Каяк одномісний",
    "kayak_double": "Каяк двомісний",
}

EQUIPMENT_COLUMN_GROUPS = {
    "sup_single": ["Сап білий"],
    "sup_double": ["Сап червоний", "Сап Оранжевий"],
    "kayak_single": ["Каяк одномісний"],
    "kayak_double": ["Каяк двомісний"],
}

try:
    gc = gspread.service_account(filename=GOOGLE_CREDENTIALS_PATH)
    sh = gc.open(SHEET_NAME)
except RefreshError as err:
    raise SystemExit(
        "\nПомилка авторизації Google Service Account: invalid_grant (account not found).\n"
        "Що перевірити:\n"
        f"1) У {GOOGLE_CREDENTIALS_PATH} поле client_email має існувати в Google Cloud IAM.\n"
        "2) Якщо service account видалений/перейменований - створіть новий ключ JSON.\n"
        "3) Увімкніть Google Drive API та Google Sheets API в тому самому проєкті.\n"
        "4) Відкрийте таблицю для client_email (доступ Editor).\n"
        f"\nДеталі: {err}"
    ) from err

bot = Bot(token=TOKEN)
dp = Dispatcher()

reminder_tasks: dict[str, asyncio.Task] = {}
morning_finalization_tasks: dict[str, asyncio.Task] = {}

# One lock per date-sheet to serialize structural writes (sheet repair, morning table
# creation, booking writes) and prevent duplicate rows / column collisions under load.
sheet_locks: dict[str, asyncio.Lock] = {}


def get_sheet_lock(date_str: str) -> asyncio.Lock:
    lock = sheet_locks.get(date_str)
    if lock is None:
        lock = asyncio.Lock()
        sheet_locks[date_str] = lock
    return lock

class Booking(StatesGroup):
    date = State()
    duration = State()
    equipment = State()
    time = State()
    quantity = State()
    name = State()
    phone = State()
    cancel_code = State()


def parse_booking_datetime(date_str: str, start_time_str: str) -> datetime:
    now = get_current_time()
    day = datetime.strptime(date_str, "%d.%m")
    start_time = datetime.strptime(start_time_str, "%H:%M")
    booking_dt = datetime(
        year=now.year,
        month=day.month,
        day=day.day,
        hour=start_time.hour,
        minute=start_time.minute,
        tzinfo=APP_TIMEZONE,
    )
    if booking_dt < now - timedelta(days=1):
        booking_dt = booking_dt.replace(year=now.year + 1)
    return booking_dt


def cancel_reminder_task(booking_code: str):
    task = reminder_tasks.pop(booking_code, None)
    if task and not task.done():
        task.cancel()


async def send_reminder(
    user_chat_id: int,
    booking_code: str,
    booking_date: str,
    booking_window: str,
    actual_equipment: str,
    equipment_note: str,
):
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(
            text="✅ Так, все в силі",
            callback_data=f"rem_yes_{booking_code}",
        )
    )
    builder.row(
        types.InlineKeyboardButton(
            text="❌ Ні, передумав",
            callback_data=f"rem_no_{booking_code}",
        )
    )

    await bot.send_message(
        chat_id=user_chat_id,
        text=(
            "⏰ Нагадування про бронювання\n"
            f"Код: {booking_code}\n"
            f"Дата: {booking_date}\n"
            f"Час: {booking_window}\n"
            f"Обладнання: {actual_equipment} {equipment_note}".rstrip()
        ),
        reply_markup=builder.as_markup(),
    )


async def reminder_worker(
    user_chat_id: int,
    booking_code: str,
    booking_date: str,
    booking_window: str,
    actual_equipment: str,
    equipment_note: str,
):
    try:
        start_time_str = booking_window.split("-")[0]
        booking_start_dt = parse_booking_datetime(booking_date, start_time_str)
        reminder_dt = booking_start_dt - timedelta(hours=2)
        delay_seconds = (reminder_dt - get_current_time()).total_seconds()

        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

        await send_reminder(
            user_chat_id=user_chat_id,
            booking_code=booking_code,
            booking_date=booking_date,
            booking_window=booking_window,
            actual_equipment=actual_equipment,
            equipment_note=equipment_note,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        reminder_tasks.pop(booking_code, None)


def schedule_booking_reminder(
    user_chat_id: int,
    booking_code: str,
    booking_date: str,
    booking_window: str,
    actual_equipment: str,
    equipment_note: str,
):
    cancel_reminder_task(booking_code)
    reminder_tasks[booking_code] = asyncio.create_task(
        reminder_worker(
            user_chat_id=user_chat_id,
            booking_code=booking_code,
            booking_date=booking_date,
            booking_window=booking_window,
            actual_equipment=actual_equipment,
            equipment_note=equipment_note,
        )
    )


def booking_code_exists(code: str) -> bool:
    marker = f"ID:{code}"
    for ws in get_booking_worksheets_from_today():
        values = ws.get_all_values()
        for row in values:
            for cell in row:
                if marker in cell:
                    return True
    return False


def get_all_booking_codes_from_today() -> set:
    codes = set()
    for ws in get_booking_worksheets_from_today():
        try:
            values = ws.get_all_values()
            for row in values:
                for cell in row:
                    if isinstance(cell, str) and cell.startswith("ID:"):
                        code_part = cell.split("\n")[0][3:].strip()
                        if code_part.isdigit() and len(code_part) == 5:
                            codes.add(code_part)
        except Exception:
            pass
    return codes


def generate_booking_code() -> str:
    existing_codes = get_all_booking_codes_from_today()
    for _ in range(1000):
        code = f"{random.randint(0, 99999):05d}"
        if code not in existing_codes:
            return code
    raise RuntimeError("Не вдалося згенерувати унікальний код бронювання")


def normalize_phone_number(phone: str) -> str:
    return "".join(ch for ch in phone if ch.isdigit())


def is_phone_blacklisted(phone: str) -> bool:
    normalized_phone = normalize_phone_number(phone)
    if not normalized_phone:
        return False

    blacklist_ws = get_first_worksheet()
    if blacklist_ws is None:
        return False

    for row in blacklist_ws.get_all_values():
        for cell in row:
            if normalize_phone_number(cell) == normalized_phone:
                return True

    return False


def get_equipment_name_for_column(col: int) -> str:
    """Map a 1-based sheet column to its equipment column name (col 1 = ВІКНО)."""
    idx = col - 2
    if 0 <= idx < len(COLUMNS):
        return COLUMNS[idx]
    return "Невідомо"


def find_and_clear_booking_by_code(code: str):
    marker = f"ID:{code}"
    updates_by_ws = {}
    booking_name = ""
    equipment_names: set[str] = set()
    booking_date = ""
    booking_time_window = ""

    for ws in get_booking_worksheets_from_today():
        try:
            values = ws.get_all_values()
        except Exception:
            continue

        # Find morning header row (if any) to detect morning bookings
        morning_header_row_idx = None
        for r_idx, row in enumerate(values):
            if row and isinstance(row[0], str) and row[0].strip().lower().startswith("ранков"):
                morning_header_row_idx = r_idx
                break

        updates_for_this_ws = []
        for r_idx, row in enumerate(values, start=1):
            for c_idx, cell in enumerate(row, start=1):
                if marker in cell:
                    if not booking_name:
                        lines = cell.split("\n")
                        if len(lines) > 1:
                            booking_name = lines[1].strip()
                        elif "|" in cell:
                            booking_name = cell.split("|", 1)[1].strip()

                    equipment_names.add(get_equipment_name_for_column(c_idx))

                    if not booking_date:
                        booking_date = ws.title

                    if not booking_time_window:
                        if morning_header_row_idx is not None and r_idx == morning_header_row_idx + 2:
                            booking_time_window = MORNING_WINDOW
                        else:
                            row_time_value = values[r_idx - 1][0].strip() if r_idx - 1 < len(values) else ""
                            if row_time_value and row_time_value != "Ранковий сплав":
                                booking_time_window = row_time_value

                    updates_for_this_ws.append((r_idx, c_idx))

        if updates_for_this_ws:
            updates_by_ws[ws] = updates_for_this_ws

    total_cleared = 0
    for ws, cells_to_clear in updates_by_ws.items():
        try:
            update_batch = [gspread.utils.rowcol_to_a1(r, c) for r, c in cells_to_clear]
            for cell_a1 in update_batch:
                ws.update(cell_a1, [[""]])
            total_cleared += len(update_batch)
        except Exception:
            pass

    return total_cleared, booking_name, ", ".join(sorted(equipment_names)), booking_date, booking_time_window


def get_target_columns_for_names(names: list[str]) -> list[int]:
    return [index + 2 for index, name in enumerate(COLUMNS) if name in names]


def find_free_column_for_duration(all_values, row_idx: int, duration: int, target_cols: list[int]):
    for col in target_cols:
        if _col_status(all_values, row_idx, duration, col) == "free":
            return col
    return None


def find_free_columns_for_duration(all_values, row_idx: int, duration: int, target_cols: list[int], quantity: int) -> list[int]:
    free_cols = []
    for col in target_cols:
        if _col_status(all_values, row_idx, duration, col) == "free":
            free_cols.append(col)
            if len(free_cols) >= quantity:
                return free_cols
    return free_cols


def build_time_window(start_index: int, duration: int) -> str:
    start_time = TIME_SLOTS[start_index].split('-')[0]
    start_dt = datetime.strptime(start_time, "%H:%M")
    end_time = (start_dt + timedelta(hours=duration)).strftime("%H:%M")
    return f"{start_time}-{end_time}"


def get_current_time() -> datetime:
    return datetime.now(APP_TIMEZONE)


def parse_sheet_date(date_str: str) -> datetime:
    now = get_current_time()
    booking_day = datetime.strptime(date_str, "%d.%m").replace(year=now.year, tzinfo=APP_TIMEZONE)
    if booking_day < now - timedelta(days=1):
        booking_day = booking_day.replace(year=now.year + 1)
    return booking_day


def get_morning_booking_deadline(date_str: str) -> datetime:
    booking_day = parse_sheet_date(date_str)
    pre_day = booking_day - timedelta(days=1)
    return pre_day.replace(hour=19, minute=0, second=0, microsecond=0)


def cancel_morning_finalization_task(date_str: str):
    task = morning_finalization_tasks.pop(date_str, None)
    if task and not task.done():
        task.cancel()


# Maps date_str -> set of chat_ids that booked morning rafting
morning_booking_chat_ids: dict[str, set[int]] = {}


def count_morning_booked_slots(ws) -> int:
    """Count total occupied (non-empty, non-dash, non-star) cells in the morning data row."""
    try:
        all_values = ws.get_all_values()
    except Exception:
        return 0

    header_row_idx = None
    for r_idx, row in enumerate(all_values):
        if row and isinstance(row[0], str) and row[0].strip().lower().startswith("ранков"):
            header_row_idx = r_idx
            break

    if header_row_idx is None:
        return 0

    data_row_idx = header_row_idx + 1
    if data_row_idx >= len(all_values):
        return 0

    count = 0
    for cell in all_values[data_row_idx][1:]:
        v = cell.strip()
        if v and v not in ("-", "*"):
            count += 1
    return count


def is_morning_confirmed(ws) -> bool:
    """True if 6 or more slots are booked in the morning table."""
    return count_morning_booked_slots(ws) >= 6


async def finalize_morning_booking_if_needed(date_str: str):
    ws = get_or_create_sheet(date_str)
    ensure_morning_table(ws)

    booked_slots = count_morning_booked_slots(ws)

    if booked_slots >= 6:
        # Enough people — confirm to all registered chat_ids
        chat_ids = morning_booking_chat_ids.get(date_str, set())
        confirm_message = (
            f"✅ Ранковий сплав на {date_str} відбудеться! Набралося достатньо учасників.\n"
            "Чекаємо вас о 05:45!"
        )
        for chat_id in chat_ids:
            try:
                await bot.send_message(chat_id=chat_id, text=confirm_message)
            except Exception:
                pass
        return

    # Less than 6 — notify but DO NOT delete bookings
    chat_ids = morning_booking_chat_ids.get(date_str, set())
    if not chat_ids:
        return

    cancel_message = (
        f"⚠️ Ранковий сплав на {date_str}: до 19:00 не набралося 6 учасників ({booked_slots} з 6).\n"
        "Бронювання залишається, але сплав може не відбутися. Ми зв'яжемося з вами для підтвердження."
    )
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=cancel_message)
        except Exception:
            pass


def schedule_morning_finalization(date_str: str):
    deadline = get_morning_booking_deadline(date_str)
    now = get_current_time()
    if now >= deadline:
        return

    existing_task = morning_finalization_tasks.get(date_str)
    if existing_task and not existing_task.done():
        return

    async def worker():
        try:
            delay_seconds = (deadline - get_current_time()).total_seconds()
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            await finalize_morning_booking_if_needed(date_str)
        except asyncio.CancelledError:
            raise
        finally:
            morning_finalization_tasks.pop(date_str, None)

    morning_finalization_tasks[date_str] = asyncio.create_task(worker())


def get_first_worksheet():
    try:
        return sh.get_worksheet(0)
    except (IndexError, gspread.exceptions.WorksheetNotFound):
        return None


def get_booking_worksheets_from_today() -> list:
    now = get_current_time()
    today = now.date()
    relevant_sheets = []

    for ws in sh.worksheets():
        try:
            sheet_day = datetime.strptime(ws.title, "%d.%m").replace(year=now.year)
        except ValueError:
            continue

        if sheet_day.date() < today and (today - sheet_day.date()).days > 1:
            sheet_day = sheet_day.replace(year=now.year + 1)

        if sheet_day.date() >= today:
            relevant_sheets.append((sheet_day, ws))

    relevant_sheets.sort(key=lambda item: item[0])
    return [ws for _, ws in relevant_sheets]


def _worksheet_date_key(ws):
    try:
        return datetime.strptime(ws.title, "%d.%m")
    except ValueError:
        return None


def reorder_booking_worksheets():
    first_ws = get_first_worksheet()
    if first_ws is None:
        return

    dated_worksheets = []
    other_worksheets = []

    for ws in sh.worksheets():
        if ws.id == first_ws.id:
            continue

        date_key = _worksheet_date_key(ws)
        if date_key is None:
            other_worksheets.append(ws)
        else:
            dated_worksheets.append((date_key, ws))

    ordered_worksheets = [first_ws]
    ordered_worksheets.extend(ws for _, ws in sorted(dated_worksheets, key=lambda item: item[0]))
    ordered_worksheets.extend(other_worksheets)

    sh.reorder_worksheets(ordered_worksheets)


def resolve_equipment_booking(requested_equipment: str, all_values, row_idx: int, duration: int):
    preferred_names = EQUIPMENT_COLUMN_GROUPS[requested_equipment]
    preferred_target_cols = get_target_columns_for_names(preferred_names)

    free_pref = find_free_columns_for_duration(all_values, row_idx, duration, preferred_target_cols, 1)
    if free_pref:
        col = free_pref[0]
        return {
            "resolved_equipment": requested_equipment,
            "equip_col": col,
            "actual_equipment": EQUIPMENT_LABELS[requested_equipment],
            "note": "",
        }

    if requested_equipment == "sup_single":
        double_target_cols = get_target_columns_for_names(EQUIPMENT_COLUMN_GROUPS["sup_double"])
        free_double = find_free_columns_for_duration(all_values, row_idx, duration, double_target_cols, 1)
        if free_double:
            col = free_double[0]
            return {
                "resolved_equipment": "sup_double",
                "equip_col": col,
                "actual_equipment": "Сап двомісний",
                "note": "(одна людина)",
            }

    return None

def _get_cell(all_values, row_idx: int, col: int) -> str:
    """Return stripped cell value (1-based row_idx, 1-based col)."""
    row = all_values[row_idx - 1] if row_idx - 1 < len(all_values) else []
    return row[col - 1].strip() if col - 1 < len(row) else ""


def _col_status(all_values, row_idx: int, duration: int, col: int) -> str:
    """Classify a column block as 'free', 'dash', 'star', or 'booked'.

    Rules (checked across all rows in the duration block):
    - If any row has '-'  → 'dash'   (live-queue reserved)
    - If any row has '*'  → 'star'   (closed)
    - If any row is non-empty (and not '-'/'*') → 'booked'
    - All rows empty → 'free'
    """
    has_dash = has_star = has_booking = False
    for offset in range(duration):
        v = _get_cell(all_values, row_idx + offset, col)
        if v == "-":
            has_dash = True
        elif v == "*":
            has_star = True
        elif v:
            has_booking = True

    if has_dash:
        return "dash"
    if has_star:
        return "star"
    if has_booking:
        return "booked"
    return "free"


def is_weather_blocked_slot(all_values, row_idx: int, duration: int, target_cols: list[int]) -> bool:
    """True when every target column in the block is '*'."""
    if not target_cols:
        return False
    return all(_col_status(all_values, row_idx, duration, col) == "star" for col in target_cols)


def is_live_queue_only_slot(all_values, row_idx: int, duration: int, target_cols: list[int]) -> bool:
    """True when no column is 'free' and at least one is 'dash' (or all are booked/dash mix)."""
    if not target_cols:
        return False
    statuses = [_col_status(all_values, row_idx, duration, col) for col in target_cols]
    has_free = any(s == "free" for s in statuses)
    has_dash = any(s == "dash" for s in statuses)
    return not has_free and has_dash


def are_all_non_dash_columns_occupied(all_values, row_idx: int, duration: int, target_cols: list[int]) -> bool:
    """True when no column is 'free' (all are booked/dash/star)."""
    if not target_cols:
        return True
    statuses = [_col_status(all_values, row_idx, duration, col) for col in target_cols]
    return not any(s == "free" for s in statuses)

def get_or_create_sheet(date_str):
    try:
        ws = sh.worksheet(date_str)
        # Read only the first 10 rows of column A (header + 9 time slots).
        # Reading the full column would include morning table rows which must not be touched here.
        existing_col_a = ws.col_values(1)[:len(TIME_SLOTS) + 1]
        for index, slot in enumerate(TIME_SLOTS, start=2):
            current_value = existing_col_a[index - 1].strip() if len(existing_col_a) >= index else ""
            if current_value != slot:
                ws.update(gspread.utils.rowcol_to_a1(index, 1), [[slot]])
        return ws
    except gspread.exceptions.WorksheetNotFound:
        # Re-check after acquiring intent to create — another concurrent call may have
        # already created it while we were checking.
        try:
            return sh.worksheet(date_str)
        except gspread.exceptions.WorksheetNotFound:
            pass
        new_ws = sh.add_worksheet(title=date_str, rows="100", cols="30")
        headers = ["ВІКНО"] + COLUMNS
        new_ws.update('A1', [headers])
        time_col = [[t] for t in TIME_SLOTS]
        end_row = 1 + len(TIME_SLOTS)
        new_ws.update(f'A2:A{end_row}', time_col)
        reorder_booking_worksheets()
        return new_ws


def ensure_morning_table(ws) -> list:
    all_vals = ws.get_all_values()

    # Find ALL morning header rows (to detect duplicates)
    header_rows = [
        r_idx for r_idx, row in enumerate(all_vals)
        if row and isinstance(row[0], str) and row[0].strip().lower().startswith("ранков")
    ]

    # Remove duplicate morning tables (keep only the first)
    if len(header_rows) > 1:
        rows_to_delete = []
        for dup_header_idx in header_rows[1:]:
            rows_to_delete.append(dup_header_idx + 1)  # 1-based
            data_row = dup_header_idx + 1
            if data_row < len(all_vals):
                rows_to_delete.append(data_row + 1)  # 1-based
        # Delete from bottom up to preserve row indices
        for row_1based in sorted(set(rows_to_delete), reverse=True):
            try:
                ws.delete_rows(row_1based)
            except Exception:
                pass
        all_vals = ws.get_all_values()
        header_rows = [header_rows[0]]

    if header_rows:
        header_row_idx = header_rows[0]
        # data row is immediately after header (0-based idx → 1-based = +1, then +1 for the next row)
        data_row_1based = header_row_idx + 2
        if data_row_1based <= len(all_vals):
            current_value = all_vals[data_row_1based - 1][0].strip() if all_vals[data_row_1based - 1] else ""
            if current_value != MORNING_WINDOW:
                ws.update(gspread.utils.rowcol_to_a1(data_row_1based, 1), [[MORNING_WINDOW]])
                return ws.get_all_values()
        else:
            # Data row missing — append it
            ws.append_rows([[MORNING_WINDOW] + ["" for _ in COLUMNS]])
            return ws.get_all_values()
        return all_vals

    # No morning table at all — append both rows at once
    ws.append_rows([
        ["Ранковий сплав"] + COLUMNS,
        [MORNING_WINDOW] + ["" for _ in COLUMNS],
    ])
    return ws.get_all_values()


def is_morning_weather_blocked(ws) -> bool:
    """Return True if all non-header cells in the morning table are filled with '*'."""
    try:
        all_values = ws.get_all_values()
    except Exception:
        return False

    header_row_idx = None
    for r_idx, row in enumerate(all_values):
        if row and isinstance(row[0], str) and row[0].strip().lower().startswith("ранков"):
            header_row_idx = r_idx
            break

    if header_row_idx is None:
        return False

    data_row_idx = header_row_idx + 1
    if data_row_idx >= len(all_values):
        return False

    row = all_values[data_row_idx]
    data_cells = row[1:]  # skip the time window cell
    if not data_cells:
        return False

    return all(cell.strip() == "*" for cell in data_cells)


def is_weather_blocked_sheet(all_values) -> bool:
    all_cols = list(range(2, len(COLUMNS) + 2))  # columns B onwards (1-based)
    for row_idx in range(2, len(TIME_SLOTS) + 2):  # rows 2..10
        for col in all_cols:
            v = _get_cell(all_values, row_idx, col)
            if v != "*":
                return False
    return True

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()

    reply_keyboard = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="📚 Забронювати", web_app=types.WebAppInfo(url=WEBAPP_URL))],
            [types.KeyboardButton(text="❌ Скасувати бронювання")],
            [types.KeyboardButton(text="📋 Правила користування"), types.KeyboardButton(text="🚨 Краш ліст")],
            [types.KeyboardButton(text="🍽️ Меню")],
            [types.KeyboardButton(text="📞 Контакти")]
        ],
        resize_keyboard=True
    )

    await message.answer("Головне меню:", reply_markup=reply_keyboard)

@dp.message(F.text == "📚 Забронювати")
async def start_booking_from_menu(message: types.Message, state: FSMContext):
    builder = InlineKeyboardBuilder()
    now = get_current_time()
    left_column_dates = [(now + timedelta(days=day_offset)).strftime("%d.%m") for day_offset in range(7)]
    right_column_dates = [(now + timedelta(days=day_offset)).strftime("%d.%m") for day_offset in range(7, 14)]

    for left_date, right_date in zip(left_column_dates, right_column_dates):
        builder.row(
            types.InlineKeyboardButton(text=left_date, callback_data=f"date_{left_date}"),
            types.InlineKeyboardButton(text=right_date, callback_data=f"date_{right_date}"),
        )

    builder.row(types.InlineKeyboardButton(text="➡️", callback_data="dates_next"))

    await message.answer("Оберіть дату:", reply_markup=builder.as_markup())
    await state.set_state(Booking.date)


@dp.callback_query(F.data == "dates_next")
async def show_next_dates(callback: types.CallbackQuery):
    now = get_current_time()
    left_column_dates = [(now + timedelta(days=day_offset)).strftime("%d.%m") for day_offset in range(14, 21)]
    right_column_dates = [(now + timedelta(days=day_offset)).strftime("%d.%m") for day_offset in range(21, 28)]

    builder = InlineKeyboardBuilder()
    for left_date, right_date in zip(left_column_dates, right_column_dates):
        builder.row(
            types.InlineKeyboardButton(text=left_date, callback_data=f"date_{left_date}"),
            types.InlineKeyboardButton(text=right_date, callback_data=f"date_{right_date}"),
        )

    builder.row(types.InlineKeyboardButton(text="⬅️", callback_data="dates_prev"))

    await callback.message.edit_text("Оберіть дату:", reply_markup=builder.as_markup())
    await callback.answer()


@dp.callback_query(F.data == "dates_prev")
async def show_prev_dates(callback: types.CallbackQuery):
    now = get_current_time()
    left_column_dates = [(now + timedelta(days=day_offset)).strftime("%d.%m") for day_offset in range(7)]
    right_column_dates = [(now + timedelta(days=day_offset)).strftime("%d.%m") for day_offset in range(7, 14)]

    builder = InlineKeyboardBuilder()
    for left_date, right_date in zip(left_column_dates, right_column_dates):
        builder.row(
            types.InlineKeyboardButton(text=left_date, callback_data=f"date_{left_date}"),
            types.InlineKeyboardButton(text=right_date, callback_data=f"date_{right_date}"),
        )

    builder.row(types.InlineKeyboardButton(text="➡️", callback_data="dates_next"))

    await callback.message.edit_text("Оберіть дату:", reply_markup=builder.as_markup())
    await callback.answer()

@dp.message(F.text == "🍽️ Меню")
async def show_prices(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="Відкрити меню", url=MENU_URL))
    await message.answer("Натисніть кнопку нижче, щоб відкрити меню:", reply_markup=builder.as_markup())


@dp.message(F.text == "❌ Скасувати бронювання")
async def start_cancel_booking(message: types.Message, state: FSMContext):
    await state.set_state(Booking.cancel_code)
    await message.answer("Введіть 5-значний номер бронювання для скасування:")

@dp.message(F.text == "📞 Контакти")
async def show_contacts(message: types.Message):
    await message.answer(
        "Контакти:\n"
        "Телефон: <a href='tel:+380989055753'>+380 98 905 57 53</a>\n"
        "Пошта: <a href='mailto:mandry70625@gmail.com'>mandry70625@gmail.com</a>\n"
        "Inst: <a href='https://www.instagram.com/mandry.sup/'>@mandry.sup</a>",
        parse_mode="HTML"
    )

@dp.message(F.text == "📋 Правила користування")
async def show_rules(message: types.Message):
    await message.answer(
        "📋 Правила користування:\n\n"
        "1. Дотримуйтесь безпеки на воді\n"
        "2. Використовуйте рятувальний жилет\n"
        "3. Не перевищуйте дозволену вагу\n"
        "4. Приходьте за 15 хвилин до старту\n"
        "5. Слідуйте інструкціям персоналу\n"
        "6. Повідомте про травми чи поломки"
    )

@dp.message(F.text == "🚨 Краш ліст")
async def show_crash_list(message: types.Message):
    crash_list_text = (
        "🚨 <b>КРАШ ЛІСТ</b> - Ціна відшкодування\n\n"
        "<b>Надувні SUP дошки:</b>\n"
        "• AQUA MARINA MONSTER BT-21 - \n15 500 грн\n"
        "• AQUA MARINA MONSTER BT-23 - \n15 500 грн\n"
        "• AQUA MARINA PURE AIR - 9 100 грн\n\n"
        "<b>Надувні каяки:</b>\n"
        "• Aqua Marina BETTA BE-312 - 12 300 грн\n"
        "• Aqua Marina LAXO LA-380 - 17 700 грн\n\n"
        "<b>Аксесуари:</b>\n"
        "• Весло під сапборд - 2 200 грн\n"
        "• Весло під каяк - 2 800 грн\n"
        "• Плавник під сап-дошку - 600 грн\n"
        "• Плавник до каяка - 700 грн\n"
        "• Сидіння до каяка - 1 200 грн\n"
        "• Гермомішок - 400 грн\n"
        "• Страхувальний лиш для SUP - 650 грн\n\n"
        "<i>У разі пошкодження обладнання до вас буде застосована відповідна компенсація.</i>"
    )
    await message.answer(crash_list_text, parse_mode="HTML")

@dp.message(F.web_app_data)
async def process_web_app_data(message: types.Message, state: FSMContext):
    import json

    try:
        payload = json.loads(message.web_app_data.data)
    except (ValueError, AttributeError):
        await message.answer("❌ Не вдалося прочитати дані з міні-застосунку.")
        return

    kind = payload.get("type")

    # ---------- CANCELLATION ----------
    if kind == "cancel":
        code = str(payload.get("cancel_code", "")).strip()
        if not (code.isdigit() and len(code) == 5):
            await message.answer("❌ Невірний формат коду бронювання.")
            return

        await message.answer("Відбувається процес скасування!\nДякуємо за очікування💙")
        cleared_count, booking_name, equipment_names, booking_date, booking_time_window = find_and_clear_booking_by_code(code)

        if cleared_count == 0:
            await message.answer("❌ Бронювання з таким номером не знайдено.")
            return

        cancel_reminder_task(code)
        cancellation_dt = get_current_time().strftime("%d.%m.%Y %H:%M")
        cancel_notify = (
            "Скасування бронювання! (Mini App)\n"
            f"Код: {code}\n"
            f"Клієнт: {booking_name if booking_name else 'Невідомо'}\n"
            f"Дата бронювання: {booking_date if booking_date else 'Невідомо'}\n"
            f"Час бронювання: {booking_time_window if booking_time_window else 'Невідомо'}\n"
            f"Обладнання: {equipment_names if equipment_names else 'Невідомо'}\n"
            f"Очищено слотів: {cleared_count}\n"
            f"Скасовано: {cancellation_dt}"
        )
        if STAFF_CHAT_ID is not None:
            try:
                await bot.send_message(chat_id=STAFF_CHAT_ID, text=cancel_notify)
            except Exception as e:
                print(f"[ERROR] Помилка при надсиланні повідомлення про скасування: {e}")

        await message.answer(f"✅ Бронювання {code} скасовано.")
        return

    # ---------- BOOKING ----------
    if kind != "booking":
        await message.answer("❌ Невідомий тип запиту з міні-застосунку.")
        return

    selected_date = payload.get("date")          # expected "dd.mm"
    duration_raw = payload.get("duration")        # "1" | "2" | "morning"
    equipment = payload.get("equipment")           # sup_single | sup_double | kayak_single | kayak_double
    quantity = int(payload.get("quantity", 1))
    time_slot = payload.get("time")
    client_name = payload.get("full_name", "").strip()
    phone = payload.get("phone", "")

    if not (selected_date and duration_raw and equipment and client_name):
        await message.answer("❌ Дані бронювання неповні. Спробуйте ще раз через міні-застосунок.")
        return
    if equipment not in EQUIPMENT_COLUMN_GROUPS:
        await message.answer("❌ Невідомий тип обладнання.")
        return
    if not phone:
        await message.answer("❌ Не вдалося отримати номер телефону. Спробуйте ще раз і поділіться контактом.")
        return
    if not phone.startswith("+"):
        phone = "+" + phone

    is_morning = (duration_raw == "morning")
    ws = get_or_create_sheet(selected_date)
    all_values = ws.get_all_values()

    if is_weather_blocked_sheet(all_values):
        await message.answer("Прогнозується негода, тому ми зачинені на цю дату🏄‍♂️")
        return

    data = {"date": selected_date, "equipment": equipment, "quantity": quantity}

    if is_morning:
        ensure_morning_table(ws)
        if is_morning_weather_blocked(ws):
            await message.answer("На цю дату не плануємо ранковий сплав")
            return
        confirmed = is_morning_confirmed(ws)
        past_deadline = get_current_time() >= get_morning_booking_deadline(selected_date)
        if past_deadline and not confirmed:
            await message.answer("Ранковий сплав на цю дату недоступний: до 19:00 не набралося 6 учасників.")
            return

        data["morning"] = True
        data["duration"] = 1
    else:
        duration = int(duration_raw)
        if not time_slot or time_slot not in TIME_SLOTS:
            await message.answer("❌ Невірний часовий слот.")
            return
        start_index = TIME_SLOTS.index(time_slot)
        row_idx = start_index + 2

        if row_idx + duration - 1 > len(TIME_SLOTS) + 1:
            await message.answer("❌ Для цієї тривалості оберіть раніший старт.")
            return

        preferred_names = EQUIPMENT_COLUMN_GROUPS[equipment]
        target_cols = get_target_columns_for_names(preferred_names)

        if is_live_queue_only_slot(all_values, row_idx, duration, target_cols):
            await message.answer("⏳ На цей час доступна лише жива черга. Скористайтесь текстовим меню бота.")
            return

        booking_resolution = resolve_equipment_booking(equipment, all_values, row_idx, duration)
        if not booking_resolution:
            await message.answer("❌ Немає вільних місць на обраний час. Спробуйте інший слот.")
            return

        data.update(
            duration=duration,
            time_row=row_idx,
            equip_col=booking_resolution["equip_col"],
            actual_equipment=booking_resolution["actual_equipment"],
            equipment_note=booking_resolution["note"],
            resolved_equipment=booking_resolution.get("resolved_equipment", equipment),
        )

        if equipment.startswith("sup_") and quantity > 1:
            single_cols = get_target_columns_for_names(EQUIPMENT_COLUMN_GROUPS["sup_single"])
            double_cols = get_target_columns_for_names(EQUIPMENT_COLUMN_GROUPS["sup_double"])
            if equipment == "sup_single":
                primary_cols, fallback_cols = single_cols, double_cols
            else:
                primary_cols, fallback_cols = double_cols, single_cols

            free_primary = find_free_columns_for_duration(all_values, row_idx, duration, primary_cols, quantity)
            needed = quantity - len(free_primary)
            free_fallback = find_free_columns_for_duration(all_values, row_idx, duration, fallback_cols, needed) if needed > 0 else []
            free_cols = free_primary + free_fallback

            if len(free_cols) < quantity:
                await message.answer("❌ На цей час немає достатньої кількості вільних сапів.")
                return
            data["equip_cols"] = free_cols

    await finalize_booking(message, state, phone, client_name, data)


@dp.message(Booking.cancel_code)
async def process_cancel_booking(message: types.Message, state: FSMContext):
    code = message.text.strip()
    if not (code.isdigit() and len(code) == 5):
        await message.answer("Невірний формат. Введіть саме 5 цифр, наприклад: 04231")
        return

    await message.answer("Відбувається процес скасування!\nДякуємо за очікування💙")

    cleared_count, booking_name, equipment_names, booking_date, booking_time_window = find_and_clear_booking_by_code(code)
    if cleared_count == 0:
        await message.answer("❌ Бронювання з таким номером не знайдено.")
        await state.clear()
        return

    cancel_reminder_task(code)

    cancellation_dt = get_current_time().strftime("%d.%m.%Y %H:%M")
    cancel_notify = (
        "Скасування бронювання!\n"
        f"Код: {code}\n"
        f"Клієнт: {booking_name if booking_name else 'Невідомо'}\n"
        f"Дата бронювання: {booking_date if booking_date else 'Невідомо'}\n"
        f"Час бронювання: {booking_time_window if booking_time_window else 'Невідомо'}\n"
        f"Обладнання: {equipment_names if equipment_names else 'Невідомо'}\n"
        f"Очищено слотів: {cleared_count}\n"
        f"Скасовано: {cancellation_dt}"
    )
    if STAFF_CHAT_ID is not None:
        try:
            print(f"[INFO] Надсилаємо повідомлення про скасування в чат {STAFF_CHAT_ID}")
            await bot.send_message(chat_id=STAFF_CHAT_ID, text=cancel_notify)
            print(f"[INFO] Повідомлення про скасування успішно надіслано")
        except Exception as e:
            print(f"[ERROR] Помилка при надсиланні повідомлення про скасування: {e}")
    else:
        print("[WARNING] STAFF_CHAT_ID не задано")

    await message.answer(f"✅ Бронювання {code} скасовано.")
    await state.clear()


@dp.callback_query(F.data.startswith("rem_yes_"))
async def process_reminder_yes(callback: types.CallbackQuery):
    await callback.message.edit_text("Дякуємо за підтвердження. Чекаємо вас.")
    await callback.answer()


@dp.callback_query(F.data.startswith("rem_no_"))
async def process_reminder_no(callback: types.CallbackQuery):
    code = callback.data.split("_", 2)[2]
    cleared_count, booking_name, equipment_names, booking_date, booking_time_window = find_and_clear_booking_by_code(code)
    cancel_reminder_task(code)

    if cleared_count == 0:
        await callback.message.edit_text("❌ Бронювання вже неактуальне або не знайдено.")
        await callback.answer()
        return

    cancellation_dt = get_current_time().strftime("%d.%m.%Y %H:%M")
    if STAFF_CHAT_ID is not None:
        try:
            print(f"[INFO] Надсилаємо повідомлення про скасування з нагадування в чат {STAFF_CHAT_ID}")
            await bot.send_message(
                chat_id=STAFF_CHAT_ID,
                text=(
                    "Скасування з нагадування!\n"
                    f"Код: {code}\n"
                    f"Клієнт: {booking_name if booking_name else 'Невідомо'}\n"
                    f"Дата бронювання: {booking_date if booking_date else 'Невідомо'}\n"
                    f"Час бронювання: {booking_time_window if booking_time_window else 'Невідомо'}\n"
                    f"Обладнання: {equipment_names if equipment_names else 'Невідомо'}\n"
                    "Клієнт натиснув: Ні, передумав\n"
                    f"Скасовано: {cancellation_dt}"
                ),
            )
            print(f"[INFO] Повідомлення про скасування з нагадування успішно надіслано")
        except Exception as e:
            print(f"[ERROR] Помилка при надсиланні повідомлення про скасування з нагадування: {e}")
    else:
        print("[WARNING] STAFF_CHAT_ID не задано")

    await callback.message.edit_text("✅ Бронювання скасовано. Якщо захочете, можете створити нове у головному меню.")
    await callback.answer()

@dp.callback_query(F.data.startswith("date_"))
async def process_date(callback: types.CallbackQuery, state: FSMContext):
    selected_date = callback.data.split("_", 1)[1]
    ws = get_or_create_sheet(selected_date)
    all_values = ws.get_all_values()

    if is_weather_blocked_sheet(all_values):
        await callback.message.edit_text("Прогнозується негода, тому ми зачинені на цю дату🏄‍♂️")
        await state.clear()
        await callback.answer()
        return

    await state.update_data(date=selected_date)

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="1 година", callback_data="dur_1"))
    builder.row(types.InlineKeyboardButton(text="2 години", callback_data="dur_2"))
    builder.row(types.InlineKeyboardButton(text="Ранковий сплав", callback_data="morning"))

    await callback.message.edit_text("Скільки часу хочете плавати?", reply_markup=builder.as_markup())
    await state.set_state(Booking.duration)
    await callback.answer()

@dp.callback_query(F.data.startswith("dur_"))
async def process_duration(callback: types.CallbackQuery, state: FSMContext):
    duration = int(callback.data.split("_")[1])
    await state.update_data(duration=duration)

    builder = InlineKeyboardBuilder()
    for label, code in EQUIPMENT_OPTIONS:
        builder.row(types.InlineKeyboardButton(text=label, callback_data=f"eq_{code}"))

    await callback.message.edit_text("Оберіть обладнання:", reply_markup=builder.as_markup())
    await state.set_state(Booking.equipment)


@dp.callback_query(F.data == "morning")
async def process_morning(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_date = data.get('date')

    if selected_date:
        try:
            ws = get_or_create_sheet(selected_date)
            ensure_morning_table(ws)

            if is_morning_weather_blocked(ws):
                await callback.message.edit_text("На цю дату не плануємо ранковий сплав")
                await state.clear()
                await callback.answer()
                return

            confirmed = is_morning_confirmed(ws)
        except Exception:
            confirmed = False

        past_deadline = get_current_time() >= get_morning_booking_deadline(selected_date)

        if past_deadline and not confirmed:
            await callback.message.edit_text(
                "Ранковий сплав на цю дату недоступний: до 19:00 не набралося 6 учасників."
            )
            await state.clear()
            await callback.answer()
            return

    await state.update_data(morning=True)

    builder = InlineKeyboardBuilder()
    for label, code in EQUIPMENT_OPTIONS:
        builder.row(types.InlineKeyboardButton(text=label, callback_data=f"eq_{code}"))

    await callback.message.edit_text("Оберіть обладнання для ранкового сплаву:", reply_markup=builder.as_markup())
    await state.set_state(Booking.equipment)
    await callback.answer()

@dp.callback_query(F.data.startswith("eq_"))
async def process_equip(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(equipment=callback.data.split("_", 1)[1])

    data = await state.get_data()

    # Morning booking: skip time selection, go straight to quantity/name
    if data.get('morning'):
        if data.get('equipment', '').startswith("sup_"):
            builder = InlineKeyboardBuilder()
            for value in range(1, 11):
                builder.button(text=str(value), callback_data=f"qty_{value}")
            builder.adjust(5, 5)
            await callback.message.edit_text("Скільки сапів бронюєте?", reply_markup=builder.as_markup())
            await state.set_state(Booking.quantity)
        else:
            await callback.message.edit_text("Введіть ваше Прізвище та Ім'я:")
            await state.set_state(Booking.name)
        await callback.answer()
        return

    duration = int(data.get('duration', 1))
    selected_date = data.get('date')
    current_time = get_current_time()
    today_str = current_time.strftime("%d.%m")
    now_time = current_time.time()

    ws = get_or_create_sheet(selected_date)
    all_values = ws.get_all_values()

    builder = InlineKeyboardBuilder()
    max_start_index = len(TIME_SLOTS) - duration
    visible_slots = 0

    preferred_names = EQUIPMENT_COLUMN_GROUPS[data['equipment']]
    target_cols = get_target_columns_for_names(preferred_names)

    for start_index in range(max_start_index + 1):
        start_time_str = TIME_SLOTS[start_index].split('-')[0]
        start_time = datetime.strptime(start_time_str, "%H:%M").time()
        if selected_date == today_str and start_time <= now_time:
            continue

        row_idx = start_index + 2
        window_label = build_time_window(start_index, duration)

        # Skip weather-blocked (*) slots entirely — don't show them
        if is_weather_blocked_slot(all_values, row_idx, duration, target_cols):
            continue

        if is_live_queue_only_slot(all_values, row_idx, duration, target_cols):
            builder.row(types.InlineKeyboardButton(text=window_label, callback_data=f"tmidx_{start_index}"))
            visible_slots += 1
            continue

        booking_resolution = resolve_equipment_booking(data['equipment'], all_values, row_idx, duration)
        if booking_resolution:
            builder.row(types.InlineKeyboardButton(text=window_label, callback_data=f"tmidx_{start_index}"))
            visible_slots += 1

    if visible_slots == 0:
        # Determine why: all slots are dash-only (live queue) or simply all gone
        has_live_queue = False
        for start_index in range(max_start_index + 1):
            start_time_str = TIME_SLOTS[start_index].split('-')[0]
            start_time = datetime.strptime(start_time_str, "%H:%M").time()
            if selected_date == today_str and start_time <= now_time:
                continue
            row_idx = start_index + 2
            if not is_weather_blocked_slot(all_values, row_idx, duration, target_cols) and \
               are_all_non_dash_columns_occupied(all_values, row_idx, duration, target_cols):
                statuses = [_col_status(all_values, row_idx, duration, col) for col in target_cols]
                if any(s == "dash" for s in statuses):
                    has_live_queue = True
                    break

        if has_live_queue:
            await callback.message.edit_text("На жаль, бронювання вже недоступне — наразі працюємо лише в форматі живої черги.\nБудемо раді бачити вас на сплаві!🏄‍♂️")
        else:
            await callback.message.edit_text("На сьогодні вільні часові слоти вже завершилися. Оберіть іншу дату.")
        await state.clear()
        return

    await callback.message.edit_text("Оберіть час:", reply_markup=builder.as_markup())
    await state.set_state(Booking.time)


@dp.callback_query(F.data.startswith("tmidx_"))
async def process_time(callback: types.CallbackQuery, state: FSMContext):
    start_index = int(callback.data.split("_")[1])
    data = await state.get_data()
    duration = int(data.get('duration', 1))

    ws = get_or_create_sheet(data['date'])
    all_values = ws.get_all_values()

    row_idx = start_index + 2

    if row_idx + duration - 1 > len(TIME_SLOTS) + 1:
        await callback.answer("❌ Для цієї тривалості оберіть раніший старт!", show_alert=True)
        return

    preferred_names = EQUIPMENT_COLUMN_GROUPS[data['equipment']]
    target_cols = get_target_columns_for_names(preferred_names)

    if is_live_queue_only_slot(all_values, row_idx, duration, target_cols):
        await callback.answer("⏳ На цей час доступна лише жива черга", show_alert=True)
        return

    booking_resolution = resolve_equipment_booking(data['equipment'], all_values, row_idx, duration)

    if not booking_resolution:
        await callback.answer("❌ Немає вільних місць на обраний час!", show_alert=True)
    else:
        requested_equipment = data['equipment']
        await state.update_data(
            time_row=row_idx,
            equip_col=booking_resolution["equip_col"],
            duration=duration,
            actual_equipment=booking_resolution["actual_equipment"],
            equipment_note=booking_resolution["note"],
            resolved_equipment=booking_resolution.get("resolved_equipment", requested_equipment),
        )
        if requested_equipment.startswith("sup_"):
            builder = InlineKeyboardBuilder()
            for value in range(1, 11):
                builder.button(text=str(value), callback_data=f"qty_{value}")
            builder.adjust(5, 5)

            await callback.message.edit_text("Скільки сапів бронюєте?", reply_markup=builder.as_markup())
            await state.set_state(Booking.quantity)
        else:
            await callback.message.edit_text("Введіть ваше Прізвище та Ім'я:")
            await state.set_state(Booking.name)


@dp.callback_query(F.data.startswith("qty_"))
async def process_quantity(callback: types.CallbackQuery, state: FSMContext):
    quantity = int(callback.data.split("_")[1])
    if quantity < 1 or quantity > 10:
        await callback.answer("❌ Оберіть кількість від 1 до 10", show_alert=True)
        return

    data = await state.get_data()
    original_equipment = data.get('equipment')
    if not original_equipment or not original_equipment.startswith("sup_"):
        await callback.answer("❌ Цей крок доступний лише для сапів", show_alert=True)
        return

    ws = get_or_create_sheet(data['date'])
    all_values = ws.get_all_values()

    if data.get('morning'):
        all_vals = ensure_morning_table(ws)

        header_row_idx = None
        for r_idx, row in enumerate(all_values):
            if row and len(row) > 0 and isinstance(row[0], str) and row[0].strip().lower().startswith("ранков"):
                header_row_idx = r_idx
                break

        if header_row_idx is None:
            await callback.answer("❌ Не вдалося знайти ранкову таблицю", show_alert=True)
            return

        row_idx = header_row_idx + 1 + 1
    else:
        row_idx = int(data.get('time_row', 0))

    duration = int(data.get('duration', 1)) if not data.get('morning') else 1

    # Collect free columns: prefer the originally-requested type, fill remainder from the other
    single_cols = get_target_columns_for_names(EQUIPMENT_COLUMN_GROUPS["sup_single"])
    double_cols = get_target_columns_for_names(EQUIPMENT_COLUMN_GROUPS["sup_double"])

    if original_equipment == "sup_single":
        primary_cols, fallback_cols = single_cols, double_cols
    else:
        primary_cols, fallback_cols = double_cols, single_cols

    free_primary = find_free_columns_for_duration(all_values, row_idx, duration, primary_cols, quantity)
    needed = quantity - len(free_primary)
    free_fallback = find_free_columns_for_duration(all_values, row_idx, duration, fallback_cols, needed) if needed > 0 else []
    free_cols = free_primary + free_fallback

    if len(free_cols) < quantity:
        await callback.answer("❌ На цей час немає достатньої кількості вільних сапів", show_alert=True)
        return

    await state.update_data(quantity=quantity, equip_cols=free_cols)
    await callback.message.edit_text("Введіть ваше Прізвище та Ім'я:")
    await state.set_state(Booking.name)

@dp.message(Booking.name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(client_name=message.text)

    phone_keyboard = types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="📱 Поділитись номером", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer("Надішліть ваш номер телефону:", reply_markup=phone_keyboard)
    await state.set_state(Booking.phone)


@dp.message(Booking.phone)
async def process_phone(message: types.Message, state: FSMContext):
    if message.contact:
        phone = message.contact.phone_number
        if not phone.startswith("+"):
            phone = "+" + phone
    else:
        await message.answer("Будь ласка, скористайтесь кнопкою нижче щоб поділитись номером.")
        return

    data = await state.get_data()
    client_name = data.get('client_name', '')
    await finalize_booking(message, state, phone, client_name, data)


async def finalize_booking(message: types.Message, state: FSMContext, phone: str, client_name: str, data: dict):
    await message.answer("Відбувається процес бронювання!\nДякуємо за очікування💙")

    try:
        if is_phone_blacklisted(phone):
            await message.answer("❌ Цей номер телефону є у чорному списку. Бронювання недоступне.")
            await state.clear()
            return

        ws = get_or_create_sheet(data['date'])
        duration = int(data.get('duration', 1))
        booking_code = generate_booking_code()
        actual_equipment = data.get('actual_equipment', data['equipment'])
        equipment_note = data.get('equipment_note', '')
        user_chat_id = message.chat.id
        quantity = int(data.get('quantity', 1))
    except Exception as e:
        print(f"[ERROR] finalize_booking - initialization failed: {e}")
        await message.answer("❌ Помилка при ініціалізації бронювання. Спробуйте ще раз.")
        await state.clear()
        return

    # booking_value written to cells: ID, name, phone, telegram user id
    booking_lines = [f"ID:{booking_code}", client_name, phone, f"UID:{user_chat_id}"]
    if equipment_note:
        booking_lines.append(equipment_note)
    booking_value = "\n".join(booking_lines)

    # --- РАНКОВИЙ СПЛАВ ---
    if data.get('morning'):
        try:
            booking_window = MORNING_WINDOW
            actual_equipment = EQUIPMENT_LABELS.get(data.get('equipment'), data.get('equipment'))

            async with get_sheet_lock(data['date']):
                all_vals = ensure_morning_table(ws)

                header_row_idx = None
                for r_idx, row in enumerate(all_vals):
                    if row and len(row) > 0 and isinstance(row[0], str) and row[0].strip().lower().startswith("ранков"):
                        header_row_idx = r_idx
                        break

                if header_row_idx is None:
                    await message.answer("❌ Сталася помилка структури таблиці. Спробуйте ще раз або зверніться до адміністратора.")
                    await state.clear()
                    return

                write_row = header_row_idx + 2  # header row + 1 data row (1-based)

                requested_equipment = data.get('equipment')
                target_cols = get_target_columns_for_names(EQUIPMENT_COLUMN_GROUPS[requested_equipment])

                free_cols = find_free_columns_for_duration(all_vals, write_row, 1, target_cols, quantity)

                if len(free_cols) < quantity:
                    await message.answer("❌ Не вдалося зафіксувати бронювання: недостатньо вільних сапів на цей час.")
                    await state.clear()
                    return

                for col in free_cols:
                    start_cell = gspread.utils.rowcol_to_a1(write_row, col)
                    ws.update(start_cell, [[booking_value]])

            duration = 1
        except Exception as e:
            print(f"[ERROR] finalize_booking - morning booking failed: {e}")
            await message.answer("❌ Помилка при збереженні ранкового бронювання. Спробуйте ще раз.")
            await state.clear()
            return

    # --- ЗВИЧАЙНИЙ СПЛАВ ---
    else:
        try:
            async with get_sheet_lock(data['date']):
                # Re-check availability under the lock to avoid races overwriting another booking
                fresh_values = ws.get_all_values()
                if quantity > 1:
                    requested_equipment = data.get('resolved_equipment', data.get('equipment'))
                    preferred_cols = get_target_columns_for_names(EQUIPMENT_COLUMN_GROUPS.get(requested_equipment, []))
                    single_cols = get_target_columns_for_names(EQUIPMENT_COLUMN_GROUPS["sup_single"])
                    double_cols = get_target_columns_for_names(EQUIPMENT_COLUMN_GROUPS["sup_double"])
                    original_equipment = data.get('equipment')
                    if original_equipment == "sup_single":
                        primary_cols, fallback_cols = single_cols, double_cols
                    else:
                        primary_cols, fallback_cols = double_cols, single_cols

                    free_primary = find_free_columns_for_duration(fresh_values, data['time_row'], duration, primary_cols, quantity)
                    needed = quantity - len(free_primary)
                    free_fallback = find_free_columns_for_duration(fresh_values, data['time_row'], duration, fallback_cols, needed) if needed > 0 else []
                    equip_cols = free_primary + free_fallback

                    if len(equip_cols) < quantity:
                        await message.answer("❌ Не вдалося зафіксувати бронювання: недостатньо вільних сапів на цей час.")
                        await state.clear()
                        return

                    for row_offset in range(duration):
                        row_idx = data['time_row'] + row_offset
                        for col in equip_cols:
                            start_cell = gspread.utils.rowcol_to_a1(row_idx, col)
                            ws.update(start_cell, [[booking_value]])
                else:
                    # quantity == 1, single equipment item
                    if 'equip_col' not in data:
                        print(f"[ERROR] finalize_booking - equip_col missing for single booking")
                        await message.answer("❌ Помилка: обладнання не обрано правильно. Спробуйте ще раз.")
                        await state.clear()
                        return
                    
                    if _col_status(fresh_values, data['time_row'], duration, data['equip_col']) != "free":
                        await message.answer("❌ На жаль, це місце вже зайняли. Спробуйте інший час або обладнання.")
                        await state.clear()
                        return
                    start_cell = gspread.utils.rowcol_to_a1(data['time_row'], data['equip_col'])
                    end_cell = gspread.utils.rowcol_to_a1(data['time_row'] + duration - 1, data['equip_col'])
                    ws.update(f"{start_cell}:{end_cell}", [[booking_value] for _ in range(duration)])

            start_slot_index = data['time_row'] - 2
            booking_window = build_time_window(start_slot_index, duration)
        except KeyError as e:
            print(f"[ERROR] finalize_booking - missing data key: {e}")
            await message.answer("❌ Помилка при обробці даних бронювання. Спробуйте ще раз.")
            await state.clear()
            return
        except Exception as e:
            print(f"[ERROR] finalize_booking - regular booking failed: {e}")
            await message.answer("❌ Помилка при збереженні бронювання. Спробуйте ще раз.")
            await state.clear()
            return

    try:
        schedule_booking_reminder(
            user_chat_id=user_chat_id,
            booking_code=booking_code,
            booking_date=data['date'],
            booking_window=booking_window,
            actual_equipment=actual_equipment,
            equipment_note=equipment_note,
        )

        if data.get('morning'):
            morning_booking_chat_ids.setdefault(data['date'], set()).add(user_chat_id)
            schedule_morning_finalization(data['date'])

        notify_text = (
            "Нове бронювання!\n"
            f"Код: {booking_code}\n"
            f"Дата: {data['date']}\n"
            f"Час: {booking_window}\n"
            f"Тривалість: {duration} год\n"
            f"Кількість сапів: {quantity}\n"
            f"Обладнання: {actual_equipment} {equipment_note}".rstrip() + "\n"
            f"Клієнт: {client_name}\n"
            f"Телефон: {phone}"
        )
        if STAFF_CHAT_ID is not None:
            try:
                print(f"[INFO] Надсилаємо повідомлення в чат {STAFF_CHAT_ID}")
                await bot.send_message(chat_id=STAFF_CHAT_ID, text=notify_text)
                print(f"[INFO] Повідомлення успішно надіслано")
            except Exception as e:
                print(f"[ERROR] Помилка при надсиланні повідомлення в чат персоналу: {e}")
        else:
            print("[WARNING] STAFF_CHAT_ID не задано")

        reply_keyboard = types.ReplyKeyboardMarkup(
            keyboard=[
                [types.KeyboardButton(text="📚 Забронювати"), types.KeyboardButton(text="❌ Скасувати бронювання")],
                [types.KeyboardButton(text="📋 Правила користування"), types.KeyboardButton(text="🚨 Краш ліст")],
                [types.KeyboardButton(text="🍽️ Меню")],
                [types.KeyboardButton(text="📞 Контакти")]
            ],
            resize_keyboard=True
        )
        if data.get('morning'):
            await message.answer(
                f"✅ Бронювання прийнято!\n"
                f"Дата: {data['date']}\n"
                f"Ім'я клієнта: {client_name}\n"
                f"Номер бронювання: {booking_code}\n"
                f"Тривалість: {duration} год\n"
                f"Кількість сапів: {quantity}\n"
                f"Час: {booking_window}\n"
                f"Обладнання: {actual_equipment} {equipment_note}".rstrip() + "\n"
                f"Наш менеджер зв'яжеться з вами для оплати та підтвердження бронювання.",
                reply_markup=reply_keyboard
            )
        else:
            await message.answer(
                f"✅ Записано!\n"
                f"Дата: {data['date']}\n"
                f"Ім'я клієнта: {client_name}\n"
                f"Номер бронювання: {booking_code}\n"
                f"Тривалість: {duration} год\n"
                f"Кількість сапів: {quantity}\n"
                f"Час: {booking_window}\n"
                f"Обладнання: {actual_equipment} {equipment_note}".rstrip() + "\n"
                f"Чекаємо вас на воді!",
                reply_markup=reply_keyboard
            )
        await state.clear()
    except Exception as e:
        print(f"[ERROR] finalize_booking - notification/finalization failed: {e}")
        await message.answer("⚠️ Бронювання було збережено, але сталася помилка при надісланні повідомлення.")
        await state.clear()
        return


WEBAPP_API_PORT = int(os.getenv("WEBAPP_API_PORT", "8081"))


def _cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


async def api_options(request: web.Request) -> web.Response:
    return web.Response(headers=_cors_headers())


def _sup_max_qty(all_values, row_idx: int, duration: int, requested_equipment: str) -> int:
    """How many SUPs (including cross-type fallback) are free for this slot."""
    single_cols = get_target_columns_for_names(EQUIPMENT_COLUMN_GROUPS["sup_single"])
    double_cols = get_target_columns_for_names(EQUIPMENT_COLUMN_GROUPS["sup_double"])
    primary_cols, fallback_cols = (single_cols, double_cols) if requested_equipment == "sup_single" else (double_cols, single_cols)
    free_primary = find_free_columns_for_duration(all_values, row_idx, duration, primary_cols, 999)
    free_fallback = find_free_columns_for_duration(all_values, row_idx, duration, fallback_cols, 999)
    return min(10, len(free_primary) + len(free_fallback))


async def api_availability(request: web.Request) -> web.Response:
    headers = _cors_headers()
    try:
        date_str = request.query.get("date", "")
        equipment = request.query.get("equipment", "")
        duration_param = request.query.get("duration", "1")

        if equipment not in EQUIPMENT_COLUMN_GROUPS:
            return web.json_response({"error": "invalid equipment"}, status=400, headers=headers)
        try:
            datetime.strptime(date_str, "%d.%m")
        except ValueError:
            return web.json_response({"error": "invalid date, expected dd.mm"}, status=400, headers=headers)

        ws = get_or_create_sheet(date_str)
        all_values = ws.get_all_values()

        current_time = get_current_time()
        today_str = current_time.strftime("%d.%m")
        now_time = current_time.time()

        if duration_param == "morning":
            if is_weather_blocked_sheet(all_values):
                return web.json_response({"closed": True, "available": False, "maxQty": 0}, headers=headers)

            ensure_morning_table(ws)
            if is_morning_weather_blocked(ws):
                return web.json_response({"closed": True, "available": False, "maxQty": 0}, headers=headers)

            confirmed = is_morning_confirmed(ws)
            past_deadline = current_time >= get_morning_booking_deadline(date_str)
            if past_deadline and not confirmed:
                return web.json_response({"closed": True, "available": False, "maxQty": 0}, headers=headers)

            fresh_vals = ws.get_all_values()
            header_row_idx = None
            for r_idx, row in enumerate(fresh_vals):
                if row and isinstance(row[0], str) and row[0].strip().lower().startswith("ранков"):
                    header_row_idx = r_idx
                    break
            if header_row_idx is None:
                return web.json_response({"closed": True, "available": False, "maxQty": 0}, headers=headers)

            write_row = header_row_idx + 2
            if equipment.startswith("sup_"):
                max_qty = _sup_max_qty(fresh_vals, write_row, 1, equipment)
            else:
                target_cols = get_target_columns_for_names(EQUIPMENT_COLUMN_GROUPS[equipment])
                max_qty = len(find_free_columns_for_duration(fresh_vals, write_row, 1, target_cols, 999))

            return web.json_response({"closed": False, "available": max_qty > 0, "maxQty": max_qty}, headers=headers)

        duration = int(duration_param)
        if duration not in (1, 2):
            return web.json_response({"error": "invalid duration"}, status=400, headers=headers)

        if is_weather_blocked_sheet(all_values):
            return web.json_response({"closed": True, "slots": []}, headers=headers)

        target_cols = get_target_columns_for_names(EQUIPMENT_COLUMN_GROUPS[equipment])
        max_start_index = len(TIME_SLOTS) - duration
        slots = []

        for start_index in range(max_start_index + 1):
            start_time_str = TIME_SLOTS[start_index].split('-')[0]
            start_time = datetime.strptime(start_time_str, "%H:%M").time()
            row_idx = start_index + 2

            if date_str == today_str and start_time <= now_time:
                slots.append({"start": start_time_str, "available": False, "maxQty": 0})
                continue

            if is_weather_blocked_slot(all_values, row_idx, duration, target_cols):
                slots.append({"start": start_time_str, "available": False, "maxQty": 0})
                continue

            if is_live_queue_only_slot(all_values, row_idx, duration, target_cols):
                # Mini app doesn't support the live-queue flow — treat as unavailable here.
                slots.append({"start": start_time_str, "available": False, "maxQty": 0})
                continue

            booking_resolution = resolve_equipment_booking(equipment, all_values, row_idx, duration)
            if not booking_resolution:
                slots.append({"start": start_time_str, "available": False, "maxQty": 0})
                continue

            if equipment.startswith("sup_"):
                max_qty = _sup_max_qty(all_values, row_idx, duration, equipment)
            else:
                max_qty = 1

            slots.append({"start": start_time_str, "available": max_qty > 0, "maxQty": max_qty})

        return web.json_response({"closed": False, "slots": slots}, headers=headers)

    except Exception as e:
        print(f"[ERROR] api_availability: {e}")
        return web.json_response({"error": "internal error"}, status=500, headers=headers)


async def api_mybookings(request: web.Request) -> web.Response:
    headers = _cors_headers()
    try:
        user_id = request.query.get("user_id", "")
        if not user_id:
            return web.json_response({"error": "missing user_id"}, status=400, headers=headers)
        marker = f"UID:{user_id}"

        results = {}  # (date, code) -> {rows:set, cols:set, is_morning:bool}
        for ws in get_booking_worksheets_from_today():
            date_str = ws.title
            all_values = ws.get_all_values()
            for r_idx, row in enumerate(all_values, start=1):
                for c_idx, cell in enumerate(row, start=1):
                    if marker in cell:
                        code = None
                        for line in cell.split("\n"):
                            if line.startswith("ID:"):
                                code = line[3:]
                                break
                        if not code:
                            continue
                        key = (date_str, code)
                        entry = results.setdefault(key, {"rows": set(), "cols": set(), "is_morning": r_idx > len(TIME_SLOTS) + 1})
                        entry["rows"].add(r_idx)
                        entry["cols"].add(c_idx)

        bookings = []
        for (date_str, code), entry in results.items():
            equipment_names = sorted({get_equipment_name_for_column(c) for c in entry["cols"]})
            qty = len(entry["cols"])
            equipment_label = f"{', '.join(equipment_names)} × {qty}" if qty > 1 else (equipment_names[0] if equipment_names else "Невідомо")

            if entry["is_morning"]:
                time_label = "Сплав на світанку"
            else:
                rows = sorted(entry["rows"])
                start_idx = rows[0] - 2
                end_idx = rows[-1] - 2
                if 0 <= start_idx < len(TIME_SLOTS) and 0 <= end_idx < len(TIME_SLOTS):
                    time_label = f"{TIME_SLOTS[start_idx].split('-')[0]}-{TIME_SLOTS[end_idx].split('-')[1]}"
                else:
                    time_label = "Невідомо"

            bookings.append({"code": code, "date": date_str, "time": time_label, "equipment": equipment_label})

        return web.json_response(bookings, headers=headers)

    except Exception as e:
        print(f"[ERROR] api_mybookings: {e}")
        return web.json_response({"error": "internal error"}, status=500, headers=headers)


async def start_web_api():
    app = web.Application()
    app.router.add_get("/api/availability", api_availability)
    app.router.add_get("/api/mybookings", api_mybookings)
    app.router.add_route("OPTIONS", "/api/availability", api_options)
    app.router.add_route("OPTIONS", "/api/mybookings", api_options)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBAPP_API_PORT)
    await site.start()
    print(f"[INFO] Availability API listening on 0.0.0.0:{WEBAPP_API_PORT}")


async def main():
    await bot.set_my_commands([
        BotCommand(command="start", description="Bot Restart"),
    ])
    await start_web_api()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())