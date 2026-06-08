import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from dateutil.relativedelta import relativedelta
import os
import shutil

db = SQLAlchemy()

class RecurrenceTemplate(db.Model):
    __tablename__ = 'recurrence_template'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    context = db.Column(db.Text, nullable=True)
    label = db.Column(db.String(50), nullable=True)
    assignee = db.Column(db.String(50), nullable=True)
    
    # Types: 'relative', 'interval', 'calendar'
    recurrence_type = db.Column(db.String(30), nullable=False)
    interval_value = db.Column(db.Integer, default=1)
    interval_unit = db.Column(db.String(20)) # 'days', 'weeks', 'months'
    
    calendar_month = db.Column(db.Integer, nullable=True)
    calendar_day = db.Column(db.Integer, nullable=True)
    
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.datetime.now)

    def calculate_next_date(self, baseline_date):
            """Calculates the next valid future date strictly after the baseline."""
            today = datetime.date.today()
            # Use baseline if provided, otherwise fallback to today
            current = baseline_date if baseline_date else today
            
            if self.recurrence_type == 'relative':
                # Relative ALWAYS moves forward from the moment of completion (today)
                delta_args = {self.interval_unit: self.interval_value}
                return today + relativedelta(**delta_args)

            elif self.recurrence_type == 'interval':
                # Interval moves forward from the target due date
                delta_args = {self.interval_unit: self.interval_value}
                while True:
                    current = current + relativedelta(**delta_args)
                    if current > today:
                        return current

            elif self.recurrence_type == 'calendar':
                # Calendar moves to the next year if the baseline date is already this year's instance
                year = current.year
                try:
                    candidate = datetime.date(year, self.calendar_month, self.calendar_day)
                except ValueError: # Handle Feb 29
                    candidate = datetime.date(year, self.calendar_month, self.calendar_day - 1)
                
                # If our candidate is the same as or before the baseline, 
                # we MUST jump to at least the next year.
                if candidate <= current:
                    year += 1
                
                while True:
                    try:
                        candidate = datetime.date(year, self.calendar_month, self.calendar_day)
                    except ValueError:
                        candidate = datetime.date(year, self.calendar_month, self.calendar_day - 1)
                    
                    if candidate > today:
                        return candidate
                    year += 1
                    
            return None

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
    parent_id = db.Column(db.Integer, nullable=True)
    
    created_at = db.Column(db.DateTime, default=datetime.datetime.now)
    completed_at = db.Column(db.DateTime, nullable=True)

    def get_urgency_color(self):
        if self.completed_at:
            return "#555555" # Muted gray for history
        import datetime
        delta = (self.due_date - datetime.date.today()).days
        if delta <= 1:
            return "#ff5252" # Coral Red: Overdue
        if delta <= 3:
            return "#ffab40" # Amber Orange: Due Today
        if delta <= 7:
            return "#ffd740" # Soft Yellow: Imminent
        return "#448aff"     # Electric Blue: Future window

    def get_time_display(self):
        if not self.due_date: return None
        today = datetime.date.today()
        delta = (self.due_date - today).days
        if delta == 0: return "Due today"
        if delta == 1: return "1 day left"
        if delta > 1:  return f"{delta} days left"
        if delta == -1: return "1 day overdue"
        return f"{abs(delta)} days overdue"

def init_db(app):
    db.init_app(app)
    with app.app_context():
        db.session.execute(text("PRAGMA journal_mode=WAL"))
        db.create_all()

import os
import datetime
import shutil

def perform_backup(db_path, data_dir):
    """Keeps up to 5 rolling historical database backups on application startup or API call."""
    if not os.path.exists(db_path):
        return False, "Database file not found."
        
    backup_dir = os.path.join(data_dir, 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = os.path.join(backup_dir, f'tasks_{timestamp}.db')
    
    try:
        shutil.copy2(db_path, backup_path)
        
        # Prune old backups, keeping only the 5 most recent
        all_backups = sorted(
            [os.path.join(backup_dir, f) for f in os.listdir(backup_dir) if f.endswith('.db')],
            key=os.path.getmtime
        )
        while len(all_backups) > 5:
            os.remove(all_backups.pop(0))
            
        # Return True for success and the path to the new backup file
        return True, backup_path
        
    except Exception as e:
        print(f"Backup engine failed safely: {e}")
        # Return False for failure and the string representation of the error
        return False, str(e)