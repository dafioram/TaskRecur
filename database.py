import calendar
import datetime
import os
import sqlite3
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.relativedelta import relativedelta, weekday as rd_weekday
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

db = SQLAlchemy()

# --- TIMEZONE ---
# Resolve "today" through zoneinfo (backed by the pip `tzdata` package) instead of
# the OS clock. Slim Docker images may ship without /usr/share/zoneinfo, in which
# case the TZ env var is silently ignored and the container runs on UTC.
try:
    LOCAL_TZ = ZoneInfo(os.environ.get('TZ', 'America/New_York'))
except (ZoneInfoNotFoundError, ValueError):
    print(f"Unknown TZ {os.environ.get('TZ')!r}; falling back to UTC")
    LOCAL_TZ = ZoneInfo('UTC')


def now():
    """Naive local datetime (matches what is already stored in the DB)."""
    return datetime.datetime.now(LOCAL_TZ).replace(tzinfo=None)


def today():
    return now().date()


UNITS = ('days', 'weeks', 'months', 'years')
ORDINALS = {1: '1st', 2: '2nd', 3: '3rd', 4: '4th', -1: 'last'}


def week_of_month(d):
    """(n, is_last) for a date: e.g. Oct 13 2026 -> (2, False) = 2nd Tuesday."""
    n = (d.day - 1) // 7 + 1
    is_last = d.day + 7 > calendar.monthrange(d.year, d.month)[1]
    return n, is_last


class RecurrenceTemplate(db.Model):
    __tablename__ = 'recurrence_template'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    context = db.Column(db.Text, nullable=True)
    label = db.Column(db.String(50), nullable=True)
    assignee = db.Column(db.String(50), nullable=True)

    # Types: 'relative' (from completion date), 'interval' (from previous due date)
    recurrence_type = db.Column(db.String(30), nullable=False)
    interval_value = db.Column(db.Integer, default=1)
    interval_unit = db.Column(db.String(20))  # 'days', 'weeks', 'months', 'years'

    # Anchor for month/year intervals, so "every month on the 31st" lands on the
    # last day of short months and returns to the 31st afterwards (no drift).
    calendar_month = db.Column(db.Integer, nullable=True)
    calendar_day = db.Column(db.Integer, nullable=True)
    # Monthly "nth weekday" rule, e.g. 2nd Tuesday: monthly_week=2, monthly_weekday=1.
    # monthly_week is 1-4, or -1 for "last". Mon=0 .. Sun=6.
    monthly_week = db.Column(db.Integer, nullable=True)
    monthly_weekday = db.Column(db.Integer, nullable=True)

    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=now)

    def _step(self, n):
        unit = self.interval_unit if self.interval_unit in UNITS else 'days'
        size = max(1, self.interval_value or 1) * n
        anchor = {}
        if self.recurrence_type == 'interval':
            if unit == 'months' and self.monthly_week and self.monthly_weekday is not None:
                wd = rd_weekday(self.monthly_weekday)
                if self.monthly_week == -1:
                    anchor = {'day': 31, 'weekday': wd(-1)}  # last <weekday> of the month
                else:
                    anchor = {'day': 1, 'weekday': wd(self.monthly_week)}
            elif unit in ('months', 'years') and self.calendar_day:
                anchor['day'] = self.calendar_day  # relativedelta clamps to month end
            if unit == 'years' and self.calendar_month:
                anchor['month'] = self.calendar_month
        return relativedelta(**{unit: size}, **anchor)

    def calculate_next_date(self, baseline_date):
        """Next due date strictly after today."""
        t = today()
        if self.recurrence_type == 'relative':
            # Always counts forward from the moment of completion (today)
            return t + self._step(1)

        # 'interval': fixed schedule counted from the previous due date.
        # Missed occurrences are skipped; each candidate is computed from the
        # baseline (not the previous candidate) so month-end dates don't drift.
        base = baseline_date or t
        k = 1
        while True:
            candidate = base + self._step(k)
            if candidate > t:
                return candidate
            k += 1

    def set_anchor_from(self, due_date, monthly_mode='day'):
        """Pin month/year repeats to the due date: its day number, or (monthly only)
        its weekday position, e.g. 'nth' -> 2nd Tuesday, 'last' -> last Tuesday."""
        self.calendar_day = self.calendar_month = None
        self.monthly_week = self.monthly_weekday = None
        if self.recurrence_type != 'interval' or self.interval_unit not in ('months', 'years'):
            return
        n, is_last = week_of_month(due_date)
        if self.interval_unit == 'months' and monthly_mode == 'nth' and n <= 4:
            self.monthly_week, self.monthly_weekday = n, due_date.weekday()
        elif self.interval_unit == 'months' and monthly_mode == 'last' and is_last:
            self.monthly_week, self.monthly_weekday = -1, due_date.weekday()
        else:
            self.calendar_day = due_date.day
            if self.interval_unit == 'years':
                self.calendar_month = due_date.month

    def monthly_mode(self):
        if self.monthly_week == -1:
            return 'last'
        return 'nth' if self.monthly_week else 'day'

    def describe(self):
        n = max(1, self.interval_value or 1)
        unit = self.interval_unit or 'days'
        every = f"Every {unit[:-1]}" if n == 1 else f"Every {n} {unit}"
        if self.recurrence_type == 'relative':
            return f"{every} after completion"
        if unit == 'years' and self.calendar_month and self.calendar_day:
            day = min(self.calendar_day, calendar.monthrange(2000, self.calendar_month)[1])
            return f"{every} on {datetime.date(2000, self.calendar_month, day):%b %-d}"
        if unit == 'months' and self.monthly_week and self.monthly_weekday is not None:
            return f"{every} on the {ORDINALS[self.monthly_week]} {calendar.day_abbr[self.monthly_weekday]}"
        if unit == 'months' and self.calendar_day:
            return f"{every} on day {self.calendar_day}"
        return every


class Event(db.Model):
    __tablename__ = 'event'
    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey('recurrence_template.id', ondelete='SET NULL'), nullable=True)
    content = db.Column(db.Text, nullable=False)
    context = db.Column(db.Text, nullable=True)

    label = db.Column(db.String(50), nullable=True)
    assignee = db.Column(db.String(50), nullable=True)
    due_date = db.Column(db.Date, nullable=False)
    completion_note = db.Column(db.Text, nullable=True)
    skipped = db.Column(db.Boolean, nullable=False, default=False, server_default='0')

    created_at = db.Column(db.DateTime, default=now)
    completed_at = db.Column(db.DateTime, nullable=True)

    def days_left(self):
        return (self.due_date - today()).days

    def get_urgency_color(self):
        if self.completed_at:
            return "#555555"  # Muted gray for history
        delta = self.days_left()
        if delta < 0:
            return "#ff5252"  # Red: overdue
        if delta <= 1:
            return "#ffab40"  # Amber: due today / tomorrow
        if delta <= 7:
            return "#ffd740"  # Yellow: this week
        return "#448aff"      # Blue: later

    def get_time_display(self):
        delta = self.days_left()
        if delta == 0: return "Due today"
        if delta == 1: return "Due tomorrow"
        if delta > 1: return f"{delta} days left"
        if delta == -1: return "1 day overdue"
        return f"{abs(delta)} days overdue"


def init_db(app):
    db.init_app(app)
    with app.app_context():
        db.session.execute(text("PRAGMA journal_mode=WAL"))
        db.create_all()
        add_missing_columns()
        # One-time data fixes (idempotent):
        # 'calendar' was "every N years on Month/Day" -> now interval/years with an anchor.
        db.session.execute(text(
            "UPDATE recurrence_template SET recurrence_type='interval', interval_unit='years' "
            "WHERE recurrence_type='calendar'"))
        # Guard against bad intervals that would loop forever.
        db.session.execute(text(
            "UPDATE recurrence_template SET interval_value=1 WHERE interval_value IS NULL OR interval_value < 1"))
        # Drop dangling references left behind by earlier deletes.
        db.session.execute(text(
            "UPDATE event SET template_id=NULL WHERE template_id IS NOT NULL "
            "AND template_id NOT IN (SELECT id FROM recurrence_template)"))
        # Skips used to be stored as the note text "[SKIPPED]"; now a flag so they can have notes.
        db.session.execute(text(
            "UPDATE event SET skipped=1, completion_note=NULL WHERE completion_note='[SKIPPED]'"))
        db.session.commit()


def add_missing_columns():
    """Tiny migration step: add columns introduced after the DB was created."""
    wanted = {
        'event': {'skipped': "BOOLEAN NOT NULL DEFAULT 0"},
        'recurrence_template': {'monthly_week': "INTEGER", 'monthly_weekday': "INTEGER"},
    }
    for table, cols in wanted.items():
        have = {row[1] for row in db.session.execute(text(f"PRAGMA table_info({table})"))}
        for name, ddl in cols.items():
            if name not in have:
                db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def perform_backup(db_path, data_dir, keep=5):
    """Writes a consistent snapshot (WAL-safe) and keeps the `keep` most recent."""
    if not os.path.exists(db_path):
        return False, "Database file not found."

    backup_dir = os.path.join(data_dir, 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(backup_dir, f'tasks_{now():%Y%m%d_%H%M%S}.db')

    try:
        # sqlite3's backup API includes pages still sitting in the -wal file,
        # which a plain file copy of tasks.db would miss.
        src, dst = sqlite3.connect(db_path), sqlite3.connect(backup_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        backups = sorted(f for f in os.listdir(backup_dir) if f.startswith('tasks_') and f.endswith('.db'))
        for old in backups[:-keep]:
            os.remove(os.path.join(backup_dir, old))
        return True, backup_path
    except Exception as e:
        print(f"Backup failed: {e}")
        return False, str(e)

